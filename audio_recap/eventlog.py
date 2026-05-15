"""Append-only debug log for the Stop hook and slash command.

Live use surfaced cases where the audio output is the only signal
available — "the message ended mid-sentence," "the long reply played
verbatim instead of being summarized," "the hook took 90 s." All three
are debuggable from a record of the decisions the hook made on each
fire; without one, we have to guess.

:class:`FileEventLog` owns one append-only file. Every hook invocation
and every slash-command invocation appends one or more lines:

- :meth:`FileEventLog.event` writes structured ``key=value``-formatted
  INFO lines. Use this for "what path did the code take" entries the
  user will grep through.
- :meth:`FileEventLog.message` writes a free-form INFO line. Use this
  for one-off messages that don't decompose into structured fields. The
  mirror to stderr stays the caller's responsibility — see
  :func:`audio_recap.pipeline._user_warn`.
- :meth:`FileEventLog.event_trace` and :meth:`FileEventLog.message_trace`
  are the TRACE-level counterparts. They emit only when ``trace_enabled``
  was set at construction — INFO is the default, TRACE is opt-in via the
  ``log_level`` per-cwd config field. Use these for subprocess
  inputs/outputs, raw payloads, full tracebacks, or any data large
  enough that a contributor may not want it on every fire.

:class:`FileEventLog` takes its log path and ``trace_enabled`` flag as
constructor arguments and resolves nothing itself — no env vars, no
default paths. The composition root (:mod:`audio_recap.services`)
resolves the path and the TRACE setting and passes them in; tests
construct ``FileEventLog(tmp_path, trace_enabled=...)`` directly.

Format: ISO-8601 timestamp + level (``INFO`` / ``TRACE``) +
space-separated ``key=value`` tokens. Plain text, grep-friendly. Values
with spaces, ``=``, or quotes are single-quoted; embedded newlines /
tabs / carriage returns are escaped as the literal two-character
sequences ``\\n`` / ``\\t`` / ``\\r`` so every event stays exactly one
log line — even when a TRACE field is a multi-KB JSON blob or a
traceback.

INFO is the default level — TRACE is opt-in (set ``log_level`` to
``"trace"`` in the per-cwd config). Single global file, no rotation,
accumulates indefinitely — mirrors the "state files accumulate forever"
stance in :mod:`audio_recap.state`.

Failure mode: any I/O error during a write is swallowed silently. The
logger is a debugging aid, not a load-bearing path.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

LEVEL_INFO = "INFO"
LEVEL_TRACE = "TRACE"


def _timestamp() -> str:
    """Compact ISO-8601 with seconds resolution, UTC."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _format_value(value: Any) -> str:
    """Render a value for the structured ``key=value`` format.

    Booleans become ``true``/``false``. Other types are stringified;
    embedded control characters (``\\n``, ``\\r``, ``\\t``) are escaped
    to their literal two-character backslash sequences so a multi-line
    string never breaks the one-line-per-event invariant. Values with
    spaces, ``=``, or single quotes are wrapped in single quotes (with
    embedded quotes escaped) so the output stays grep-line-stable.
    """

    if isinstance(value, bool):
        return "true" if value else "false"
    s = str(value)
    if not s:
        return "''"
    s = s.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    if any(ch in s for ch in (" ", "=", "'")):
        # POSIX-style single-quote escape: `it's` → `'it'\''s'`.
        return "'" + s.replace("'", "'\\''") + "'"
    return s


def _format_fields(event: str, fields: dict[str, Any]) -> str:
    parts = [f"event={_format_value(event)}"]
    for key, value in fields.items():
        parts.append(f"{key}={_format_value(value)}")
    return " ".join(parts)


