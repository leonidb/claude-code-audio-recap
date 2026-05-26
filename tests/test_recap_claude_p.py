from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from audio_recap.config import Recap as RecapConfig
from audio_recap.payload import PayloadParser
from audio_recap.recap import RecapFailed
from audio_recap.recap.claude_p import ClaudePRecap
from tests.fakes import FakeEventLog, FakeProcessRunner

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> Any:
    """Load a CC-shape stop payload and parse it into a TurnPayload."""

    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return PayloadParser.from_dict(data)


def _impl(
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    raises: BaseException | None = None,
    config: RecapConfig | None = None,
) -> tuple[ClaudePRecap, FakeProcessRunner]:
    """Construct a ``ClaudePRecap`` wired to a ``FakeProcessRunner``.

    Returns the impl + the runner so tests can both call generate() and
    inspect the captured argv / input on the runner.
    """

    runner = FakeProcessRunner.with_claude_p(
        stdout=stdout, stderr=stderr, returncode=returncode, raises=raises
    )
    return (
        ClaudePRecap(
            config if config is not None else RecapConfig(),
            "test-sid",
            runner,
            FakeEventLog(),
        ),
        runner,
    )


def test_success_returns_trimmed_stdout() -> None:
    impl, _ = _impl(stdout="  Edited one file and ran the tests.  \n")
    out = impl.generate(_load_fixture("stop_payload.json"))
    assert out == "Edited one file and ran the tests."


def test_multi_sentence_is_truncated_at_first_period_space() -> None:
    impl, _ = _impl(stdout="Edited a file, ran pytest. Also refactored utils.py.\n")
    out = impl.generate(_load_fixture("stop_payload.json"))
    assert out == "Edited a file, ran pytest."


def test_whitespace_is_collapsed() -> None:
    impl, _ = _impl(stdout="Edited  a  file\n\nand ran\tthe tests.")
    out = impl.generate(_load_fixture("stop_payload.json"))
    assert out == "Edited a file and ran the tests."


def test_timeout_raises_recap_failed() -> None:
    impl, _ = _impl(raises=subprocess.TimeoutExpired(cmd="claude", timeout=3.0))
    with pytest.raises(RecapFailed, match="timed out"):
        impl.generate(_load_fixture("stop_payload.json"))


def test_nonzero_exit_raises_recap_failed() -> None:
    impl, _ = _impl(returncode=1, stderr="authentication required")
    with pytest.raises(RecapFailed, match="exited 1"):
        impl.generate(_load_fixture("stop_payload.json"))


def test_empty_stdout_raises_recap_failed() -> None:
    impl, _ = _impl(stdout="   \n")
    with pytest.raises(RecapFailed, match="empty output"):
        impl.generate(_load_fixture("stop_payload.json"))


def test_claude_binary_missing_raises_recap_failed() -> None:
    # The runner now wraps FileNotFoundError in ProcessFailed; ClaudePRecap
    # catches that and translates to RecapFailed("claude CLI not found...").
    from audio_recap.process import ProcessFailed

    impl, _ = _impl(raises=ProcessFailed(["claude"], "binary not found"))
    with pytest.raises(RecapFailed, match="not found"):
        impl.generate(_load_fixture("stop_payload.json"))


def test_subprocess_is_called_with_configured_model() -> None:
    impl, runner = _impl(stdout="ok.\n", config=RecapConfig(model="claude-opus-4-7", timeout_s=5.0))
    impl.generate(_load_fixture("stop_payload.json"))
    argv, kwargs = runner.calls[0]
    assert argv[0] == "claude"
    assert argv[1] == "-p"
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "claude-opus-4-7"
    assert "--strict-mcp-config" in argv
    assert kwargs["timeout"] == 5.0


def test_subprocess_uses_isolation_flags_and_neutral_cwd() -> None:
    import tempfile

    impl, runner = _impl(stdout="ok.\n")
    impl.generate(_load_fixture("stop_payload.json"))
    argv, kwargs = runner.calls[0]

    # Isolation comes from the CLI flags (env-independent), not an env scrub:
    # --strict-mcp-config with no --mcp-config loads zero MCP servers.
    for flag in ("--strict-mcp-config", "--tools", "--setting-sources", "--no-session-persistence"):
        assert flag in argv, f"missing isolation flag: {flag}"
    # Neutral cwd — the system temp dir, not the caller's project dir.
    assert kwargs["cwd"] == tempfile.gettempdir()
    # The child inherits the parent environment unchanged (no env plumbing).
    assert "env" not in kwargs


