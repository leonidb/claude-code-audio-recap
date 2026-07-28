from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from audio_recap import hook
from audio_recap.cache import FileNarrationCache
from audio_recap.services import Services
from audio_recap.state import FileStateStore, State
from tests.fakes import (
    FakeProcessRunner,
    audio_handlers,
    cache_root,
    completed,
    raw_payload,
    real_services,
    seed_enabled,
    state_root,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ---------- payload + Services helpers ----------


def _raw_fixture(name: str) -> tuple[bytes, dict[str, Any]]:
    """Load a committed CC-shape Stop payload as (raw bytes, parsed dict)."""

    raw = (FIXTURES / name).read_bytes()
    return raw, json.loads(raw)


def _runner_with_default_handlers(calls: dict[str, list[Any]]) -> FakeProcessRunner:
    """FakeProcessRunner whose handlers stuff each captured argv into ``calls``.

    Lets test bodies assert directly off ``calls["say"][0][-1]`` etc.
    without patching ``subprocess.run`` globally.
    """

    def claude_handler(
        argv: list[str], *, input: str | None, timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        calls["claude_p"].append({"argv": argv, "input": input})
        return completed(argv, stdout="Edited some files and ran tests.\n")

    def say_handler(
        argv: list[str], *, input: str | None, timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        calls["say"].append(argv)
        return completed(argv)

    def afinfo_handler(
        argv: list[str], *, input: str | None, timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        calls["afinfo"].append(argv)
        return completed(argv, stdout="estimated duration: 999.000 sec")

    def afplay_handler(
        argv: list[str], *, input: str | None, timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        calls["afplay"].append(argv)
        return completed(argv)

    return FakeProcessRunner(
        {
            "claude": claude_handler,
            "say": say_handler,
            "afinfo": afinfo_handler,
            "afplay": afplay_handler,
        }
    )


class _CountingRecap:
    """Wraps a Recap impl to record each ``generate`` call into a shared list.

    Lets the fallback-fired assertion work without patching the class —
    the test reads ``calls["rule_based"]`` off the shared dict.
    """

    def __init__(self, inner: Any, calls: list[Any]) -> None:
        self._inner = inner
        self._calls = calls

    def generate(self, turn: Any) -> str:
        self._calls.append(turn)
        return self._inner.generate(turn)


def _mock_services(cwd: str, tmp_path: Path) -> tuple[Services, dict[str, list[Any]]]:
    """Real component graph + a FakeProcessRunner with default-success handlers.

    Returns ``(services, calls)``. ``calls`` collects per-binary argv
    (claude / say / afinfo / afplay) plus the rule-based fallback's
    invocations, so test bodies assert directly off it. The real
    :meth:`Services.from_config` runs (per-cwd config overrides under
    ``cwd`` apply), but no subprocess is spawned and every file-backed
    service is rooted under ``tmp_path``.
    """

    calls: dict[str, list[Any]] = {
        "claude_p": [],
        "rule_based": [],
        "say": [],
        "afinfo": [],
        "afplay": [],
    }
    services = real_services(cwd, tmp_path, runner=_runner_with_default_handlers(calls))
    services.recap_fallback = _CountingRecap(services.recap_fallback, calls["rule_based"])
    return services, calls


# ---------- cases ----------


def test_default_state_skips_narration(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # No state file saved → default off → nothing speaks.
    raw, payload = _raw_fixture("stop_payload.json")
    services, calls = _mock_services(payload["cwd"], tmp_path)
    assert hook.main(raw, services=services) == 0
    assert calls["claude_p"] == []
    assert calls["rule_based"] == []
    assert calls["say"] == []
    assert "audio recap disabled" in capsys.readouterr().err


def test_enabled_state_narrates_recap_then_message(tmp_path: Path) -> None:
    raw, payload = _raw_fixture("stop_payload.json")
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services(payload["cwd"], tmp_path)

    assert hook.main(raw, services=services) == 0

    # Exactly one claude -p call, zero rule_based calls (primary succeeded).
    assert len(calls["claude_p"]) == 1
    assert calls["rule_based"] == []

    # Two say calls: recap first, then message.
    assert len(calls["say"]) == 2
    assert calls["say"][0][-1] == "Edited some files and ran tests."
    assert "fixed the failing test" in calls["say"][1][-1]


def test_explicitly_disabled_state_exits_immediately(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw, payload = _raw_fixture("stop_payload.json")
    # Save explicit disabled state for the session_id on the payload.
    sid = payload.get("session_id") or "_global"
    cwd = payload.get("cwd") or ""
    FileStateStore(state_root(tmp_path)).save(State(enabled=False), sid, cwd)
    services, calls = _mock_services(payload["cwd"], tmp_path)

    assert hook.main(raw, services=services) == 0
    assert calls["claude_p"] == []
    assert calls["rule_based"] == []
    assert calls["say"] == []
    assert "audio recap disabled" in capsys.readouterr().err


def test_enabled_under_one_cwd_is_seen_when_hook_fires_under_another(tmp_path: Path) -> None:
    """End-to-end regression for the silent-turn bug.

    ``/audio-recap:on`` ran under one cwd; the Stop hook later fires under
    a drifted cwd (the payload's). With the old per-cwd keying the state
    file was written under one directory and missed under the other →
    silent turn. Session-only keying must find it: the hook reads the
    enabled flag and narrates despite the cwd mismatch.
    """

    raw, payload = _raw_fixture("stop_payload.json")
    sid = payload.get("session_id") or "_global"
    # State written under a cwd that is NOT the one the Stop hook fires under.
    drifted_cwd = payload["cwd"] + "/subdir/the/agent/cd-ed/into"
    assert drifted_cwd != payload["cwd"]
    FileStateStore(state_root(tmp_path)).save(State(enabled=True), sid, drifted_cwd)

    services, calls = _mock_services(payload["cwd"], tmp_path)
    assert hook.main(raw, services=services) == 0

    # Narration ran → the enabled flag was found despite the cwd drift.
    assert calls["say"] != []


def test_skip_if_no_tool_use_fires_on_qa_fixture(tmp_path: Path) -> None:
    raw, payload = _raw_fixture("stop_payload_qa.json")
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services(payload["cwd"], tmp_path)

    assert hook.main(raw, services=services) == 0
    # No recap generated: Q&A has zero tool-use blocks.
    assert calls["claude_p"] == []
    assert calls["rule_based"] == []
    # One say call for the message only.
    assert len(calls["say"]) == 1
    assert "frozenset" in calls["say"][0][-1]


def test_skip_if_message_words_lt_fires_on_short_message(tmp_path: Path) -> None:
    # Mixed turn with tool use (so skip_if_no_tool_use does NOT fire)
    # but a very short user-facing message (< 30 words).
    payload: dict[str, Any] = {
        "session_id": "s",
        "cwd": "/proj",
        "transcript": [
            {"role": "user", "content": "fix it"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x"},
                    },
                    {"type": "text", "text": "Done."},
                ],
            },
        ],
    }
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services("/proj", tmp_path)

    assert hook.main(raw_payload(payload), services=services) == 0
    assert calls["claude_p"] == []
    assert calls["rule_based"] == []
    assert len(calls["say"]) == 1
    assert calls["say"][0][-1] == "Done."


def test_claude_p_failure_triggers_rule_based_fallback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    said: list[list[str]] = []

    def claude(argv: list[str], **_: Any) -> BaseException:
        return subprocess.TimeoutExpired(cmd="claude", timeout=15.0)

    def say(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        said.append(argv)
        return completed(argv)

    runner = FakeProcessRunner({**audio_handlers(), "claude": claude, "say": say})

    raw, payload = _raw_fixture("stop_payload.json")
    seed_enabled(tmp_path, payload)
    services = real_services(payload["cwd"], tmp_path, runner=runner)

    assert hook.main(raw, services=services) == 0
    # First say is the rule-based recap; second is the message.
    assert len(said) == 2
    assert said[0][-1] == "Read a file, edited a file, ran `pytest`."
    captured = capsys.readouterr()
    assert "claude -p recap failed" in captured.err
    assert "rule-based fallback" in captured.err


def test_tts_failure_exits_1_with_stderr_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def claude(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return completed(argv, stdout="A recap.\n")

    def say(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return completed(argv, returncode=1, stderr="bad voice")

    runner = FakeProcessRunner({**audio_handlers(), "claude": claude, "say": say})

    raw, payload = _raw_fixture("stop_payload.json")
    seed_enabled(tmp_path, payload)
    services = real_services(payload["cwd"], tmp_path, runner=runner)

    assert hook.main(raw, services=services) == 1
    assert "TTS failed" in capsys.readouterr().err


def test_pure_tool_use_turn_speaks_only_recap(tmp_path: Path) -> None:
    payload: dict[str, Any] = {
        "session_id": "s",
        "cwd": "/proj",
        "transcript": [
            {"role": "user", "content": "do it"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x"},
                    },
                ],
            },
        ],
    }
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services("/proj", tmp_path)

    assert hook.main(raw_payload(payload), services=services) == 0
    # One say for the recap; message is empty so no second say.
    assert len(calls["say"]) == 1
    assert calls["say"][0][-1] == "Edited some files and ran tests."


def test_invalid_json_on_stdin_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    services, calls = _mock_services("/proj", tmp_path)
    assert hook.main(b"{not json", services=services) == 1
    assert calls["say"] == []
    assert "invalid hook JSON" in capsys.readouterr().err


def test_non_object_payload_exits_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    services, _ = _mock_services("/proj", tmp_path)
    assert hook.main(b"[1, 2, 3]", services=services) == 1
    assert "not a JSON object" in capsys.readouterr().err


def test_hook_main_is_callable() -> None:
    assert callable(hook.main)


# ---------- last_assistant_message + transcript_path (real CC payload) -----


def test_last_assistant_message_field_is_used_when_present(tmp_path: Path) -> None:
    # Real CC Stop payload exposes the reply directly. With no inline
    # transcript and no transcript_path, the recap is skipped (no tool
    # uses to summarize) but the message still narrates.
    payload: dict[str, Any] = {
        "session_id": "s",
        "cwd": "/proj",
        "hook_event_name": "Stop",
        "last_assistant_message": "A short direct reply.",
    }
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services("/proj", tmp_path)
    assert hook.main(raw_payload(payload), services=services) == 0
    assert calls["claude_p"] == []
    assert len(calls["say"]) == 1
    assert calls["say"][0][-1] == "A short direct reply."


def test_transcript_path_synthesizes_inline_transcript(tmp_path: Path) -> None:
    # 35-word reply: above the recap-skip threshold (30 words).
    long_message = " ".join(["w"] * 35)
    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {"type": "user", "message": {"role": "user", "content": "do it"}},
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "Edit",
                                "input": {"file_path": "/x"},
                            },
                            {"type": "text", "text": long_message},
                        ],
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    payload: dict[str, Any] = {
        "session_id": "s",
        "cwd": "/proj",
        "hook_event_name": "Stop",
        "transcript_path": str(jsonl),
        "last_assistant_message": long_message,
    }
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services("/proj", tmp_path)

    assert hook.main(raw_payload(payload), services=services) == 0
    # Recap fired (tool use synthesized from JSONL), message spoken verbatim.
    assert len(calls["claude_p"]) == 1
    assert len(calls["say"]) == 2
    assert calls["say"][0][-1] == "Edited some files and ran tests."
    assert calls["say"][1][-1] == long_message


def test_transcript_with_invalid_utf8_degrades_instead_of_crashing(tmp_path: Path) -> None:
    """A bad byte in the JSONL must not take the Stop hook down.

    ``UnicodeDecodeError`` is a ``ValueError`` — neither an ``OSError`` nor a
    ``JSONDecodeError`` — so it used to escape the parser as an unhandled
    crash. The transcript race (a partially-flushed multi-byte sequence) is a
    real way to hit it. Degrade to message-only narration instead.
    """
    jsonl = tmp_path / "bad.jsonl"
    jsonl.write_bytes(b'{"type":"user","message":{"role":"user","content":"hi \xff\xfe"}}\n')
    payload: dict[str, Any] = {
        "session_id": "s",
        "cwd": "/proj",
        "transcript_path": str(jsonl),
        "last_assistant_message": "Here is the reply.",
    }
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services("/proj", tmp_path)

    assert hook.main(raw_payload(payload), services=services) == 0
    # Fell back to the last_assistant_message field — still narrated.
    assert calls["say"][-1][-1] == "Here is the reply."


def test_real_payload_fixture_narrates_summarized_message_no_recap(tmp_path: Path) -> None:
    # The committed real-shape fixture carries a `transcript_path` that
    # doesn't exist on this machine — the JSONL synthesis returns None,
    # so no tool uses are seen and the recap is skipped. The reply is
    # ~80 words after the code-block transform, over the 50-word
    # summarize threshold, so the summarizer fires (mocked stdout below)
    # and the summary is spoken instead of the verbatim message.
    raw, payload = _raw_fixture("stop_payload_real.json")
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services(payload["cwd"], tmp_path)
    assert hook.main(raw, services=services) == 0

    assert calls["rule_based"] == []
    # Exactly one claude -p call: the summarizer. No recap (no tool uses).
    assert len(calls["claude_p"]) == 1
    summarizer_input = calls["claude_p"][0]["input"]
    assert "Your reply, to be rewritten:" in summarizer_input
    assert "frozenset" in summarizer_input
    # Code block already collapsed by speakable transforms before
    # the summarizer saw the text.
    assert "Code block:" in summarizer_input

    # One say call with the (mocked) summary stdout.
    assert len(calls["say"]) == 1
    assert calls["say"][0][-1] == "Edited some files and ran tests."


def test_unreadable_transcript_path_falls_back_to_message_only(tmp_path: Path) -> None:
    payload: dict[str, Any] = {
        "session_id": "s",
        "cwd": "/proj",
        "hook_event_name": "Stop",
        "transcript_path": "/nonexistent/path/should/never/exist.jsonl",
        "last_assistant_message": "Reply text.",
    }
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services("/proj", tmp_path)
    assert hook.main(raw_payload(payload), services=services) == 0
    # No tool uses → recap skipped; message still narrates.
    assert calls["claude_p"] == []
    assert len(calls["say"]) == 1
    assert calls["say"][0][-1] == "Reply text."


# ---------- multi-text-block turn aggregation ---------------------------


def test_multi_text_block_turn_narrates_all_text_blocks_in_order(tmp_path: Path) -> None:
    # Agent-style turn shape: [thinking, text, tool_use, tool_use,
    # text]. The pre-fix code spoke only the final text block
    # ("Standing by.") and dropped
    # the substantive review entirely. Combined word count chosen
    # to land between the recap-skip-if-message-words-lt floor (30)
    # and the summarize-message-over-words ceiling (50) so the test
    # asserts on the verbatim merged text without the summarizer
    # rewriting it.
    review = " ".join(["alpha"] * 30)
    confirmation = " ".join(["omega"] * 10)
    payload: dict[str, Any] = {
        "session_id": "s",
        "cwd": "/proj",
        "hook_event_name": "Stop",
        "transcript": [
            {"role": "user", "content": "review the plan"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "weighing tradeoffs", "signature": "x"},
                    {"type": "text", "text": review},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "send",
                        "input": {"to": "builder"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "send",
                        "input": {"to": "user"},
                    },
                    {"type": "text", "text": confirmation},
                ],
            },
        ],
        # Lossy CC convenience field — should NOT win over the
        # transcript when both are present.
        "last_assistant_message": confirmation,
    }
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services("/proj", tmp_path)
    assert hook.main(raw_payload(payload), services=services) == 0

    # Recap fires (two tool uses) + message. Two say calls.
    assert len(calls["say"]) == 2
    spoken_message = calls["say"][1][-1]
    # Both text blocks present, in original order, joined with the
    # paragraph separator.
    assert review in spoken_message
    assert confirmation in spoken_message
    assert spoken_message.index(review) < spoken_message.index(confirmation)
    # Thinking block content must not leak into audio.
    assert "weighing tradeoffs" not in spoken_message


def test_inline_transcript_wins_over_last_assistant_message(tmp_path: Path) -> None:
    # When both an inline transcript and `last_assistant_message`
    # are present, the transcript is authoritative — it has every
    # text block; `last_assistant_message` only carries the final
    # one. Lock this priority so the 053 fix doesn't silently
    # regress to CC's lossy field.
    first_half = " ".join(["alpha"] * 35)  # > 30-word recap-skip floor
    final_half = "Final paragraph after the tool call."
    payload: dict[str, Any] = {
        "session_id": "s",
        "cwd": "/proj",
        "hook_event_name": "Stop",
        "transcript": [
            {"role": "user", "content": "x"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": first_half},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x"},
                    },
                    {"type": "text", "text": final_half},
                ],
            },
        ],
        "last_assistant_message": final_half,
    }
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services("/proj", tmp_path)
    assert hook.main(raw_payload(payload), services=services) == 0

    # Recap (one tool_use, message above threshold) + message → two say calls.
    assert len(calls["say"]) == 2
    spoken_message = calls["say"][1][-1]
    # First-half block survived the extraction, not just the
    # `last_assistant_message` final block.
    assert first_half in spoken_message
    assert final_half in spoken_message


