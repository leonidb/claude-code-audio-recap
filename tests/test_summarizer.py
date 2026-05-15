from __future__ import annotations

import subprocess

import pytest

from audio_recap.config import Summarizer as SummarizerConfig
from audio_recap.process import ProcessFailed
from audio_recap.summarizer import ClaudePSummarizer, SummarizerFailed
from tests.fakes import FakeEventLog, FakeProcessRunner


def _impl(
    *,
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    raises: BaseException | None = None,
    config: SummarizerConfig | None = None,
) -> tuple[ClaudePSummarizer, FakeProcessRunner]:
    runner = FakeProcessRunner.with_claude_p(
        stdout=stdout, stderr=stderr, returncode=returncode, raises=raises
    )
    return (
        ClaudePSummarizer(
            config if config is not None else SummarizerConfig(),
            "test-sid",
            runner,
            FakeEventLog(),
        ),
        runner,
    )


def test_summarize_returns_stripped_stdout() -> None:
    s, runner = _impl(
        stdout="  Three short sentences. About the message. Done.  \n",
        config=SummarizerConfig(model="claude-haiku-4-5"),
    )
    out = s.summarize("a long message " * 60)

    assert out == "Three short sentences. About the message. Done."
    argv, kwargs = runner.calls[0]
    assert argv == ["claude", "-p", "--model", "claude-haiku-4-5"]
    assert "Your reply, to be rewritten:" in kwargs["input"]
    assert "a long message" in kwargs["input"]


def test_timeout_raises_summarizer_failed() -> None:
    s, _ = _impl(
        raises=subprocess.TimeoutExpired(cmd="claude", timeout=15.0),
        config=SummarizerConfig(timeout_s=15.0),
    )
    with pytest.raises(SummarizerFailed, match="timed out"):
        s.summarize("text")


def test_missing_cli_raises_summarizer_failed() -> None:
    s, _ = _impl(raises=ProcessFailed(["claude"], "binary not found"))
    with pytest.raises(SummarizerFailed, match="not found on PATH"):
        s.summarize("text")


def test_nonzero_exit_raises_summarizer_failed() -> None:
    s, _ = _impl(returncode=2, stderr="boom")
    with pytest.raises(SummarizerFailed, match="exited 2"):
        s.summarize("text")


def test_empty_stdout_raises_summarizer_failed() -> None:
    s, _ = _impl(stdout="   \n  ")
    with pytest.raises(SummarizerFailed, match="empty output"):
        s.summarize("text")


# ---------- prompt-spec constraints (048: compression contract) ----------


def _capture_prompt(message: str) -> str:
    """Run summarize and return the prompt the runner saw."""

    s, runner = _impl(stdout="ok.")
    s.summarize(message)
    return " ".join(runner.calls[0][1]["input"].split())


def test_prompt_includes_input_word_count() -> None:
    flat = _capture_prompt(" ".join(["word"] * 200))
    # Concrete numbers in the prompt — a model-visible target beats a vague
    # "be brief" hint per the eval findings.
    assert "200 words long" in flat


def test_prompt_includes_compression_target_half_of_input() -> None:
    flat = _capture_prompt(" ".join(["word"] * 200))
    # 200 // 2 = 100, well under the 120-word ceiling.
    assert "no more than 100 words" in flat


def test_prompt_target_is_capped_at_hard_ceiling() -> None:
    # 500-word input → half is 250, which exceeds the 120-word hard ceiling.
    flat = _capture_prompt(" ".join(["word"] * 500))
    assert "no more than 120 words" in flat


def test_prompt_forbids_lengthening_the_input() -> None:
    flat = _capture_prompt(" ".join(["word"] * 200))
    assert "Never lengthen the input" in flat


def test_prompt_carries_audio_rendering_hint() -> None:
    flat = _capture_prompt(" ".join(["word"] * 200))
    assert "spoken English" in flat
    assert "twenty dollars" in flat
    assert "five percent" in flat


# ---------- prompt-spec constraints (052: preserve assistant voice) ----------


def test_prompt_frames_model_as_assistant_speaking_first_person() -> None:
    # Regression lock for the bug surfaced in the 2026-04 release on
    # 2026-04-30, where summaries came out as "the assistant laid
    # out…", "they suggest…". The framing/pronoun rule has to live
    # in the prompt; the LLM output isn't deterministic, but the
    # prompt is.
    flat = _capture_prompt(" ".join(["word"] * 200))
    # Model IS the assistant, not a third-party narrator describing it.
    assert "You ARE the assistant" in flat
    # Explicit pronoun rule.
    assert "first-person" in flat
    assert "second-person" in flat
    # Explicit ban on third-person self-reference (the exact forms
    # that triggered the bug).
    assert "third person" in flat
    assert '"the assistant"' in flat
    assert '"they"' in flat
