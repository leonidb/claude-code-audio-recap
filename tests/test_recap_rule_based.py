from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from audio_recap.recap.rule_based import RuleBasedRecap
from tests.fakes import FakeEventLog

FIXTURES = Path(__file__).parent / "fixtures"


def _recap() -> RuleBasedRecap:
    """A RuleBasedRecap with a throwaway EventLog — these tests assert on
    the returned recap string, not on log output."""

    return RuleBasedRecap("test-sid", FakeEventLog())


def _load(name: str) -> Any:
    """Load a CC-shape stop payload and parse it into a TurnPayload."""

    from audio_recap.payload import PayloadParser

    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return PayloadParser.from_dict(data)


def _payload(transcript: list[dict[str, Any]]) -> Any:
    """Helper: build a TurnPayload from a transcript shape."""

    from audio_recap.payload import PayloadParser

    return PayloadParser.from_dict({"transcript": transcript})


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        # Mixed turn: 1 Read, 1 Edit, 1 Bash(`pytest -q`).
        ("stop_payload.json", "Read a file, edited a file, ran `pytest`."),
        # Pure Q&A: zero tool uses.
        ("stop_payload_qa.json", "Asked a question."),
        # Read-only: two Read tool uses.
        ("stop_payload_read_only.json", "Read 2 files."),
        # Single Bash command.
        ("stop_payload_failed_command.json", "Ran `ruff`."),
    ],
)
def test_fixture_snapshots(fixture: str, expected: str) -> None:
    assert _recap().generate(_load(fixture)) == expected


def test_unknown_tool_aggregates_as_count_without_naming() -> None:
    # 057: never echo unrecognized tool names — they may follow internal
    # naming conventions (``mcp__service__verb``) that ``say`` mangles.
    # Aggregate as a count instead.
    payload: dict[str, Any] = {
        "transcript": [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "TodoWrite", "input": {}},
                    {"type": "tool_use", "id": "t2", "name": "TodoWrite", "input": {}},
                    {"type": "text", "text": "Tracked."},
                ],
            }
        ]
    }
    out = _recap().generate(_payload(payload["transcript"]))
    assert out == "Used 2 other tools."
    assert "TodoWrite" not in out


def test_mcp_style_tool_name_is_never_echoed() -> None:
    # Reproduces the eval-run regression where ``mcp__example__reply``
    # leaked into TTS as "M C P underscore example underscore reply".
    payload: dict[str, Any] = {
        "transcript": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "mcp__example__reply",
                        "input": {},
                    },
                ],
            }
        ]
    }
    out = _recap().generate(_payload(payload["transcript"]))
    assert "mcp__example__reply" not in out
    assert "mcp" not in out.lower()
    assert "_" not in out
    assert out == "Used 1 other tool."


def test_other_bucket_count_aggregates_distinct_names() -> None:
    # 3 unrecognized tool uses across 2 distinct names → count by
    # *uses*, not by distinct names. The user only wants to know how
    # many tools fired, not which ones.
    payload: dict[str, Any] = {
        "transcript": [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Zeta", "input": {}},
                    {"type": "tool_use", "id": "t2", "name": "Alpha", "input": {}},
                    {"type": "tool_use", "id": "t3", "name": "Alpha", "input": {}},
                ],
            }
        ]
    }
    out = _recap().generate(_payload(payload["transcript"]))
    assert out == "Used 3 other tools."
    assert "Alpha" not in out
    assert "Zeta" not in out


def test_bash_without_input_dict_falls_back_to_generic_command() -> None:
    payload: dict[str, Any] = {
        "transcript": [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": None},
                ],
            }
        ]
    }
    assert _recap().generate(_payload(payload["transcript"])) == "Ran a command."


def test_bash_with_empty_command_uses_generic_fallback() -> None:
    payload: dict[str, Any] = {
        "transcript": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "   "},
                    },
                ],
            }
        ]
    }
    assert _recap().generate(_payload(payload["transcript"])) == "Ran a command."


def test_multiple_bash_commands_produce_count() -> None:
    payload: dict[str, Any] = {
        "transcript": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "pytest"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Bash",
                        "input": {"command": "ruff check ."},
                    },
                ],
            }
        ]
    }
    assert _recap().generate(_payload(payload["transcript"])) == "Ran 2 commands."


def test_mixed_reads_and_edits_no_runs() -> None:
    payload: dict[str, Any] = {
        "transcript": [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                    {"type": "tool_use", "id": "t2", "name": "Grep", "input": {}},
                    {"type": "tool_use", "id": "t3", "name": "Edit", "input": {}},
                ],
            }
        ]
    }
    out = _recap().generate(_payload(payload["transcript"]))
    assert out == "Read 2 files, edited a file."


def test_transcript_missing_is_treated_as_qa() -> None:
    from audio_recap.payload import PayloadParser

    assert _recap().generate(PayloadParser.from_dict({})) == "Asked a question."


def test_non_assistant_last_message_is_skipped() -> None:
    payload: dict[str, Any] = {
        "transcript": [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                ],
            },
            {"role": "user", "content": "thanks"},
        ]
    }
    assert _recap().generate(_payload(payload["transcript"])) == "Read a file."