# ---------- per-session keying ----------


def test_two_sessions_in_same_project_have_independent_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Session A is enabled; session B is not. B's hook fires, B stays silent."""

    FileStateStore(state_root(tmp_path)).save(State(enabled=True), "session-A", "/proj")
    # Session B has no state file → default off.
    payload_b: dict[str, Any] = {
        "session_id": "session-B",
        "cwd": "/proj",
        "hook_event_name": "Stop",
        "last_assistant_message": "Reply for B.",
    }
    services, calls = _mock_services("/proj", tmp_path)
    assert hook.main(raw_payload(payload_b), services=services) == 0
    # B saw default off, narrated nothing.
    assert calls["say"] == []
    assert "audio recap disabled" in capsys.readouterr().err


# ---------- summarize-long-message ----------


_LONG_MESSAGE = " ".join(["lorem"] * 80)  # 80 words, well over the 50 default.


def test_long_message_is_summarized_via_claude_p(tmp_path: Path) -> None:
    # Distinct stdouts for recap (tool-uses prompt) and summarizer (message
    # prompt) so we can assert the routing without inspecting argv.
    said: list[list[str]] = []
    claude_calls: list[str] = []

    def claude(
        argv: list[str], *, input: str | None = None, **_: Any
    ) -> subprocess.CompletedProcess[str]:
        prompt = input or ""
        claude_calls.append(prompt)
        if "Tool uses:" in prompt:
            return completed(argv, stdout="Edited a file.\n")
        if "Your reply, to be rewritten:" in prompt:
            return completed(argv, stdout="Two-sentence summary. Of the long reply.\n")
        raise AssertionError(f"unexpected claude prompt: {prompt!r}")

    def say(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        said.append(argv)
        return completed(argv)

    runner = FakeProcessRunner({**audio_handlers(), "claude": claude, "say": say})

    payload: dict[str, Any] = {
        "session_id": "s",
        "cwd": "/proj",
        "transcript": [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x"},
                    },
                    {"type": "text", "text": _LONG_MESSAGE},
                ],
            },
        ],
    }
    seed_enabled(tmp_path, payload)
    services = real_services("/proj", tmp_path, runner=runner)
    assert hook.main(raw_payload(payload), services=services) == 0

    # Two claude -p calls: recap + summarizer.
    assert len(claude_calls) == 2
    assert any("Tool uses:" in p for p in claude_calls)
    assert any("Your reply, to be rewritten:" in p for p in claude_calls)

    # Two say calls: recap + summary (not verbatim long text).
    assert len(said) == 2
    assert said[0][-1] == "Edited a file."
    assert said[1][-1] == "Two-sentence summary. Of the long reply."


