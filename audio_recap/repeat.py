"""``/audio-recap:repeat`` slash-command entrypoint.

Replays the most recent narration audibly. State-independent: runs
whether ``/audio-recap:on`` is on or off (the on/off toggle gates
auto-narration on Stop events; ``/repeat`` is an explicit user
request for sound and is intentionally decoupled).

Decision tree:

1. **Cache hit** for the current ``session_id`` → speak the cached
   ``(recap, message)`` pair via TTS in the same order the Stop hook
   would. Instant.
2. **Cache miss** → read the most recent assistant turn from CC's
   own session transcript at
   ``~/.claude/projects/<encoded-cwd>/<session_id>.jsonl``, run the
   shared :class:`audio_recap.pipeline.Pipeline`, cache the result,
   speak it.
3. **Cache miss + transcript empty / unreadable** → speak a short
   audible "nothing to repeat" cue and log ``path=empty_transcript``.

V1 limitation: ``/repeat`` takes no playback lock, so if a Stop hook
is mid-speak when it fires the two overlap audibly. Deliberate for
now — it is an explicit "play it now" aimed at a session the user is
looking at, and queueing that behind background narration would be
the wrong answer to the request.

Presence is the other half and cuts the other way: a replay is a
sound this session made, so it should count. This module writes no
heartbeat itself, but on an enabled session the Stop hook that
follows the slash-command turn records one before it short-circuits
(see :mod:`audio_recap.hook`) — which is the wanted outcome. With
narration off the replay is still audible and nothing registers; a
session the user muted does not get to make other sessions announce
themselves.

Invocation shape mirrors ``audio_recap.command`` so ``scripts/run.sh``
can dispatch both:

    python -m audio_recap.repeat --session-id <id> --cwd <path>
"""

from __future__ import annotations

import os
import sys
import time

from audio_recap.pipeline import Pipeline, PipelineResult
from audio_recap.services import Services

_EMPTY_TRANSCRIPT_CUE = "Nothing to repeat in this session."

_USAGE = (
    "usage: repeat\n"
    "  --session-id <id>   per-session keying (substituted by CC)\n"
    "  --cwd <path>        project directory (defaults to $PWD)\n"
)


def _parse_flags(argv: list[str]) -> tuple[str | None, str | None, list[str]]:
    sid: str | None = None
    cwd: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--session-id" and i + 1 < len(argv):
            sid = argv[i + 1]
            i += 2
            continue
        if token == "--cwd" and i + 1 < len(argv):
            cwd = argv[i + 1]
            i += 2
            continue
        rest.append(token)
        i += 1
    return sid, cwd, rest


def _resolve_session_id(sid: str | None) -> str:
    return sid if sid else "_global"


def _resolve_cwd(cwd: str | None) -> str:
    return cwd if cwd else os.getcwd()


def _speak(services: Services, recap: str | None, message: str) -> int:
    """Speak ``(recap, message)`` via the configured TTS.

    Mirrors :class:`TTSRunner.speak` but bypasses the per-fire telemetry
    aggregation since /repeat already logs its own ``event=repeat`` line.
    Honors ``config.dry_run``.
    """

    if services.config.dry_run:
        return 0
    from audio_recap.tts import TTSFailed

    tts = services.tts
    try:
        if recap:
            tts.speak(recap)
        if message:
            tts.speak(message)
    except TTSFailed as e:
        sys.stderr.write(f"[audio-recap] TTS failed: {e}\n")
        return 1
    return 0


def _speak_empty_transcript_cue(
    services: Services, session_id: str, cwd: str, started: float
) -> int:
    sys.stderr.write("[audio-recap] nothing to repeat in this session\n")
    rc = _speak(services, None, _EMPTY_TRANSCRIPT_CUE)
    services.eventlog.event(
        "repeat",
        session_id=session_id,
        cwd=cwd,
        path="empty_transcript",
        elapsed_s=round(time.monotonic() - started, 3),
    )
    return rc


def main(argv: list[str] | None = None, *, services: Services | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    sid, cwd, rest = _parse_flags(argv[1:])
    session_id = _resolve_session_id(sid)
    project_cwd = _resolve_cwd(cwd)

    if rest:
        sys.stderr.write(_USAGE)
        return 2

    if services is None:
        services = Services.from_config(project_cwd, session_id=session_id)

    started = time.monotonic()
    cached = services.cache.read(session_id)
    if cached is not None:
        recap, message = cached
        rc = _speak(services, recap, message)
        services.eventlog.event(
            "repeat",
            session_id=session_id,
            cwd=project_cwd,
            path="cache_hit",
            had_recap=recap is not None,
            elapsed_s=round(time.monotonic() - started, 3),
        )
        return rc

    return _on_demand_generate(services, session_id, project_cwd, started)


def _on_demand_generate(
    services: Services,
    session_id: str,
    cwd: str,
    started: float,
) -> int:
    turn = services.transcript_reader.load(session_id, cwd)
    if turn is None:
        return _speak_empty_transcript_cue(services, session_id, cwd, started)

    result: PipelineResult = Pipeline(services).run(turn, event="repeat")
    if not result.recap_text and not result.message_text:
        return _speak_empty_transcript_cue(services, session_id, cwd, started)

    services.eventlog.event(
        "repeat",
        session_id=session_id,
        cwd=cwd,
        path="on_demand_generation",
        recap_path=result.recap_path,
        summary_path=result.summary_path,
        elapsed_s=round(time.monotonic() - started, 3),
    )
    rc = _speak(services, result.recap_text, result.message_text)
    services.cache.write(session_id, result.recap_text, result.message_text)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
