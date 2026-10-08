"""Stop-hook entrypoint.

Reads the Claude Code Stop-event JSON payload from stdin, consults
persistent state keyed on ``session_id`` alone, and only when narration
is on parses the turn into a :class:`audio_recap.payload.TurnPayload`
(reading the session transcript), runs the recap + summary
:class:`audio_recap.pipeline.Pipeline`, and speaks the result through
the configured :class:`audio_recap.tts.TTS` backend. One-shot per
invocation; no long-running state.

Every fire in a session with narration on appends a structured trail
of ``key=value`` lines to
``~/.claude/audio-recap/logs/audio-recap.log`` (see
:mod:`audio_recap.eventlog`). ``tail -f`` that file or
``grep session_id=<id>`` to scope to a single session — the log is
the canonical "what did the hook decide" surface.

State files live forever — same lifetime as Claude Code's own
``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`` transcripts, so
a session resumed months later sees the toggle it had when it last
ran. Disk cost is negligible (~30 bytes per session) and nothing GCs
them. (The plugin's one SessionEnd hook clears the session's presence
heartbeat; it does not touch state.)

Failure behavior:

- Invalid JSON on stdin or non-object payload → log and exit 1.
- Audio Recap disabled (the default, or ``/audio-recap:off``) → exit 0
  before the transcript is opened and without a per-turn log line (one
  TRACE line when ``AUDIO_RECAP_LOG_LEVEL=trace``). Building ``Services``
  can still log an unavailable lock file.
- ``claude -p`` recap fails → fall back to the rule-based recap. If
  that too fails → log and speak the message alone.
- Long-message summarizer fails → log and speak the verbatim message.
- ``say`` fails → log and exit 1. Silent TTS failure would be worse
  than a loud one for a narration-first product.
"""

from __future__ import annotations

import sys
from typing import Any

from audio_recap.payload import PayloadParser, cwd_from_payload, session_id_from_payload
from audio_recap.pipeline import Pipeline, TTSRunner
from audio_recap.services import Services, default_event_log
from audio_recap.state import State


def main(raw: bytes, *, services: Services | None = None) -> int:
    """Stop-hook entry. Parses ``raw``, runs the Pipeline, speaks the result.

    ``raw`` is the Claude Code Stop-event JSON payload — the
    ``__main__`` dispatcher reads it from stdin and passes it in, so
    this function is pure-args and unit-testable without patching
    ``sys.stdin``. ``services`` defaults to a wired-up production graph
    derived from :meth:`Services.from_config`; tests pass a fake-loaded
    ``Services`` to substitute Recap / TTS / etc.
    """

    # Decoding runs before the Services graph exists, so the parser logs to a
    # bootstrap event log (TRACE follows ``AUDIO_RECAP_LOG_LEVEL``). When
    # ``services`` is injected (tests), reuse its log instead.
    eventlog = services.eventlog if services is not None else default_event_log()
    payload = PayloadParser(eventlog).decode(raw)
    if payload is None:
        return 1
    session_id = session_id_from_payload(payload)
    cwd = cwd_from_payload(payload)

    if services is None:
        # Services builds the real event log, with its trace level from
        # ``AUDIO_RECAP_LOG_LEVEL``.
        services = Services.from_config(session_id=session_id)
    log = services.eventlog

    # State gate — before the transcript is opened or the turn is logged. A
    # session with narration off (the default) must not have its
    # conversation read or recorded: the hook takes the session id and cwd
    # from the payload, looks up the flag, and stops.
    state: State = services.state.load(session_id, cwd)
    if not state.enabled:
        sys.stderr.write("[audio-recap] audio recap disabled; skipping\n")
        log.event_trace("stop", session_id=session_id, state="disabled")
        return 0

    # Past the gate: build the turn, which reads the transcript.
    turn = PayloadParser.from_dict(payload)

    # Entry telemetry — single line per narrated fire.
    since_last_fire_ms = log.record_fire_delta("stop")
    fired_fields: dict[str, Any] = {
        "session_id": turn.session_id,
        "cwd": turn.cwd,
        "fired": True,
    }
    if since_last_fire_ms is not None:
        fired_fields["since_last_fire_ms"] = since_last_fire_ms
    log.event("stop", **fired_fields)
    PayloadParser.log_payload_trace(log, turn)
    log.event("stop", session_id=turn.session_id, state="enabled")

    # Presence heartbeat — mark this session as open with narration on, so
    # concurrent sessions announce their names to each other.
    #
    # ``/audio-recap:on`` writes it; this refresh keeps a long-lived session
    # ahead of the orphan horizon. Past the enabled gate, which is the whole
    # condition: a heartbeat means enabled and open.
    #
    # Best-effort, and the count excludes self, so this never affects its own
    # label. See :mod:`audio_recap.presence` for what a heartbeat means.
    services.presence_registry.heartbeat(turn.session_id)

    # ``/audio-recap:repeat`` short-circuits the pipeline so the user
    # doesn't hear "Replayed last narration." narrated on top of the
    # actual replay (which the slash command already produced).
    if turn.own_command == "repeat":
        log.event(
            "stop",
            session_id=turn.session_id,
            recap_path="skipped_slash_command",
            summary_path="skipped_slash_command",
            tts_status="skipped",
            slash_command="repeat",
        )
        return 0

    result = Pipeline(services).run(turn, event="stop")

    rc = TTSRunner(services).speak(
        result,
        session_id=turn.session_id,
        cwd=turn.cwd,
        custom_title=turn.custom_title,
        session_title=turn.session_title,
        event="stop",
    )
    if rc != 0:
        return rc

    # ``confirm`` slash commands (on/off/status) skip the cache write —
    # confirmation lines aren't conversation turns worth replaying.
    if turn.own_command == "confirm":
        log.event(
            "stop",
            session_id=turn.session_id,
            cache_skipped="own_command",
        )
    else:
        services.cache.write(turn.session_id, result.recap_text, result.message_text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.stdin.buffer.read()))