def test_summarizer_failure_falls_back_to_verbatim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    said: list[list[str]] = []

    def claude(
        argv: list[str], *, input: str | None = None, **_: Any
    ) -> subprocess.CompletedProcess[str] | BaseException:
        prompt = input or ""
        if "Tool uses:" in prompt:
            return completed(argv, stdout="Edited a file.\n")
        # Summarizer call → fail.
        return subprocess.TimeoutExpired(cmd="claude", timeout=15.0)

    def say(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        said.append(argv)
        return completed(argv)

    runner = FakeProcessRunner({**audio_handlers(), "claude": claude, "say": say})

    payload: dict[str, Any] = {
        "session_id": "s",
        "cwd": "/proj",
        "transcript": [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x"},
                    },
                    {"type": "text", "text": _LONG_MESSAGE},
                ],
            },
        ],
    }
    seed_enabled(tmp_path, payload)
    services = real_services("/proj", tmp_path, runner=runner)
    assert hook.main(raw_payload(payload), services=services) == 0

    # Two say calls: recap + verbatim long message (NOT summary).
    assert len(said) == 2
    assert said[0][-1] == "Edited a file."
    assert said[1][-1] == _LONG_MESSAGE
    assert "summarizer failed" in capsys.readouterr().err


def test_recap_and_summary_run_concurrently(tmp_path: Path) -> None:
    # Both `claude -p` calls block on a 2-party barrier; if the hook ran
    # them sequentially the second call would never start, the barrier
    # would never release, and the test would deadlock until the timeout
    # fires it. The barrier's own `timeout=2.0` catches that case.
    barrier = threading.Barrier(2, timeout=2.0)
    said: list[list[str]] = []

    def claude(
        argv: list[str], *, input: str | None = None, **_: Any
    ) -> subprocess.CompletedProcess[str]:
        prompt = input or ""
        barrier.wait()
        stdout = (
            "Edited a file.\n"
            if "Tool uses:" in prompt
            else "Two-sentence summary. Of the long reply.\n"
        )
        return completed(argv, stdout=stdout)

    def say(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        said.append(argv)
        return completed(argv)

    runner = FakeProcessRunner({**audio_handlers(), "claude": claude, "say": say})

    payload: dict[str, Any] = {
        "session_id": "s",
        "cwd": "/proj",
        "transcript": [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x"},
                    },
                    {"type": "text", "text": _LONG_MESSAGE},
                ],
            },
        ],
    }
    seed_enabled(tmp_path, payload)
    services = real_services("/proj", tmp_path, runner=runner)
    assert hook.main(raw_payload(payload), services=services) == 0
    # Both segments spoke — the barrier released, so both subprocess
    # calls were in flight at once.
    assert len(said) == 2