def test_prompt_contains_tool_use_trace() -> None:
    impl, runner = _impl(stdout="ok.\n")
    impl.generate(_load_fixture("stop_payload.json"))
    prompt = runner.calls[0][1]["input"]
    assert isinstance(prompt, str)
    # Tool names from the fixture should be visible in the serialized trace.
    assert "Read" in prompt
    assert "Edit" in prompt
    assert "Bash" in prompt


def test_qa_turn_produces_empty_tool_use_trace() -> None:
    impl, runner = _impl(stdout="Answered.\n")
    out = impl.generate(_load_fixture("stop_payload_qa.json"))
    assert out == "Answered."
    prompt = runner.calls[0][1]["input"]
    assert isinstance(prompt, str)
    # Zero tool uses serializes to "[]" in the prompt.
    assert "[]" in prompt


# ---------- prompt-spec constraints (PRD §2 / 028 polish) ----------


def test_prompt_template_carries_no_preamble_no_quote_no_bullet_rules() -> None:
    impl, runner = _impl(stdout="ok.\n")
    impl.generate(_load_fixture("stop_payload.json"))
    prompt = runner.calls[0][1]["input"]
    assert isinstance(prompt, str)
    # Whitespace-flatten so the substring checks are robust to line wraps.
    flat = " ".join(prompt.split())
    # Each constraint from PRD §2 must be communicated to the model.
    assert "max 20 words" in flat
    assert "past tense" in flat
    assert "subject omitted" in flat
    assert "Aggregate similar actions" in flat
    assert "never list filenames" in flat
    assert "errors that stopped progress" in flat
    assert "spoken separately" in flat
    assert "No preamble" in flat
    assert "no code fences" in flat
    assert "no quotes" in flat
    assert "no bullet points" in flat
    # Audio-rendering hint — added to fix the "dollar sign 20" / "percent
    # sign 5" mishearings the eval logs surfaced.
    assert "spoken English" in flat
    assert "twenty dollars" in flat
    assert "five percent" in flat
    # Tool-name guidance (057) — the prompt must steer the model away
    # from echoing internal-naming-convention identifiers verbatim.
    assert "Never echo a tool name verbatim" in flat
    assert "describe the action in plain English" in flat
    assert "replied" in flat or "sent a reply" in flat


def test_prompt_template_does_not_special_case_mcp_prefix() -> None:
    """The instruction must be general — no hard-coded prefix patterns."""

    impl, runner = _impl(stdout="ok.\n")
    impl.generate(_load_fixture("stop_payload.json"))
    prompt = runner.calls[0][1]["input"]
    assert isinstance(prompt, str)
    # The prompt may *illustrate* the issue with example-shaped names,
    # but it must not name a real prefix that exists in any user's
    # plugin set. ``mcp__`` is the well-known offender; assert it is
    # NOT singled out as a magic prefix to filter.
    assert "mcp__" not in prompt


def test_overlong_output_is_truncated_to_30_words_with_ellipsis() -> None:
    # 50-word run-on with no sentence boundary — exercises the hard cap path.
    overlong = " ".join(["word"] * 50)
    impl, _ = _impl(stdout=overlong + "\n")
    out = impl.generate(_load_fixture("stop_payload.json"))

    parts = out.split()
    # 30 words + " ..." marker.
    assert len(parts) == 31
    assert parts[-1] == "..."
    assert parts[:30] == ["word"] * 30


def test_30_word_output_is_unchanged() -> None:
    # Exact-cap output: not truncated, no ellipsis appended.
    exact = " ".join(["word"] * 30)
    impl, _ = _impl(stdout=exact + "\n")
    out = impl.generate(_load_fixture("stop_payload.json"))
    assert out == exact


def test_surrounding_double_quotes_are_stripped() -> None:
    # The prompt asks for no quotes; if the model wraps anyway, strip them.
    impl, _ = _impl(stdout='"Edited a file."\n')
    out = impl.generate(_load_fixture("stop_payload.json"))
    assert out == "Edited a file."


def test_curly_quotes_are_stripped() -> None:
    # Some models like to "smarten" the response into typographic quotes.
    impl, _ = _impl(stdout="“Edited a file.”\n")
    out = impl.generate(_load_fixture("stop_payload.json"))
    assert out == "Edited a file."


def test_leading_bullet_marker_is_stripped() -> None:
    for marker in ("- ", "* ", "• "):
        impl, _ = _impl(stdout=f"{marker}Edited a file.\n")
        out = impl.generate(_load_fixture("stop_payload.json"))
        assert out == "Edited a file.", f"failed for marker={marker!r}"