class EventLog(Protocol):
    """Append-only debug log seam.

    Production: :class:`FileEventLog`. Tests inject :class:`FakeEventLog`
    (in ``tests/fakes.py``) which captures each emit in memory for
    assertions. All methods are best-effort — implementations must
    swallow I/O errors silently; the logger is diagnostic, not
    load-bearing.
    """

    def event(self, event: str, **fields: Any) -> None:
        """Append a structured INFO line: ``event=<event> key=value …``."""
        ...

    def message(self, text: str) -> None:
        """Append a free-form INFO line. Caller mirrors to stderr if needed."""
        ...

    def event_trace(self, event: str, **fields: Any) -> None:
        """Append a structured TRACE line — opt-in via env var."""
        ...

    def message_trace(self, text: str) -> None:
        """Append a free-form TRACE line — opt-in via env var."""
        ...

    def record_fire_delta(self, event: str) -> int | None:
        """Record this fire's timestamp; return ms since the previous fire."""
        ...


class FileEventLog:
    """Disk-backed event log. Default production implementation.

    Both inputs are constructor arguments — no env-var or default
    resolution happens here. The composition root
    (:mod:`audio_recap.services`) resolves them and passes them in;
    tests pass an explicit ``path`` and ``trace_enabled``.

    ``trace_enabled`` gates :meth:`event_trace` / :meth:`message_trace`;
    it defaults to ``False`` (INFO) — TRACE is opt-in via the
    ``log_level`` per-cwd config field.
    """

    def __init__(self, path: Path, trace_enabled: bool = False) -> None:
        self._path = path
        self._trace_enabled = trace_enabled

    def _write_line(self, line: str) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            # Silent — the logger is best-effort. Callers also surface
            # human-readable failures via stderr.
            pass

    def _emit(self, level: str, body: str) -> None:
        self._write_line(f"{_timestamp()} {level} {body}")

    def event(self, event: str, **fields: Any) -> None:
        """Append a structured INFO event line.

        ``event`` is the leading ``event=`` value (e.g. ``"stop"``,
        ``"command"``). ``fields`` are additional ``key=value`` pairs in
        insertion order — a stable order makes diffs cleaner.
        """

        self._emit(LEVEL_INFO, _format_fields(event, fields))

    def message(self, text: str) -> None:
        """Append a free-form INFO line. Caller mirrors to stderr if needed."""

        self._emit(LEVEL_INFO, text)

    def event_trace(self, event: str, **fields: Any) -> None:
        """Append a structured TRACE event line.

        No-op unless ``trace_enabled`` was set at construction. Use for
        full payloads, prompts, responses, tracebacks, or any
        high-volume field we don't want on every fire.
        """

        if not self._trace_enabled:
            return
        self._emit(LEVEL_TRACE, _format_fields(event, fields))

    def message_trace(self, text: str) -> None:
        """Append a free-form TRACE line. No-op unless ``trace_enabled``."""

        if not self._trace_enabled:
            return
        self._emit(LEVEL_TRACE, text)

    def _fire_delta_path(self, event: str) -> Path:
        """Sidecar file storing the last-fire epoch ms for a given event type.

        One per event type so different hooks (stop, future SessionEnd…)
        don't trample each other. Lives next to the log file.
        """

        return self._path.parent / f".last_fire_{event}"

    def record_fire_delta(self, event: str) -> int | None:
        """Record this fire's timestamp; return ms since the previous fire.

        Returns ``None`` on the very first fire (no prior timestamp on
        disk) or if the sidecar can't be read/written. The hook surfaces
        the value as the ``since_last_fire_ms`` field — it makes
        rapid-retrigger bugs visible at a glance.

        Best-effort: any I/O error is swallowed and returns ``None``.
        """

        now_ms = int(time.time() * 1000)
        path = self._fire_delta_path(event)
        delta: int | None = None
        try:
            prev_text = path.read_text(encoding="utf-8").strip()
            prev_ms = int(prev_text)
            delta = now_ms - prev_ms
        except (OSError, ValueError):
            delta = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(now_ms), encoding="utf-8")
        except OSError:
            pass
        return delta


__all__ = [
    "LEVEL_INFO",
    "LEVEL_TRACE",
    "EventLog",
    "FileEventLog",
]