def test_summarizer_failure_truncates_long_verbatim_with_cue(tmp_path: Path) -> None:
    # 250-word reply: above the 150-word verbatim fallback cap. The
    # summarizer fails, the hook should truncate to the cap and append
    # the audible "incomplete" cue rather than blasting the full text.
    long_reply = " ".join(["lorem"] * 250)
    said: list[list[str]] = []

    def claude(
        argv: list[str], *, input: str | None = None, **_: Any
    ) -> subprocess.CompletedProcess[str] | BaseException:
        prompt = input or ""
        if "Tool uses:" in prompt:
            return completed(argv, stdout="Edited a file.\n")
        return subprocess.TimeoutExpired(cmd="claude", timeout=60.0)

    def say(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        said.append(argv)
        return completed(argv)

    runner = FakeProcessRunner({**audio_handlers(), "claude": claude, "say": say})

    payload: dict[str, Any] = {
        "session_id": "s",
        "cwd": "/proj",
        "transcript": [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x"},
                    },
                    {"type": "text", "text": long_reply},
                ],
            },
        ],
    }
    seed_enabled(tmp_path, payload)
    services = real_services("/proj", tmp_path, runner=runner)
    assert hook.main(raw_payload(payload), services=services) == 0

    # Recap + truncated message — two say calls.
    assert len(said) == 2
    spoken_message = said[1][-1]
    spoken_words = spoken_message.split()
    # 150 words of content + the cue ("… and more — full output on screen.")
    # adds 8 more whitespace-separated tokens.
    assert len(spoken_words) == 158
    # First 150 words are the original content, untouched.
    assert spoken_words[:150] == ["lorem"] * 150
    # Cue appears verbatim at the tail.
    assert spoken_message.endswith("… and more — full output on screen.")


def test_summarizer_failure_under_cap_speaks_verbatim_unchanged(tmp_path: Path) -> None:
    # 80 words: above the 50-word summarize threshold but well below the
    # 150-word verbatim fallback cap. Same behaviour as before this
    # commit — speak the full message, no cue appended.
    said: list[list[str]] = []

    def claude(
        argv: list[str], *, input: str | None = None, **_: Any
    ) -> subprocess.CompletedProcess[str] | BaseException:
        prompt = input or ""
        if "Tool uses:" in prompt:
            return completed(argv, stdout="Edited a file.\n")
        return subprocess.TimeoutExpired(cmd="claude", timeout=60.0)

    def say(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        said.append(argv)
        return completed(argv)

    runner = FakeProcessRunner({**audio_handlers(), "claude": claude, "say": say})

    payload: dict[str, Any] = {
        "session_id": "s",
        "cwd": "/proj",
        "transcript": [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x"},
                    },
                    {"type": "text", "text": _LONG_MESSAGE},
                ],
            },
        ],
    }
    seed_enabled(tmp_path, payload)
    services = real_services("/proj", tmp_path, runner=runner)
    assert hook.main(raw_payload(payload), services=services) == 0
    assert len(said) == 2
    assert said[1][-1] == _LONG_MESSAGE
    assert "… and more" not in said[1][-1]


def test_successful_narration_writes_cache(tmp_path: Path) -> None:
    raw, payload = _raw_fixture("stop_payload.json")
    seed_enabled(tmp_path, payload)
    services, _ = _mock_services(payload["cwd"], tmp_path)
    assert hook.main(raw, services=services) == 0

    got = FileNarrationCache(cache_root(tmp_path)).read(payload["session_id"])
    assert got is not None
    cached_recap, cached_message = got
    assert cached_recap == "Edited some files and ran tests."
    assert "fixed the failing test" in cached_message


def test_disabled_state_does_not_write_cache(tmp_path: Path) -> None:
    raw, payload = _raw_fixture("stop_payload.json")
    # No _seed_enabled — narration is off. Hook should exit early without
    # touching the cache.
    services, _ = _mock_services(payload["cwd"], tmp_path)
    assert hook.main(raw, services=services) == 0
    assert FileNarrationCache(cache_root(tmp_path)).read(payload["session_id"]) is None


def test_short_message_is_not_summarized(tmp_path: Path) -> None:
    # 30-word message — below the 50-word threshold. Recap+verbatim only.
    short_message = " ".join(["w"] * 30)
    payload: dict[str, Any] = {
        "session_id": "s",
        "cwd": "/proj",
        "transcript": [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x"},
                    },
                    {"type": "text", "text": short_message},
                ],
            },
        ],
    }
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services("/proj", tmp_path)
    assert hook.main(raw_payload(payload), services=services) == 0
    # Exactly one claude -p call (recap), no summarizer call.
    assert len(calls["claude_p"]) == 1
    assert "Tool uses:" in calls["claude_p"][0]["input"]
    assert len(calls["say"]) == 2
    assert calls["say"][1][-1] == short_message


# ---------- 055: per-cwd dry_run + tts_status + text snippets ----------


def _write_trace_config(cwd: Path) -> None:
    """Drop a per-cwd config that turns the event log up to TRACE.

    Recap/message text lives at TRACE (the production INFO log carries
    only metadata); tests that assert on content text turn TRACE on.
    """

    cfg = cwd / ".audio-recap" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"log_level": "trace"}), encoding="utf-8")


def _write_dry_run_config(cwd: Path) -> None:
    path = cwd / ".audio-recap" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"dry_run": True}), encoding="utf-8")


def test_dry_run_config_skips_say_subprocess(tmp_path: Path, log_path: Path) -> None:
    """``dry_run: true`` short-circuits ``say`` while keeping the pipeline live."""

    cwd = tmp_path / "proj"
    cwd.mkdir()
    _write_dry_run_config(cwd)
    payload: dict[str, Any] = {
        "session_id": "dry-sid",
        "cwd": str(cwd),
        "transcript": [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x"},
                    },
                    {"type": "text", "text": " ".join(["fixed"] * 35)},
                ],
            },
        ],
    }
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services(str(cwd), tmp_path)
    assert hook.main(raw_payload(payload), services=services) == 0

    # Recap pipeline still runs (claude -p called) but say is skipped.
    assert len(calls["claude_p"]) == 1
    assert calls["say"] == []

    text = log_path.read_text(encoding="utf-8")
    assert "tts_status=dry_run" in text
    # No say_done line — say was never called.
    assert "say_done=true" not in text


def test_dry_run_logs_recap_text_and_message_text(tmp_path: Path, log_path: Path) -> None:
    """The per-fire INFO event carries snippets so log-only assessment works."""

    cwd = tmp_path / "proj"
    cwd.mkdir()
    _write_dry_run_config(cwd)
    payload: dict[str, Any] = {
        "session_id": "snip-sid",
        "cwd": str(cwd),
        "transcript": [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x"},
                    },
                    {"type": "text", "text": " ".join(["fixed"] * 35)},
                ],
            },
        ],
    }
    seed_enabled(tmp_path, payload)
    services, _ = _mock_services(str(cwd), tmp_path)
    assert hook.main(raw_payload(payload), services=services) == 0

    text = log_path.read_text(encoding="utf-8")
    # Recap text from the mocked claude -p stdout reaches the INFO line.
    assert "recap_text=" in text
    assert "Edited some files and ran tests" in text
    # Message text snippet (verbatim path, under summarize threshold) lands too.
    assert "message_text=" in text
    assert "fixed fixed" in text


def test_normal_fire_logs_tts_status_spoken(tmp_path: Path, log_path: Path) -> None:
    """Without a config file, the fire emits ``tts_status=spoken``."""

    raw, payload = _raw_fixture("stop_payload.json")
    seed_enabled(tmp_path, payload)
    services, _ = _mock_services(payload["cwd"], tmp_path)
    assert hook.main(raw, services=services) == 0

    text = log_path.read_text(encoding="utf-8")
    assert "tts_status=spoken" in text
    # Old marker is gone.
    assert "say_started=true" not in text


