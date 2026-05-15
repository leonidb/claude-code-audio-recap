from __future__ import annotations

import re
from pathlib import Path

import pytest

from audio_recap.eventlog import FileEventLog
from tests.fakes import FakeProcessRunner, raw_payload, real_services

# An ISO-8601 timestamp prefix the log emits for every line. The ``log_path``
# fixture (conftest) is a tmp file path; ``real_services`` wires the
# production graph's event log to that same path so the integration
# tests below can read it back.

ISO8601_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\b")


@pytest.fixture
def elog(log_path: Path) -> FileEventLog:
    """A FileEventLog writing to the test's tmp log file."""

    return FileEventLog(log_path)


def test_event_creates_file_on_first_write(elog: FileEventLog, log_path: Path) -> None:
    assert not log_path.exists()
    elog.event("stop", session_id="abc")
    assert log_path.exists()
    assert log_path.read_text(encoding="utf-8").strip() != ""


def test_event_writes_iso_timestamp_and_level(elog: FileEventLog, log_path: Path) -> None:
    elog.event("stop", session_id="abc")
    line = log_path.read_text(encoding="utf-8").strip().splitlines()[0]
    assert ISO8601_RE.match(line), line
    assert " INFO " in line


def test_event_serializes_event_and_fields(elog: FileEventLog, log_path: Path) -> None:
    elog.event("stop", session_id="abc-123", state="enabled", word_count=42)
    line = log_path.read_text(encoding="utf-8").strip().splitlines()[0]
    assert "event=stop" in line
    assert "session_id=abc-123" in line
    assert "state=enabled" in line
    assert "word_count=42" in line


def test_event_serializes_booleans(elog: FileEventLog, log_path: Path) -> None:
    elog.event("stop", say_started=True, say_done=False)
    line = log_path.read_text(encoding="utf-8").strip().splitlines()[0]
    assert "say_started=true" in line
    assert "say_done=false" in line


def test_event_quotes_values_with_spaces(elog: FileEventLog, log_path: Path) -> None:
    elog.event("stop", error="claude -p timed out")
    line = log_path.read_text(encoding="utf-8").strip().splitlines()[0]
    assert "error='claude -p timed out'" in line


def test_event_escapes_embedded_single_quotes(elog: FileEventLog, log_path: Path) -> None:
    elog.event("stop", note="it's broken")
    line = log_path.read_text(encoding="utf-8").strip().splitlines()[0]
    # POSIX shell-style escape: `'it'\''s broken'`.
    assert "note='it'\\''s broken'" in line


def test_event_appends_in_order(elog: FileEventLog, log_path: Path) -> None:
    for i in range(5):
        elog.event("stop", n=i)
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 5
    for i, line in enumerate(lines):
        assert f"n={i}" in line


def test_event_appends_across_calls_does_not_truncate(elog: FileEventLog, log_path: Path) -> None:
    """A second call must not overwrite the file."""

    elog.event("stop", session_id="A")
    elog.event("stop", session_id="B")
    text = log_path.read_text(encoding="utf-8")
    assert "session_id=A" in text
    assert "session_id=B" in text


def test_message_writes_freeform_line(elog: FileEventLog, log_path: Path) -> None:
    elog.message("[audio-recap] something happened")
    line = log_path.read_text(encoding="utf-8").strip().splitlines()[0]
    assert ISO8601_RE.match(line), line
    assert " INFO [audio-recap] something happened" in line


def test_io_error_is_silent() -> None:
    """Logger swallows write failures so it never breaks the hook."""

    # Point the log at a path that can't be created (parent is a file).
    bad = Path("/dev/null/this-is-not-a-dir") / "audio-recap.log"
    elog = FileEventLog(bad)
    # Must not raise.
    elog.event("stop", session_id="abc")
    elog.message("free form")


def test_value_with_equals_sign_is_quoted(elog: FileEventLog, log_path: Path) -> None:
    elog.event("stop", note="key=val=tail")
    line = log_path.read_text(encoding="utf-8").strip().splitlines()[0]
    assert "note='key=val=tail'" in line


def test_empty_string_value_is_emitted_as_quoted_empty(elog: FileEventLog, log_path: Path) -> None:
    elog.event("stop", note="")
    line = log_path.read_text(encoding="utf-8").strip().splitlines()[0]
    assert "note=''" in line


# ---------- integration: hook + command emit the expected events ----------


def test_hook_emits_session_id_and_state_events(log_path: Path, tmp_path: Path) -> None:
    """One Stop fire ends up with at least the ``fired`` and ``state`` events."""

    from audio_recap import hook

    payload = {
        "session_id": "log-test-sid",
        "cwd": "/proj",
        "hook_event_name": "Stop",
        "last_assistant_message": "Hi.",
    }
    # No state file seeded → the hook reads default-off and exits 0
    # before any subprocess call, so an empty FakeProcessRunner is fine.
    services = real_services("/proj", tmp_path, runner=FakeProcessRunner())

    assert hook.main(raw_payload(payload), services=services) == 0
    text = log_path.read_text(encoding="utf-8")
    assert "event=stop" in text
    assert "session_id=log-test-sid" in text
    assert "state=disabled" in text


def test_command_emits_action_and_verb(log_path: Path, tmp_path: Path) -> None:
    from audio_recap import command

    services = real_services("/proj", tmp_path, runner=FakeProcessRunner())
    rc = command.main(
        ["command.py", "--session-id", "cmd-test-sid", "--cwd", "/proj", "on"],
        services=services,
    )
    assert rc == 0

    text = log_path.read_text(encoding="utf-8")
    assert "event=command" in text
    assert "action=audio_recap" in text
    assert "verb=on" in text
    assert "result=enabled" in text
    assert "session_id=cmd-test-sid" in text
