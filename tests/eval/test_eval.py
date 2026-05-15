"""Unit tests for the Audio Recap eval harness.

Fast, no-network coverage of the harness's pure helpers — the harvest
KV parser (apostrophe / control-char escaping), the fire-grouping and
collapse logic, the runner's payload synthesis, the stream-json parser,
the dry-run config context manager, the in-process hook fire, and the
``check_run`` no-failure invariant check.

The full ``claude``-CLI-driven run is the CLI's job
(``python -m tests.eval``), not pytest's — its exit code is the
pass/fail. See ``tests/eval/README.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.eval import harvest, runner
from tests.eval.prompts import PROMPTS, smoke_prompts

# ---------- KV parser regression cases ----------


def test_parse_kv_basic_unquoted_pairs() -> None:
    line = "2026-05-04T09:00:00Z INFO event=stop session_id=abc word_count=42"
    out = harvest.parse_kv(line)
    assert out["event"] == "stop"
    assert out["session_id"] == "abc"
    assert out["word_count"] == "42"


def test_parse_kv_handles_quoted_value_with_spaces() -> None:
    line = "2026-05-04T09:00:00Z INFO event=stop error='claude -p timed out'"
    out = harvest.parse_kv(line)
    assert out["error"] == "claude -p timed out"


def test_parse_kv_handles_apostrophe_escape() -> None:
    """``'\\''`` inside a single-quoted value round-trips to a literal ``'``.

    This is the regression that drove the parser to walk character by
    character instead of using a regex — it's the one shape that any
    POSIX-style quoting bug would silently mangle.
    """

    # Synthesize the exact eventlog escape: open-quote, the content with
    # an embedded ``'\''`` apostrophe escape, close-quote.
    line = "INFO event=stop note='it'\\''s broken'"
    out = harvest.parse_kv(line)
    assert out["note"] == "it's broken"


def test_parse_kv_handles_multiple_apostrophes_in_one_value() -> None:
    line = "INFO event=stop note='it'\\''s the agent'\\''s reply'"
    out = harvest.parse_kv(line)
    assert out["note"] == "it's the agent's reply"


def test_parse_kv_handles_embedded_control_chars() -> None:
    line = "INFO event=stop body='line1\\nline2\\tcol'"
    out = harvest.parse_kv(line)
    assert out["body"] == "line1\nline2\tcol"


def test_parse_kv_handles_value_with_equals() -> None:
    line = "INFO event=stop note='key=val=tail'"
    out = harvest.parse_kv(line)
    assert out["note"] == "key=val=tail"


def test_parse_kv_handles_empty_quoted_value() -> None:
    line = "INFO event=stop note=''"
    out = harvest.parse_kv(line)
    assert out["note"] == ""


def test_parse_kv_skips_non_kv_tokens() -> None:
    """The leading ISO timestamp and INFO level word aren't ``key=value`` —
    the parser must walk past them without inventing keys."""

    line = "2026-05-04T09:00:00Z INFO event=stop"
    out = harvest.parse_kv(line)
    assert "INFO" not in out
    assert "2026" not in out
    assert out["event"] == "stop"


# ---------- harvest grouping ----------


def test_fires_groups_by_fired_marker() -> None:
    log = [
        "2026-05-04T09:00:00Z INFO event=stop session_id=A fired=true",
        "2026-05-04T09:00:00Z INFO event=stop session_id=A state=enabled",
        "2026-05-04T09:00:00Z TRACE event=trace_payload session_id=A",  # ignored
        "2026-05-04T09:00:01Z INFO event=stop session_id=B fired=true",
        "2026-05-04T09:00:01Z INFO event=stop session_id=B state=enabled",
    ]
    groups = harvest.fires(log)
    assert len(groups) == 2
    sids = [g[0]["session_id"] for g in groups]
    assert sids == ["A", "B"]


def test_collapse_later_keys_win() -> None:
    g = [
        {"session_id": "A", "fired": "true"},
        {"session_id": "A", "tts_status": "spoken"},
        {"session_id": "A", "say_done": "true"},
    ]
    out = harvest.collapse(g)
    assert out["session_id"] == "A"
    assert out["tts_status"] == "spoken"
    assert out["say_done"] == "true"


# ---------- harvest end-to-end on synthetic state + log ----------


def test_harvest_run_joins_log_and_state(tmp_path: Path) -> None:
    state = {
        "run_id": "test",
        "repo_root": str(tmp_path),
        "turn_count": 1,
        "allowed_tools": ["Read"],
        "turns": [
            {
                "n": 1,
                "shape": "short-prose",
                "prompt": "hi",
                "session_id": "sid-1",
                "assistant_text": "Hello.",
                "assistant_word_count": 1,
                "text_block_count": 1,
                "tool_use_count": 0,
                "cli_error": None,
                "hook_exit": 0,
                "hook_stderr": "",
            }
        ],
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    log = "\n".join(
        [
            "2026-05-04T09:00:00Z INFO event=stop session_id=sid-1 fired=true",
            "2026-05-04T09:00:00Z INFO event=stop session_id=sid-1 state=enabled",
            (
                "2026-05-04T09:00:00Z INFO event=stop session_id=sid-1 "
                "tts_status=dry_run segments=1 recap_path=skipped_no_tool_use "
                "summary_path=under_threshold_verbatim "
                "recap_text='' message_text=Hello."
            ),
        ]
    )
    log_path = tmp_path / "logs" / "audio-recap.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(log + "\n", encoding="utf-8")

    out = harvest.harvest_run(tmp_path)
    assert out == tmp_path / "data.json"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["turn_count"] == 1
    turn = data["turns"][0]
    assert turn["session_id"] == "sid-1"
    assert turn["audio_recap"]["tts_status"] == "dry_run"
    assert turn["audio_recap"]["recap_path"] == "skipped_no_tool_use"
    assert turn["audio_recap"]["message_text"] == "Hello."
    # results.md was rendered alongside.
    assert (tmp_path / "results.md").exists()


# ---------- runner pure-helper tests ----------


def test_synthesize_stop_payload_shape() -> None:
    blocks = [
        {"type": "text", "text": "Hi."},
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "/x"}},
    ]
    p = runner._synthesize_stop_payload(blocks, "sid-x", Path("/proj"))
    assert p["session_id"] == "sid-x"
    assert p["cwd"] == "/proj"
    assert p["hook_event_name"] == "Stop"
    assert p["transcript"][0]["role"] == "assistant"
    assert p["transcript"][0]["content"] == blocks


def test_aggregate_assistant_text_joins_text_blocks_only() -> None:
    blocks = [
        {"type": "text", "text": "First."},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}},
        {"type": "text", "text": "Second."},
    ]
    out = runner._aggregate_assistant_text(blocks)
    assert out == "First.\n\nSecond."


# ---------- stream-json parser ----------


def _stream_json(*events: dict[str, Any]) -> str:
    """Render events as the JSONL ``claude -p --output-format stream-json`` emits."""

    return "\n".join(json.dumps(e) for e in events) + "\n"


def test_parse_stream_json_extracts_ordered_text_and_tool_use_blocks() -> None:
    stdout = _stream_json(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "Looking…"},
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "/x"}},
                    {"type": "text", "text": "Done."},
                ]
            },
        },
        {"type": "result", "is_error": False},
    )
    blocks, error = runner._parse_stream_json(stdout)
    assert error is None
    assert blocks == [
        {"type": "text", "text": "Looking…"},
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "/x"}},
        {"type": "text", "text": "Done."},
    ]


def test_parse_stream_json_drops_thinking_and_other_block_types() -> None:
    # The hook only walks text + tool_use; thinking (and anything else)
    # must not leak into the synthesized transcript.
    stdout = _stream_json(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "weighing options", "signature": "x"},
                    {"type": "text", "text": "Answer."},
                ]
            },
        }
    )
    blocks, error = runner._parse_stream_json(stdout)
    assert error is None
    assert blocks == [{"type": "text", "text": "Answer."}]


def test_parse_stream_json_aggregates_across_multiple_assistant_events() -> None:
    stdout = _stream_json(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "One."}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Two."}]}},
    )
    blocks, _ = runner._parse_stream_json(stdout)
    assert [b["text"] for b in blocks] == ["One.", "Two."]


def test_parse_stream_json_result_is_error_becomes_error_string() -> None:
    stdout = _stream_json(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}},
        {"type": "result", "is_error": True, "api_error_status": "overloaded_error"},
    )
    blocks, error = runner._parse_stream_json(stdout)
    assert blocks == [{"type": "text", "text": "hi"}]
    assert error == "overloaded_error"


def test_parse_stream_json_tolerates_malformed_lines() -> None:
    # A non-JSON line (stray CLI chatter) must be skipped, not crash.
    stdout = (
        "not json at all\n"
        + json.dumps(
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}
        )
        + "\n"
    )
    blocks, error = runner._parse_stream_json(stdout)
    assert blocks == [{"type": "text", "text": "ok"}]
    assert error is None


def test_parse_stream_json_empty_output_is_empty_blocks_no_error() -> None:
    blocks, error = runner._parse_stream_json("")
    assert blocks == []
    assert error is None


def test_dry_run_config_context_manager_writes_and_restores(tmp_path: Path) -> None:
    """Setup writes the dry-run override; teardown removes it."""

    repo = tmp_path / "repo"
    repo.mkdir()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    cfg = repo / ".audio-recap" / "config.json"
    assert not cfg.exists()

    with runner._dry_run_config(repo, bundle):
        assert cfg.exists()
        assert json.loads(cfg.read_text(encoding="utf-8")) == {"dry_run": True}

    # Teardown removed both the file and the now-empty parent dir.
    assert not cfg.exists()
    assert not cfg.parent.exists()


def test_dry_run_config_context_manager_preserves_prior_file(tmp_path: Path) -> None:
    """A pre-existing config gets snapshotted to the bundle and restored."""

    repo = tmp_path / "repo"
    (repo / ".audio-recap").mkdir(parents=True)
    cfg = repo / ".audio-recap" / "config.json"
    prior = json.dumps({"dry_run": False, "future_field": "ok"}) + "\n"
    cfg.write_text(prior, encoding="utf-8")
    bundle = tmp_path / "bundle"
    bundle.mkdir()

    with runner._dry_run_config(repo, bundle):
        # Inside the context the file carries the dry-run override …
        assert json.loads(cfg.read_text(encoding="utf-8")) == {"dry_run": True}
        # … and the bundle has the snapshot.
        assert (bundle / "config.json.backup").read_text(encoding="utf-8") == prior

    # On exit, the original file is restored verbatim.
    assert cfg.read_text(encoding="utf-8") == prior


def test_dry_run_config_restores_on_exception(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    cfg = repo / ".audio-recap" / "config.json"

    with pytest.raises(RuntimeError), runner._dry_run_config(repo, bundle):
        assert cfg.exists()
        raise RuntimeError("simulated runner crash")

    assert not cfg.exists()


# ---------- in-process hook fire (no claude CLI; synthesized payload) ----------


def test_fire_hook_in_process_writes_to_bundle_log(tmp_path: Path) -> None:
    """``_fire_hook`` runs the Stop hook in-process with a bundle-rooted Services.

    The synthesized payload has no pre-seeded state, so the default-off
    gate stops the hook before TTS — but the ``fired=true`` marker and
    the ``state=disabled`` line MUST land in the bundle-local log
    (``run_dir/logs/audio-recap.log``), never the developer's real log.
    That's the injection contract the runner relies on, and the hook's
    stderr is captured rather than leaked.
    """

    sid = "in-process-test-sid"
    payload = {
        "session_id": sid,
        "cwd": str(tmp_path),
        "hook_event_name": "Stop",
        "transcript": [
            {"role": "assistant", "content": [{"type": "text", "text": "Hello world."}]}
        ],
    }
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    rc, stderr = runner._fire_hook(payload, run_dir)
    assert rc == 0, stderr

    log_path = run_dir / "logs" / "audio-recap.log"
    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    assert f"session_id={sid}" in text
    assert "fired=true" in text
    assert "state=disabled" in text
    # The hook's "disabled; skipping" stderr line was captured, not leaked.
    assert "disabled" in stderr


# ---------- check_run no-failure invariants ----------


def _turn(n: int = 1, **overrides: Any) -> dict[str, Any]:
    """A harvested-turn dict that passes ``check_run``; override to break it.

    ``audio_recap=`` overrides are merged into the nested dict so a test
    can break exactly one field and leave the rest clean.
    """

    ar_overrides = overrides.pop("audio_recap", {})
    turn: dict[str, Any] = {
        "n": n,
        "cli_error": None,
        "hook_exit": 0,
        "audio_recap": {
            "state": "enabled",
            "tts_status": "dry_run",
            "recap_path": "claude_p",
        },
    }
    turn.update(overrides)
    turn["audio_recap"].update(ar_overrides)
    return turn


def test_check_run_clean_data_has_no_failures() -> None:
    data = {"turns": [_turn(1), _turn(2)]}
    assert runner.check_run(data) == []


def test_check_run_flags_nonzero_hook_exit() -> None:
    failures = runner.check_run({"turns": [_turn(1, hook_exit=1)]})
    assert len(failures) == 1
    assert "hook_exit=1" in failures[0]


def test_check_run_flags_cli_error() -> None:
    failures = runner.check_run({"turns": [_turn(1, cli_error="claude CLI timeout after 180s")]})
    assert len(failures) == 1
    assert "cli_error" in failures[0]


def test_check_run_flags_disabled_state() -> None:
    failures = runner.check_run({"turns": [_turn(1, audio_recap={"state": "disabled"})]})
    assert any("state=" in f for f in failures)


def test_check_run_flags_non_dry_run_tts_status() -> None:
    failures = runner.check_run({"turns": [_turn(1, audio_recap={"tts_status": "failed"})]})
    assert any("tts_status=" in f for f in failures)


def test_check_run_flags_recap_path_none() -> None:
    failures = runner.check_run({"turns": [_turn(1, audio_recap={"recap_path": "none"})]})
    assert any("recap_path=none" in f for f in failures)


def test_check_run_does_not_flag_rule_based_fallback() -> None:
    # A claude_p → rule_based fallback is the system handling a failure
    # gracefully, not a failure — check_run must stay quiet about it.
    data = {"turns": [_turn(1, audio_recap={"recap_path": "rule_based"})]}
    assert runner.check_run(data) == []


# ---------- prompts catalogue sanity ----------


def test_prompts_catalogue_has_unique_indices_and_shapes() -> None:
    indices = [p["n"] for p in PROMPTS]
    assert indices == sorted(indices)
    assert len(set(indices)) == len(indices)
    # Shapes should be human-readable kebab/snake labels — no whitespace.
    for p in PROMPTS:
        assert p["shape"]
        assert " " not in p["shape"]


def test_smoke_prompts_returns_first_n() -> None:
    assert smoke_prompts(3) == PROMPTS[:3]
    assert smoke_prompts(1) == PROMPTS[:1]
    assert smoke_prompts(0) == []