def test_tts_failure_logs_tts_status_failed(tmp_path: Path, log_path: Path) -> None:
    def claude(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return completed(argv, stdout="A recap.\n")

    def say(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return completed(argv, returncode=1, stderr="bad voice")

    runner = FakeProcessRunner({**audio_handlers(), "claude": claude, "say": say})

    raw, payload = _raw_fixture("stop_payload.json")
    seed_enabled(tmp_path, payload)
    services = real_services(payload["cwd"], tmp_path, runner=runner)

    assert hook.main(raw, services=services) == 1
    text = log_path.read_text(encoding="utf-8")
    assert "tts_status=failed" in text


def test_long_message_path_logs_message_text_snippet(tmp_path: Path, log_path: Path) -> None:
    """Summarizer path: the TRACE spoken-segment text reflects the summary, not the verbatim.

    Locks the privacy contract too: on ``tts_status=spoken`` no INFO
    line carries ``recap_text``/``message_text``; the content lives at
    TRACE in ``trace_segment segment=recap_spoken|message_spoken``.
    """

    _write_trace_config(tmp_path)

    def claude(
        argv: list[str], *, input: str | None = None, **_: Any
    ) -> subprocess.CompletedProcess[str]:
        prompt = input or ""
        if "Tool uses:" in prompt:
            return completed(argv, stdout="Edited a file.\n")
        return completed(argv, stdout="A short summary of the long reply.\n")

    runner = FakeProcessRunner({**audio_handlers(), "claude": claude})
    payload: dict[str, Any] = {
        "session_id": "long-sid",
        "cwd": str(tmp_path),
        "transcript": [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x"},
                    },
                    {"type": "text", "text": " ".join(["lorem"] * 80)},
                ],
            },
        ],
    }
    seed_enabled(tmp_path, payload)
    services = real_services(str(tmp_path), tmp_path, runner=runner)
    assert hook.main(raw_payload(payload), services=services) == 0

    text = log_path.read_text(encoding="utf-8")
    # Privacy: spoken INFO carries no content text.
    assert "tts_status=spoken" in text
    for ln in text.splitlines():
        if " INFO " in ln:
            assert "recap_text=" not in ln, ln
            assert "message_text=" not in ln, ln
    # TRACE carries the spoken text — recap from claude_p, message from the summarizer.
    assert "segment=recap_spoken" in text
    assert "Edited a file" in text
    assert "segment=message_spoken" in text
    assert "A short summary of the long reply" in text


def test_recap_text_logged_uncapped(tmp_path: Path, log_path: Path) -> None:
    """071: a long recap round-trips through the TRACE eventlog uncapped.

    Trace-level logging means full understanding — capping at 200
    chars threw away the per-turn quality signal the maintainer uses to
    grade summarizer/recap output (the 070 eval run made this
    bottleneck visible). The recap regex hard cap at 30 words is
    still in force, so the recap itself can't run away; this test
    just locks the eventlog from re-introducing the 200-char
    truncation it used to apply on top.
    """

    _write_trace_config(tmp_path)
    # 30 space-separated single chars (60 chars total) — under the
    # prior 200-char cap, so the regression signal here is "no
    # ellipsis appended on a normal-shaped recap". The long-message
    # test below covers the over-cap case.
    full_recap = " ".join(["A"] * 30)

    def claude(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return completed(argv, stdout=full_recap + "\n")

    runner = FakeProcessRunner({**audio_handlers(), "claude": claude})
    # Mixed turn with a tool use + a message ≥30 words so the recap
    # backend actually runs (skip_if_no_tool_use / message_too_short
    # both clear).
    payload: dict[str, Any] = {
        "session_id": "uncap-recap-sid",
        "cwd": str(tmp_path),
        "transcript": [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x"},
                    },
                    {"type": "text", "text": " ".join(["w"] * 35)},
                ],
            },
        ],
    }
    seed_enabled(tmp_path, payload)
    services = real_services(str(tmp_path), tmp_path, runner=runner)
    assert hook.main(raw_payload(payload), services=services) == 0

    text = log_path.read_text(encoding="utf-8")
    trace_lines = [
        ln for ln in text.splitlines() if " TRACE " in ln and "segment=recap_spoken" in ln
    ]
    assert len(trace_lines) == 1
    line = trace_lines[0]
    # Full recap text appears verbatim in the TRACE line — no ellipsis,
    # no truncation.
    assert full_recap in line
    assert "…" not in line


def test_message_text_logged_uncapped(tmp_path: Path, log_path: Path) -> None:
    """071: a long message text round-trips through the TRACE eventlog uncapped.

    The 200-char cap previously shaved the tail off any reply over
    that length, including the multi-paragraph summarizer output
    the maintainer needs to read in full to grade per-turn quality.
    """

    _write_trace_config(tmp_path)
    # Build a >200-char message (single text block, post-summarizer).
    long_message = " ".join(["lorem"] * 200)  # ~1200 chars
    assert len(long_message) > 200

    def claude(
        argv: list[str], *, input: str | None = None, **_: Any
    ) -> subprocess.CompletedProcess[str]:
        prompt = input or ""
        if "Tool uses:" in prompt:
            return completed(argv, stdout="Edited a file.\n")
        return completed(argv, stdout=long_message + "\n")

    runner = FakeProcessRunner({**audio_handlers(), "claude": claude})
    payload: dict[str, Any] = {
        "session_id": "long-msg-sid",
        "cwd": str(tmp_path),
        "transcript": [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x"},
                    },
                    {"type": "text", "text": " ".join(["w"] * 80)},  # over 50-word threshold
                ],
            },
        ],
    }
    seed_enabled(tmp_path, payload)
    services = real_services(str(tmp_path), tmp_path, runner=runner)
    assert hook.main(raw_payload(payload), services=services) == 0

    text = log_path.read_text(encoding="utf-8")
    trace_lines = [
        ln for ln in text.splitlines() if " TRACE " in ln and "segment=message_spoken" in ln
    ]
    assert len(trace_lines) == 1
    # Whole long_message survives. Single-quoted in the eventlog
    # because it has spaces, but the body stays intact.
    assert long_message in trace_lines[0]
    assert "…" not in trace_lines[0]


def test_dry_run_still_writes_cache(tmp_path: Path) -> None:
    """Dry-run preserves the cache so a later /repeat (with audio) replays."""

    cwd = tmp_path / "proj"
    cwd.mkdir()
    _write_dry_run_config(cwd)
    long_reply = " ".join(["fixed"] * 35)  # > 30-word recap-skip floor
    payload: dict[str, Any] = {
        "session_id": "cache-sid",
        "cwd": str(cwd),
        "transcript": [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x"},
                    },
                    {"type": "text", "text": long_reply},
                ],
            },
        ],
    }
    seed_enabled(tmp_path, payload)
    services, _ = _mock_services(str(cwd), tmp_path)
    assert hook.main(raw_payload(payload), services=services) == 0

    got = FileNarrationCache(cache_root(tmp_path)).read("cache-sid")
    assert got is not None
    cached_recap, cached_message = got
    assert cached_recap == "Edited some files and ran tests."
    assert cached_message == long_reply


def test_malformed_config_falls_back_to_defaults_and_speaks(tmp_path: Path, log_path: Path) -> None:
    """A broken config doesn't break narration — it logs and proceeds."""

    cwd = tmp_path / "proj"
    cwd.mkdir()
    cfg_path = cwd / ".audio-recap" / "config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("{not valid json", encoding="utf-8")

    long_reply = " ".join(["fixed"] * 35)
    payload: dict[str, Any] = {
        "session_id": "bad-cfg-sid",
        "cwd": str(cwd),
        "transcript": [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Edit",
                        "input": {"file_path": "/x"},
                    },
                    {"type": "text", "text": long_reply},
                ],
            },
        ],
    }
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services(str(cwd), tmp_path)
    assert hook.main(raw_payload(payload), services=services) == 0

    # Defaults applied: ``say`` was called (dry_run defaulted to False).
    assert len(calls["say"]) == 2

    text = log_path.read_text(encoding="utf-8")
    assert "event=config" in text
    assert "tts_status=spoken" in text


