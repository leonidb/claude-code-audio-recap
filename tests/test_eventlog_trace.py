"""TRACE-level emission gate, multi-line escaping, fire-delta sidecar.

The gate is the ``trace_enabled`` constructor arg — no env var. Resolution
of ``Config.log_level`` → ``trace_enabled`` is covered in ``test_config.py``
and the hook integration tests in ``test_hook_trace.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from audio_recap.eventlog import FileEventLog


@pytest.fixture
def elog(log_path: Path) -> FileEventLog:
    """A FileEventLog with TRACE on, writing to the test's tmp log file."""

    return FileEventLog(log_path, trace_enabled=True)


# ---------- emission gate: driven by the trace_enabled ctor arg ----------


def test_event_trace_writes_when_enabled(elog: FileEventLog, log_path: Path) -> None:
    elog.event_trace("trace_payload", session_id="abc", payload_json='{"foo": 1}')
    line = log_path.read_text(encoding="utf-8").strip()
    assert " TRACE " in line
    assert "event=trace_payload" in line
    assert "session_id=abc" in line
    # The JSON-encoded payload contains both ``{`` and a quote — should
    # be POSIX-quoted because it has spaces and ``=``-relevant chars.
    assert '"foo": 1' in line.replace("\\", "")


def test_event_trace_is_silent_when_disabled(log_path: Path) -> None:
    FileEventLog(log_path, trace_enabled=False).event_trace(
        "trace_payload", payload_json='{"foo": 1}'
    )
    assert not log_path.exists() or log_path.read_text(encoding="utf-8") == ""


def test_trace_is_off_by_default(log_path: Path) -> None:
    """INFO is the default — ``trace_enabled`` must be opted into."""

    FileEventLog(log_path).event_trace("trace_payload", payload_json='{"foo": 1}')
    assert not log_path.exists() or log_path.read_text(encoding="utf-8") == ""


def test_message_trace_writes_when_enabled(elog: FileEventLog, log_path: Path) -> None:
    elog.message_trace("[audio-recap] some debug detail")
    line = log_path.read_text(encoding="utf-8").strip()
    assert " TRACE " in line
    assert "[audio-recap] some debug detail" in line


def test_message_trace_is_silent_when_disabled(log_path: Path) -> None:
    FileEventLog(log_path, trace_enabled=False).message_trace("[audio-recap] some debug detail")
    assert not log_path.exists() or log_path.read_text(encoding="utf-8") == ""


def test_info_emission_unaffected_by_trace_disabled(log_path: Path) -> None:
    """INFO output is independent of the trace gate."""

    FileEventLog(log_path, trace_enabled=False).event("stop", session_id="abc")
    line = log_path.read_text(encoding="utf-8").strip()
    assert " INFO " in line
    assert " TRACE " not in line


# ---------- multi-line escape ----------


def test_newlines_escaped_to_literal_backslash_n(elog: FileEventLog, log_path: Path) -> None:
    elog.event_trace("trace_x", body="line1\nline2\nline3")
    text = log_path.read_text(encoding="utf-8")
    # Exactly one log line, even though the value held two newlines.
    assert text.count("\n") == 1
    # The literal escape sequence appears in the value.
    assert "\\n" in text


def test_tabs_and_carriage_returns_escaped(elog: FileEventLog, log_path: Path) -> None:
    elog.event_trace("trace_x", body="a\tb\rc")
    text = log_path.read_text(encoding="utf-8")
    assert text.count("\n") == 1
    assert "\\t" in text
    assert "\\r" in text


def test_traceback_string_stays_one_line(elog: FileEventLog, log_path: Path) -> None:
    """A real-shaped Python traceback (multi-line, indented) escapes cleanly."""

    tb = (
        "Traceback (most recent call last):\n"
        '  File "x.py", line 1, in <module>\n'
        '    raise RuntimeError("boom")\n'
        "RuntimeError: boom\n"
    )
    elog.event_trace("trace_exception", traceback=tb)
    text = log_path.read_text(encoding="utf-8")
    assert text.count("\n") == 1  # one log line, plus its trailing newline
    assert "Traceback" in text
    assert "RuntimeError: boom" in text


# ---------- since_last_fire_ms sidecar ----------


def test_first_fire_returns_none(elog: FileEventLog) -> None:
    assert elog.record_fire_delta("stop") is None


def test_subsequent_fire_returns_non_negative_ms(elog: FileEventLog) -> None:
    elog.record_fire_delta("stop")
    delta = elog.record_fire_delta("stop")
    assert isinstance(delta, int)
    assert delta >= 0


def test_fire_delta_event_types_are_independent(elog: FileEventLog) -> None:
    """A second event type is on its own timer."""

    elog.record_fire_delta("stop")
    # Different event type — first time we see it, expect None.
    assert elog.record_fire_delta("session_end") is None


def test_fire_delta_sidecar_lives_next_to_log(elog: FileEventLog, log_path: Path) -> None:
    elog.record_fire_delta("stop")
    sidecar = log_path.parent / ".last_fire_stop"
    assert sidecar.exists()
    # File contents are the epoch-ms integer the next call diffs against.
    assert sidecar.read_text(encoding="utf-8").strip().isdigit()


def test_fire_delta_io_failure_returns_none() -> None:
    """Sidecar I/O errors don't bubble — same best-effort stance as the log."""

    bad = Path("/dev/null/this-is-not-a-dir") / "audio-recap.log"
    # Must not raise even when the sidecar's parent directory can't be created.
    assert FileEventLog(bad).record_fire_delta("stop") is None
