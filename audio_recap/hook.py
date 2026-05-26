"""Stop-hook entrypoint.

Reads the Claude Code Stop-event JSON payload from stdin, parses it
into a :class:`audio_recap.payload.TurnPayload`, consults persistent
state keyed on ``session_id`` alone, optionally runs the recap +
summary :class:`audio_recap.pipeline.Pipeline`, and speaks the result
through the configured :class:`audio_recap.tts.TTS` backend. One-shot
per invocation; no long-running state.

Every fire appends a structured trail of ``key=value`` lines to
``~/.claude/audio-recap/logs/audio-recap.log`` (see
:mod:`audio_recap.eventlog`). ``tail -f`` that file or
``grep session_id=<id>`` to scope to a single session — the log is
the canonical "what did the hook decide" surface.

State files live forever — same lifetime as Claude Code's own
``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`` transcripts, so
a session resumed months later sees the toggle it had when it last
ran. Disk cost is negligible (~30 bytes per session). The plugin does
not register a SessionEnd hook and does not GC.

Failure behavior:

- Invalid JSON on stdin or non-object payload → log and exit 1.
- Audio Recap disabled via ``/audio-recap:off`` → log and exit 0 (no
  side effects).
- ``claude -p`` recap fails → fall back to the rule-based recap. If
  that too fails → log and speak the message alone.
- Long-message summarizer fails → log and speak the verbatim message.
- ``say`` fails → log and exit 1. Silent TTS failure would be worse
  than a loud one for a narration-first product.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

from audio_recap.payload import PayloadParser
from audio_recap.pipeline import Pipeline, TTSRunner
from audio_recap.services import Services, default_event_log
from audio_recap.state import State

_SPEECH_LOG_PREDICATE = (
    '(subsystem CONTAINS "speech") '
    'OR (subsystem == "com.apple.coreaudio") '
    'OR (process == "speechsynthesisd")'
)
_SPEECH_LOG_TIMEOUT_S = 5.0
_SPEECH_LOG_MAX_LINES = 20


def _capture_speech_log(window_s: float) -> str:
    """Capture the last ``window_s`` of speech-subsystem unified-log lines.

    Best-effort diagnostic: any failure (binary missing, timeout,
    non-zero exit) returns an empty string so the hook never blocks
    on diagnostic plumbing. Routes through ``subprocess.run`` directly
    rather than the central :class:`ProcessRunner` because this is a
    one-off diagnostic line that doesn't fit the recap/TTS flow shape.
    """

    span = max(1, round(window_s))
    try:
        result = subprocess.run(
            [
                "log",
                "show",
                "--info",
                "--predicate",
                _SPEECH_LOG_PREDICATE,
                "--last",
                f"{span}s",
                "--style",
                "compact",
            ],
            capture_output=True,
            text=True,
            timeout=_SPEECH_LOG_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    lines = [ln for ln in result.stdout.splitlines() if ln and not ln.startswith("Timestamp")]
    return "\n".join(lines[-_SPEECH_LOG_MAX_LINES:])


def main(raw: bytes, *, services: Services | None = None) -> int:
    """Stop-hook entry. Parses ``raw``, runs the Pipeline, speaks the result.

    ``raw`` is the Claude Code Stop-event JSON payload — the
    ``__main__`` dispatcher reads it from stdin and passes it in, so
    this function is pure-args and unit-testable without patching
    ``sys.stdin``. ``services`` defaults to a wired-up production graph
    derived from :meth:`Services.from_config`; tests pass a fake-loaded
    ``Services`` to substitute Recap / TTS / etc.
    """

    # Payload parsing runs before the Services graph (and the per-cwd
    # config) exists, so the parser logs to a bootstrap INFO event log.
    # When ``services`` is injected (tests), reuse its log instead.
    eventlog = services.eventlog if services is not None else default_event_log()
    parser = PayloadParser(eventlog)
    turn = parser.parse(raw)
    if turn is None:
        return 1

    if services is None:
        # Services builds the real event log after Config.load, so its
        # trace level reflects Config.log_level.
        services = Services.from_config(turn.cwd, session_id=turn.session_id)
    log = services.eventlog

    # Entry telemetry — single line per fire.
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

    # State gate.
    state: State = services.state.load(
        turn.session_id, turn.cwd, default_enabled=services.config.default_enabled
    )
    if not state.enabled:
        log.message("[audio-recap] audio recap disabled; skipping")
        sys.stderr.write("[audio-recap] audio recap disabled; skipping\n")
        log.event("stop", session_id=turn.session_id, state="disabled")
        return 0
    log.event("stop", session_id=turn.session_id, state="enabled")

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

    rc = TTSRunner(services).speak(result, session_id=turn.session_id, event="stop")
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

    # Optional speech-log telemetry under tts.trace_speech_log.
    if not services.config.dry_run and services.config.tts.trace_speech_log:
        log.event_trace(
            "trace_speech_log",
            session_id=turn.session_id,
            excerpt=_capture_speech_log(_SPEECH_LOG_TIMEOUT_S),
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.stdin.buffer.read()))