# ---------- skip Audio Recap's own slash-command turns -------------------


def _slash_command_payload(
    command_tag: str,
    *,
    session_id: str = "slash-sid",
    cwd: str = "/proj",
    assistant_text: str = "Replayed last narration.",
    extra_user_suffix: str = "",
) -> dict[str, Any]:
    """Build a Stop payload with a user message tagged as a slash command.

    CC injects the ``<command-name>/path</command-name>`` prefix on user
    messages whenever a slash command runs. Audio Recap's own
    commands need to be detected and routed past the regular pipeline;
    other slash commands (and untagged messages) flow through unchanged.
    """

    user_content = command_tag + extra_user_suffix
    return {
        "session_id": session_id,
        "cwd": cwd,
        "hook_event_name": "Stop",
        "transcript": [
            {"role": "user", "content": user_content},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_text}],
            },
        ],
    }


def test_audio_recap_repeat_slash_command_short_circuits_pipeline(
    tmp_path: Path, log_path: Path
) -> None:
    """``/audio-recap:repeat`` skips recap, summary, TTS, AND cache write."""

    payload = _slash_command_payload(
        "<command-name>/audio-recap:repeat</command-name>",
        session_id="repeat-sid",
    )
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services("/proj", tmp_path)

    assert hook.main(raw_payload(payload), services=services) == 0
    assert calls["claude_p"] == []
    assert calls["rule_based"] == []
    assert calls["say"] == []
    # Cache untouched — preserves whatever the prior real turn cached.
    assert FileNarrationCache(cache_root(tmp_path)).read("repeat-sid") is None

    text = log_path.read_text(encoding="utf-8")
    assert "recap_path=skipped_slash_command" in text
    assert "summary_path=skipped_slash_command" in text
    assert "tts_status=skipped" in text
    assert "slash_command=repeat" in text


def test_audio_recap_repeat_short_circuit_preserves_existing_cache(tmp_path: Path) -> None:
    """The /repeat short-circuit MUST NOT clobber the prior cache entry.

    Reproduces the second bug from 2026-05-04: the slash-command Stop
    fire used to write its own (empty / meta) recap+message into the
    cache, so the *next* /repeat played the meta-text instead of the
    actual conversation turn.
    """

    FileNarrationCache(cache_root(tmp_path)).write(
        "repeat-sid", "Earlier recap.", "Earlier message that should survive."
    )
    payload = _slash_command_payload(
        "<command-name>/audio-recap:repeat</command-name>",
        session_id="repeat-sid",
    )
    seed_enabled(tmp_path, payload)
    services, _ = _mock_services("/proj", tmp_path)
    assert hook.main(raw_payload(payload), services=services) == 0

    cached = FileNarrationCache(cache_root(tmp_path)).read("repeat-sid")
    assert cached is not None
    cached_recap, cached_message = cached
    assert cached_recap == "Earlier recap."
    assert cached_message == "Earlier message that should survive."


def test_audio_recap_on_slash_command_runs_pipeline_skips_cache(
    tmp_path: Path, log_path: Path
) -> None:
    """``/audio-recap:on`` keeps narrating its confirmation but skips cache."""

    # Pre-existing cache entry that we want preserved.
    FileNarrationCache(cache_root(tmp_path)).write("narrate-sid", "Prior recap.", "Prior message.")

    long_confirm = " ".join(["narration"] * 35)
    payload: dict[str, Any] = {
        "session_id": "narrate-sid",
        "cwd": "/proj",
        "hook_event_name": "Stop",
        "transcript": [
            {
                "role": "user",
                "content": "<command-name>/audio-recap:on</command-name>\n on",
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "narrate on"},
                    },
                    {"type": "text", "text": long_confirm},
                ],
            },
        ],
    }
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services("/proj", tmp_path)
    assert hook.main(raw_payload(payload), services=services) == 0

    # Pipeline ran: recap (one tool use, message ≥30 words) and TTS.
    assert len(calls["claude_p"]) >= 1
    assert len(calls["say"]) == 2

    # Cache write was skipped — prior entry survives.
    cached = FileNarrationCache(cache_root(tmp_path)).read("narrate-sid")
    assert cached is not None
    cached_recap, cached_message = cached
    assert cached_recap == "Prior recap."
    assert cached_message == "Prior message."

    text = log_path.read_text(encoding="utf-8")
    assert "cache_skipped=own_command" in text


def test_other_slash_command_flows_through_normally(tmp_path: Path) -> None:
    """A foreign slash command (e.g. ``/clear``) hits the regular pipeline."""

    payload = _slash_command_payload(
        "<command-name>/clear</command-name>",
        session_id="other-slash-sid",
        assistant_text=" ".join(["cleared"] * 35),
    )
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services("/proj", tmp_path)
    assert hook.main(raw_payload(payload), services=services) == 0

    # Regular pipeline: message ≥30 words but no tool_uses → recap is
    # skipped (skip_if_no_tool_use=True). One say call for the message.
    assert len(calls["say"]) == 1
    # And the cache was written (default behavior).
    assert FileNarrationCache(cache_root(tmp_path)).read("other-slash-sid") is not None


def test_untagged_user_message_flows_through_normally(tmp_path: Path) -> None:
    """No ``<command-name>`` tag → unchanged behavior."""

    payload = _slash_command_payload(
        "",  # no tag
        session_id="plain-sid",
        assistant_text=" ".join(["hello"] * 35),
        extra_user_suffix="just a regular prompt",
    )
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services("/proj", tmp_path)
    assert hook.main(raw_payload(payload), services=services) == 0

    assert len(calls["say"]) == 1
    assert FileNarrationCache(cache_root(tmp_path)).read("plain-sid") is not None


def test_detect_helper_recognises_audio_recap_commands_only() -> None:
    """Unit-level coverage for the slash-command parser."""

    from audio_recap.payload import _detect_own_slash_command

    def _detect(content: str | None) -> str | None:
        # The new helper requires a string; ``None`` was handled by the
        # old shape implicitly. Adapt the test surface here.
        if content is None:
            return None
        return _detect_own_slash_command(content)

    # /audio-recap:repeat → ``repeat`` (short-circuit).
    assert _detect("<command-name>/audio-recap:repeat</command-name>") == "repeat"
    # on / off / status all map to ``confirm`` (run pipeline, skip cache).
    for verb in ("on", "off", "status"):
        assert _detect(f"<command-name>/audio-recap:{verb}</command-name>") == "confirm", (
            f"failed for verb={verb!r}"
        )
    # Tolerant of leading whitespace — observed payloads sometimes
    # include leading newlines from the slash-command body wrapper.
    assert _detect("  \n<command-name>/audio-recap:repeat</command-name>") == "repeat"
    # Other slash commands → None.
    assert _detect("<command-name>/clear</command-name>") is None
    # No tag → None.
    assert _detect("just text") is None
    assert _detect("") is None
    assert _detect(None) is None


def test_default_enabled_config_fires_narration_without_state_file(tmp_path: Path) -> None:
    """``default_enabled: true`` lights up a fresh session with no state file.

    Reproduces the workflow that motivated 069: a worktree wants
    narration auto-on without manually invoking ``/audio-recap:on
    on`` in every new session.
    """

    cwd = tmp_path / "proj"
    cwd.mkdir()
    cfg = cwd / ".audio-recap" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"default_enabled": True}), encoding="utf-8")

    payload: dict[str, Any] = {
        "session_id": "fresh-default-on-sid",
        "cwd": str(cwd),
        "transcript": [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": " ".join(["hi"] * 35)}],
            },
        ],
    }
    # Notably: no seed_enabled() call. Config alone should be enough.
    services, calls = _mock_services(str(cwd), tmp_path)
    assert hook.main(raw_payload(payload), services=services) == 0
    # Pipeline ran (message above 30-word recap-skip threshold; no
    # tool_uses so recap is skipped; just the message goes to TTS).
    assert len(calls["say"]) == 1


