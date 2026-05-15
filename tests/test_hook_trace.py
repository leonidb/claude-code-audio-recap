"""TRACE-level emission for the Stop hook end-to-end.

Exercises the wiring between :mod:`audio_recap.hook` and
:mod:`audio_recap.eventlog` — the ``log_level`` config gate, the
payload trace, the prompt+response trace from the recap and summarizer
backends, and the exception-traceback trace on failure paths. The
file-shape and escape-rules are covered by ``test_eventlog_trace.py``;
this module verifies that the hook actually emits the events it's
supposed to.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from audio_recap import hook
from audio_recap.services import Services
from tests.fakes import (
    FakeProcessRunner,
    audio_handlers,
    raw_payload,
    real_services,
    seed_enabled,
)


def _stub_runner(claude_stdout: str = "Edited stuff.") -> FakeProcessRunner:
    def claude(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=claude_stdout, stderr="")

    return FakeProcessRunner({**audio_handlers(), "claude": claude})


def _services(cwd: str, tmp_path: Path, runner: FakeProcessRunner) -> Services:
    """Production graph wired to ``runner`` + tmp roots, for ``cwd``."""

    return real_services(cwd, tmp_path, runner=runner)


def _write_trace_config(cwd: Path) -> None:
    """Drop a per-cwd config that turns the event log up to TRACE.

    TRACE is opt-in via ``Config.log_level``; the hook reads
    ``<cwd>/.audio-recap/config.json`` when it builds the event log.
    """

    cfg = cwd / ".audio-recap" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"log_level": "trace"}), encoding="utf-8")


def _enabled_payload(cwd: str = "/proj") -> dict[str, Any]:
    """Minimal mixed turn — one tool use plus a short message."""

    return {
        "session_id": "trace-sid",
        "cwd": cwd,
        "hook_event_name": "Stop",
        "transcript": [
            {"role": "user", "content": "do the thing"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/tmp/x"},
                    },
                    {
                        "type": "text",
                        "text": (
                            "I edited the file and confirmed the change "
                            "renders correctly in the test fixture; the "
                            "change touched the import block, the public "
                            "API surface, and one inline comment that "
                            "described the prior behavior in detail, so "
                            "you can review the diff at your convenience "
                            "before the next release goes out the door."
                        ),
                    },
                ],
            },
        ],
    }


# ---------- gate behavior ----------


def test_trace_off_by_default_omits_payload_and_prompts(tmp_path: Path, log_path: Path) -> None:
    """No ``log_level`` config → INFO default → TRACE lines are silenced."""

    payload = _enabled_payload()
    seed_enabled(tmp_path, payload)
    services = _services("/proj", tmp_path, _stub_runner())

    assert hook.main(raw_payload(payload), services=services) == 0
    text = log_path.read_text(encoding="utf-8")
    assert " TRACE " not in text
    assert "trace_payload" not in text
    assert "trace_recap_prompt" not in text


def test_trace_on_emits_payload_and_recap_io(tmp_path: Path, log_path: Path) -> None:
    """``log_level: trace`` config → the payload + recap I/O are logged."""

    _write_trace_config(tmp_path)
    payload = _enabled_payload(str(tmp_path))
    seed_enabled(tmp_path, payload)
    services = _services(str(tmp_path), tmp_path, _stub_runner(claude_stdout="Edited a file.\n"))

    assert hook.main(raw_payload(payload), services=services) == 0
    text = log_path.read_text(encoding="utf-8")
    assert " TRACE " in text
    # Payload trace carries the full JSON.
    assert "event=trace_payload" in text
    assert "session_id=trace-sid" in text
    # Recap backend trace lines.
    assert "event=trace_recap_prompt" in text
    assert "backend=claude_p" in text
    assert "event=trace_recap_response" in text
    # The verbatim claude_p stdout passes through to the response trace.
    assert "Edited a file." in text


def test_trace_on_emits_segment_pre_post_transform(tmp_path: Path, log_path: Path) -> None:
    """TRACE on: pre/post-transform text is logged for each segment."""

    _write_trace_config(tmp_path)
    payload = _enabled_payload(str(tmp_path))
    seed_enabled(tmp_path, payload)
    services = _services(str(tmp_path), tmp_path, _stub_runner())

    assert hook.main(raw_payload(payload), services=services) == 0
    text = log_path.read_text(encoding="utf-8")
    assert "event=trace_segment" in text
    assert "segment=recap" in text
    assert "segment=message_first_pass" in text
    assert "segment=message_final" in text


def test_trace_on_logs_traceback_when_recap_subprocess_fails(
    tmp_path: Path, log_path: Path
) -> None:
    """A claude -p timeout is followed by a TRACE traceback line."""

    _write_trace_config(tmp_path)

    def claude(argv: list[str], **_: Any) -> BaseException:
        return subprocess.TimeoutExpired(cmd="claude", timeout=15.0)

    runner = FakeProcessRunner({**audio_handlers(), "claude": claude})
    payload = _enabled_payload(str(tmp_path))
    seed_enabled(tmp_path, payload)
    services = _services(str(tmp_path), tmp_path, runner)

    assert hook.main(raw_payload(payload), services=services) == 0
    text = log_path.read_text(encoding="utf-8")
    assert "event=trace_recap_failed" in text
    assert "error_type=TimeoutExpired" in text
    # Traceback string survives the multi-line escape and lands on the
    # same single log line as the rest of the trace fields.
    assert "Traceback" in text
    # The hook also surfaces a higher-level trace_exception line at the
    # site=recap_claude_p catching point.
    assert "event=trace_exception" in text
    assert "site=recap_claude_p" in text


# ---------- since_last_fire_ms ----------


def test_first_fire_omits_since_last_fire_ms(tmp_path: Path, log_path: Path) -> None:
    """The very first fire on a fresh log directory has no prior to diff against."""

    payload = _enabled_payload()
    seed_enabled(tmp_path, payload)
    services = _services("/proj", tmp_path, _stub_runner())

    assert hook.main(raw_payload(payload), services=services) == 0
    text = log_path.read_text(encoding="utf-8")
    # No since_last_fire_ms field on the fired=true line on first fire.
    fired_lines = [ln for ln in text.splitlines() if " fired=true" in ln or "fired=true " in ln]
    assert fired_lines, text
    for ln in fired_lines:
        assert "since_last_fire_ms" not in ln


def test_subsequent_fire_includes_non_negative_since_last_fire_ms(
    tmp_path: Path, log_path: Path
) -> None:
    """A second fire diffs against the first and reports a non-negative ms gap."""

    payload = _enabled_payload()
    seed_enabled(tmp_path, payload)
    services = _services("/proj", tmp_path, _stub_runner())

    assert hook.main(raw_payload(payload), services=services) == 0
    assert hook.main(raw_payload(payload), services=services) == 0

    text = log_path.read_text(encoding="utf-8")
    fired_lines = [ln for ln in text.splitlines() if "fired=true" in ln]
    # First fire: no since_last_fire_ms; second fire: yes.
    assert len(fired_lines) == 2
    assert "since_last_fire_ms" not in fired_lines[0]
    assert "since_last_fire_ms=" in fired_lines[1]
    # Extract and check the int value is >= 0.
    token = next(t for t in fired_lines[1].split() if t.startswith("since_last_fire_ms="))
    value = int(token.split("=", 1)[1])
    assert value >= 0