def test_explicit_off_state_beats_default_enabled_true(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Persisted ``enabled=false`` survives a default-on cwd.

    Once the user has explicitly opted out of narration for a session
    (``/audio-recap:on off``), the persisted choice MUST beat the
    config default. The state file is authoritative when present.
    """

    cwd = tmp_path / "proj"
    cwd.mkdir()
    cfg = cwd / ".audio-recap" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"default_enabled": True}), encoding="utf-8")

    sid = "explicit-off-sid"
    FileStateStore(state_root(tmp_path)).save(State(enabled=False), sid, str(cwd))

    payload: dict[str, Any] = {
        "session_id": sid,
        "cwd": str(cwd),
        "transcript": [
            {"role": "user", "content": "explain"},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": " ".join(["hi"] * 35)}],
            },
        ],
    }
    services, calls = _mock_services(str(cwd), tmp_path)
    assert hook.main(raw_payload(payload), services=services) == 0
    assert calls["say"] == []
    assert "audio recap disabled" in capsys.readouterr().err


def test_synthesized_transcript_carries_user_message_for_command_detection(
    tmp_path: Path, log_path: Path
) -> None:
    """``transcript_path`` synthesis must preserve the user message.

    Real CC fires deliver the conversation via ``transcript_path``
    (a JSONL). The synthesized inline transcript previously held only
    the assistant blocks, which would have hidden the slash-command
    tag from the detector. Verify the detection still fires when the
    payload arrives in the JSONL form.
    """

    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text(
        "\n".join(
            json.dumps(row)
            for row in [
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": ("<command-name>/audio-recap:repeat</command-name>"),
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Replayed last narration."}],
                    },
                },
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    payload: dict[str, Any] = {
        "session_id": "jsonl-repeat-sid",
        "cwd": "/proj",
        "hook_event_name": "Stop",
        "transcript_path": str(jsonl),
    }
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services("/proj", tmp_path)

    assert hook.main(raw_payload(payload), services=services) == 0
    # Short-circuited: no recap, no say, despite the JSONL carrying
    # a normal-looking assistant text turn.
    assert calls["claude_p"] == []
    assert calls["say"] == []

    text = log_path.read_text(encoding="utf-8")
    assert "slash_command=repeat" in text


# ---------- 077 Phase 2: two-stage default + telemetry + fallback ----------


def test_default_path_emits_two_stage_telemetry(tmp_path: Path, log_path: Path) -> None:
    """``MacOSSay.speak`` runs render → afinfo → afplay by default, and the
    hook lands the per-fire telemetry summary in the eventlog without any
    config flag flipped."""

    raw, payload = _raw_fixture("stop_payload.json")
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services(payload["cwd"], tmp_path)
    assert hook.main(raw, services=services) == 0

    # Two segments (recap + message) → two render calls + two afinfo
    # + two afplay; ``calls["say"]`` only contains render calls
    # (the legacy fallback didn't fire).
    say_render_calls = [c for c in calls["say"] if "-o" in c]
    say_legacy_calls = [c for c in calls["say"] if "-o" not in c]
    assert len(say_render_calls) == 2
    assert len(say_legacy_calls) == 0
    assert len(calls["afinfo"]) == 2
    assert len(calls["afplay"]) == 2

    text = log_path.read_text(encoding="utf-8")
    assert "say_path=two_stage" in text
    assert "aiff_duration_s=" in text
    assert "afplay_elapsed_s=" in text
    assert "expected_say_s=" in text
    assert "say_rate_wpm_observed=" in text
    assert "baseline_wpm=142" in text
    assert "synth_elapsed_s=" in text
    # No fallback this run.
    assert "say_path=legacy_fallback" not in text
    assert "fallback_reason=" not in text


def test_short_aiff_falls_back_and_eventlog_records_reason(tmp_path: Path, log_path: Path) -> None:
    """A short aiff (afinfo reports << expected) trips the fallback gate
    inside MacOSSay; the hook's eventlog summary records ``say_path=
    legacy_fallback`` and a ``fallback_reason`` field."""

    def claude(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return completed(argv, stdout="Edited a file.\n")

    def afinfo(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        # Sub-second duration so the < 70% expected gate fires for
        # any non-trivial recap / message word count.
        return completed(argv, stdout="estimated duration: 0.100 sec")

    def say(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return completed(argv)

    def afplay(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return completed(argv)

    runner = FakeProcessRunner({"claude": claude, "say": say, "afinfo": afinfo, "afplay": afplay})

    raw, payload = _raw_fixture("stop_payload.json")
    seed_enabled(tmp_path, payload)
    services = real_services(payload["cwd"], tmp_path, runner=runner)
    assert hook.main(raw, services=services) == 0

    text = log_path.read_text(encoding="utf-8")
    assert "say_path=legacy_fallback" in text
    assert "fallback_reason=" in text


def test_trace_speech_log_gated_by_config_flag(tmp_path: Path, log_path: Path) -> None:
    """``tts.trace_speech_log: true`` enables the optional unified-log
    capture; ``false`` (default) skips it. It writes a TRACE line, so it
    also needs ``log_level: trace``."""

    cwd = tmp_path / "proj"
    cwd.mkdir()
    cfg = cwd / ".audio-recap" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps({"log_level": "trace", "tts": {"trace_speech_log": True}}),
        encoding="utf-8",
    )
    _, payload = _raw_fixture("stop_payload.json")
    payload["cwd"] = str(cwd)
    seed_enabled(tmp_path, payload)
    services, _ = _mock_services(str(cwd), tmp_path)
    assert hook.main(raw_payload(payload), services=services) == 0
    text = log_path.read_text(encoding="utf-8")
    assert "event=trace_speech_log" in text


def test_trace_speech_log_off_by_default(tmp_path: Path, log_path: Path) -> None:
    raw, payload = _raw_fixture("stop_payload.json")
    seed_enabled(tmp_path, payload)
    services, _ = _mock_services(payload["cwd"], tmp_path)
    assert hook.main(raw, services=services) == 0
    text = log_path.read_text(encoding="utf-8")
    assert "event=trace_speech_log" not in text


# ---------- presence heartbeat (multi-session) ----------


def test_enabled_fire_records_presence_heartbeat(tmp_path: Path) -> None:
    """A narrating session registers itself under active/."""
    raw, payload = _raw_fixture("stop_payload.json")
    seed_enabled(tmp_path, payload)
    services, _ = _mock_services(payload["cwd"], tmp_path)

    assert hook.main(raw, services=services) == 0

    sid = payload.get("session_id") or "_global"
    assert (tmp_path / "active" / sid).exists()


def test_disabled_session_does_not_record_a_heartbeat(tmp_path: Path) -> None:
    """A heartbeat means "an enabled session that narrated" — nothing looser.

    A recap-disabled session (and equally a headless agent) produces no audio
    and has no listener, so it must not count as a neighbour that makes OTHER
    sessions announce their names.
    """
    raw, payload = _raw_fixture("stop_payload.json")  # no state seeded → disabled
    services, calls = _mock_services(payload["cwd"], tmp_path)

    assert hook.main(raw, services=services) == 0
    assert calls["say"] == []  # disabled: no narration

    sid = payload.get("session_id") or "_global"
    assert not (tmp_path / "active" / sid).exists()


def test_default_enabled_session_registers_only_once_it_narrates(tmp_path: Path) -> None:
    """``default_enabled: true`` counts from the first narration, not from open.

    Enablement by per-cwd config takes a different route to the state gate than
    ``/audio-recap:on`` does, but it must land on the same side of the
    heartbeat: the session is enabled from the moment it opens, yet nothing of
    ours runs until its first Stop, so it is not a neighbour until then. That is
    the wanted semantics rather than a gap — a session that has not spoken has
    produced no audio to disambiguate.
    """

    cwd = tmp_path / "proj"
    cwd.mkdir()
    cfg = cwd / ".audio-recap" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"default_enabled": True}), encoding="utf-8")

    sid = "default-on-presence-sid"
    payload: dict[str, Any] = {
        "session_id": sid,
        "cwd": str(cwd),
        "transcript": [
            {"role": "user", "content": "explain"},
            {"role": "assistant", "content": [{"type": "text", "text": " ".join(["hi"] * 35)}]},
        ],
    }

    # Enabled by config alone — but silent so far, so not yet a neighbour.
    assert not (tmp_path / "active" / sid).exists()

    services, calls = _mock_services(str(cwd), tmp_path)  # notably: no seed_enabled()
    assert hook.main(raw_payload(payload), services=services) == 0

    assert len(calls["say"]) == 1  # it did narrate...
    assert (tmp_path / "active" / sid).exists()  # ...and that is what registered it


def test_heartbeat_marks_the_start_of_a_narration_not_its_success(tmp_path: Path) -> None:
    """A failed ``say`` leaves the heartbeat behind — the accepted direction.

    The heartbeat is written before playback on purpose: the session queued
    behind this one on the playback lock has to see it while it is still
    speaking. So it marks the start of a narration, not its completion, and a
    turn whose ``say`` fails stays counted for the rest of the window. That
    still describes an enabled narrating session, so the worst case is one
    unneeded label — never the reverse.
    """

    def claude(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return completed(argv, stdout="A recap.\n")

    def say(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return completed(argv, returncode=1, stderr="bad voice")

    raw, payload = _raw_fixture("stop_payload.json")
    seed_enabled(tmp_path, payload)
    services = real_services(
        payload["cwd"],
        tmp_path,
        runner=FakeProcessRunner({**audio_handlers(), "claude": claude, "say": say}),
    )

    assert hook.main(raw, services=services) == 1

    sid = payload.get("session_id") or "_global"
    assert (tmp_path / "active" / sid).exists()


def test_turn_with_nothing_to_say_still_counts_as_a_narrating_session(tmp_path: Path) -> None:
    """A turn that produces no speakable segment does not un-register the session.

    Same trade as the failed-``say`` case above: the session is enabled and
    narrating, it just had a quiet turn, and the heartbeat is already written
    by the time we know there was nothing to speak.
    """
    payload: dict[str, Any] = {"session_id": "sid-quiet", "cwd": "/proj", "transcript": []}
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services(payload["cwd"], tmp_path)

    assert hook.main(raw_payload(payload), services=services) == 0
    assert calls["say"] == []  # nothing was spoken

    assert (tmp_path / "active" / "sid-quiet").exists()


def test_repeat_slash_command_registers_the_session(tmp_path: Path) -> None:
    """A replay is a sound this session made, so it counts as presence.

    The Stop hook for a ``/audio-recap:repeat`` turn speaks nothing itself —
    the slash command already played the audio — but the session was audible,
    so it should be named if a neighbour narrates next. The heartbeat is
    written before the repeat short-circuit, which is what makes that work.
    """
    payload = _slash_command_payload(
        "<command-name>/audio-recap:repeat</command-name>",
        session_id="repeat-sid",
    )
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services("/proj", tmp_path)

    assert hook.main(raw_payload(payload), services=services) == 0
    assert calls["say"] == []  # the Stop hook itself spoke nothing

    assert (tmp_path / "active" / "repeat-sid").exists()


def test_repeat_on_a_disabled_session_registers_nothing(tmp_path: Path) -> None:
    """``/repeat`` still plays on a muted session, but muting means not counting."""
    payload = _slash_command_payload(
        "<command-name>/audio-recap:repeat</command-name>",
        session_id="repeat-sid",
    )  # no state seeded → disabled
    services, _ = _mock_services("/proj", tmp_path)

    assert hook.main(raw_payload(payload), services=services) == 0

    assert not (tmp_path / "active" / "repeat-sid").exists()


def test_renamed_session_label_comes_from_custom_title(tmp_path: Path) -> None:
    """End-to-end: a CC ``/rename`` in the transcript becomes the spoken label.

    Drives the real Stop entrypoint against a JSONL carrying both a
    ``custom-title`` row (the rename) and an ``ai-title`` row (CC's topic
    title), with one other session live — the rename must win.
    """
    session_id = "sid-renamed"
    cwd = "/proj/myapp"
    transcript = tmp_path / f"{session_id}.jsonl"
    rows: list[dict[str, Any]] = [
        {"type": "ai-title", "aiTitle": "Review image content", "sessionId": session_id},
        {"type": "custom-title", "customTitle": "jean builder", "sessionId": session_id},
        {"type": "user", "message": {"role": "user", "content": "add a helper"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    # A tool use, so the turn earns a recap segment — the label
                    # is prepended to the recap, not to the message.
                    {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "x"}},
                    # Long enough to clear ``skip_if_message_words_lt`` (30), so
                    # the turn actually earns the recap segment the label rides on.
                    {"type": "text", "text": "Added the helper " + "and ran the tests " * 8},
                ],
            },
        },
    ]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    payload: dict[str, Any] = {
        "session_id": session_id,
        "cwd": cwd,
        "transcript_path": str(transcript),
    }
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services(cwd, tmp_path)
    # One other session live → the narration is labeled.
    services.presence_registry.heartbeat("some-other-session")

    assert hook.main(raw_payload(payload), services=services) == 0

    # The recap segment is spoken first, prefixed with the rename — not with
    # the aiTitle ("Review image content") and not with the cwd path.
    assert calls["say"][0][-1].startswith("jean builder. ")


def test_label_survives_a_turn_with_no_recap(tmp_path: Path) -> None:
    """THE LIVE-AUDIO REGRESSION: a no-tool-use turn skips the recap.

    The label used to be glued to the recap segment, so a turn that skipped the
    recap (``skipped_no_tool_use``) narrated with no session name at all — even
    under contention, after waiting its turn in the playback queue. The label
    must ride the first segment we actually speak, which here is the message.
    """
    session_id = "sid-no-tools"
    cwd = "/proj/myapp"
    transcript = tmp_path / f"{session_id}.jsonl"
    message = "Here is the answer " + "with plenty of words " * 8
    rows: list[dict[str, Any]] = [
        {"type": "custom-title", "customTitle": "jean builder", "sessionId": session_id},
        {"type": "user", "message": {"role": "user", "content": "explain the design"}},
        {
            "type": "assistant",
            # NO tool_use block → the recap is skipped (skip_if_no_tool_use).
            "message": {"role": "assistant", "content": [{"type": "text", "text": message}]},
        },
    ]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    payload: dict[str, Any] = {
        "session_id": session_id,
        "cwd": cwd,
        "transcript_path": str(transcript),
    }
    seed_enabled(tmp_path, payload)
    services, calls = _mock_services(cwd, tmp_path)
    services.presence_registry.heartbeat("some-other-session")

    assert hook.main(raw_payload(payload), services=services) == 0

    # Exactly one segment (the message) — and it carries the session name.
    assert len(calls["say"]) == 1
    assert calls["say"][0][-1].startswith("jean builder. ")
