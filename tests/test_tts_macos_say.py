from __future__ import annotations

import os
import subprocess

import pytest

from audio_recap.config import TTS as TTSConfig
from audio_recap.process import ProcessFailed
from audio_recap.tts import TTSFailed
from audio_recap.tts.macos_say import MacOSSay, _parse_afinfo_duration
from tests.fakes import FakeProcessRunner

# ---------- argv-shape dispatcher used across the tests ----------


def _make_runner(
    *,
    say_render_returncode: int = 0,
    say_render_stderr: str = "",
    say_render_raises: BaseException | None = None,
    afinfo_stdout: str = "estimated duration: 999.000 sec",
    afinfo_returncode: int = 0,
    afinfo_raises: BaseException | None = None,
    afplay_returncode: int = 0,
    afplay_stderr: str = "",
    afplay_raises: BaseException | None = None,
    say_legacy_returncode: int = 0,
    say_legacy_stderr: str = "",
    say_legacy_raises: BaseException | None = None,
) -> FakeProcessRunner:
    """FakeProcessRunner configured for the four-binary MacOSSay flow.

    ``say -o ...`` is the render call; bare ``say --`` is the streaming
    legacy call. ``afinfo`` and ``afplay`` get their own knobs. The
    default afinfo stdout returns a large duration so the short-aiff
    fallback gate doesn't trip in tests not specifically exercising it.
    """

    def say_handler(
        argv: list[str], *, input: str | None, timeout: float | None
    ) -> subprocess.CompletedProcess[str] | BaseException:
        if "-o" in argv:
            if say_render_raises is not None:
                return say_render_raises
            return subprocess.CompletedProcess(
                args=argv,
                returncode=say_render_returncode,
                stdout="",
                stderr=say_render_stderr,
            )
        if say_legacy_raises is not None:
            return say_legacy_raises
        return subprocess.CompletedProcess(
            args=argv,
            returncode=say_legacy_returncode,
            stdout="",
            stderr=say_legacy_stderr,
        )

    def afinfo_handler(
        argv: list[str], *, input: str | None, timeout: float | None
    ) -> subprocess.CompletedProcess[str] | BaseException:
        if afinfo_raises is not None:
            return afinfo_raises
        return subprocess.CompletedProcess(
            args=argv,
            returncode=afinfo_returncode,
            stdout=afinfo_stdout,
            stderr="",
        )

    def afplay_handler(
        argv: list[str], *, input: str | None, timeout: float | None
    ) -> subprocess.CompletedProcess[str] | BaseException:
        if afplay_raises is not None:
            return afplay_raises
        return subprocess.CompletedProcess(
            args=argv,
            returncode=afplay_returncode,
            stdout="",
            stderr=afplay_stderr,
        )

    return FakeProcessRunner(
        {"say": say_handler, "afinfo": afinfo_handler, "afplay": afplay_handler}
    )


def _argvs(runner: FakeProcessRunner) -> list[list[str]]:
    """Return just the argv list from each captured call."""

    return [call[0] for call in runner.calls]


# ---------- default path: render → afinfo → afplay ----------


def test_default_path_runs_three_subprocesses_in_order() -> None:
    runner = _make_runner()
    impl = MacOSSay(TTSConfig(), runner=runner)
    result = impl.speak("a sentence")

    calls = _argvs(runner)
    # The three binaries fire in order, sharing a single tmp aiff.
    assert [c[0] for c in calls] == ["say", "afinfo", "afplay"]
    say_argv, afinfo_argv, afplay_argv = calls
    assert "-o" in say_argv
    aiff_path = say_argv[say_argv.index("-o") + 1]
    assert aiff_path.endswith(".aiff")
    assert say_argv[-2:] == ["--", "a sentence"]
    assert afinfo_argv == ["afinfo", aiff_path]
    assert afplay_argv == ["afplay", aiff_path]

    assert result is not None
    assert result["say_path"] == "two_stage"
    assert result["aiff_duration_s"] == 999.0
    assert "synth_elapsed_s" in result
    assert "afplay_elapsed_s" in result
    assert "expected_say_s" in result
    assert "fallback_reason" not in result


def test_voice_flag_is_wired() -> None:
    runner = _make_runner()
    MacOSSay(TTSConfig(voice="Samantha"), runner=runner).speak("hello")
    say_argv = _argvs(runner)[0]
    assert say_argv[:3] == ["say", "-v", "Samantha"]
    assert "-o" in say_argv
    assert say_argv[-2:] == ["--", "hello"]


def test_rate_wpm_flag_is_wired() -> None:
    runner = _make_runner()
    MacOSSay(TTSConfig(rate_wpm=200), runner=runner).speak("hello")
    say_argv = _argvs(runner)[0]
    assert say_argv[:3] == ["say", "-r", "200"]
    assert "-o" in say_argv


def test_both_flags_when_both_set() -> None:
    runner = _make_runner()
    MacOSSay(TTSConfig(voice="Alex", rate_wpm=180), runner=runner).speak("hello")
    say_argv = _argvs(runner)[0]
    assert say_argv[:5] == ["say", "-v", "Alex", "-r", "180"]
    assert say_argv[-2:] == ["--", "hello"]


def test_empty_text_is_a_noop() -> None:
    runner = _make_runner()
    impl = MacOSSay(TTSConfig(), runner=runner)
    assert impl.speak("") is None
    assert impl.speak("   \n\t  ") is None
    assert runner.calls == []


def test_leading_dash_text_lands_after_double_dash() -> None:
    """``--`` end-of-options marker keeps a markdown bullet from being
    misread as a flag — same invariant the legacy path enforced."""

    runner = _make_runner()
    MacOSSay(TTSConfig(), runner=runner).speak("- bullet point")
    say_argv = _argvs(runner)[0]
    assert say_argv[-2:] == ["--", "- bullet point"]


def test_aiff_tempfile_is_cleaned_up_on_success() -> None:
    runner = _make_runner()
    MacOSSay(TTSConfig(), runner=runner).speak("clean up please")
    say_argv = _argvs(runner)[0]
    aiff = say_argv[say_argv.index("-o") + 1]
    assert not os.path.exists(aiff)


def test_returns_metrics_dict_shape() -> None:
    runner = _make_runner()
    result = MacOSSay(TTSConfig(), runner=runner).speak("two words here")
    assert result is not None
    assert set(result.keys()) == {
        "say_path",
        "synth_elapsed_s",
        "aiff_duration_s",
        "afplay_elapsed_s",
        "expected_say_s",
    }


def test_expected_seconds_uses_baseline_wpm() -> None:
    runner = _make_runner()
    impl = MacOSSay(TTSConfig(baseline_wpm=120), runner=runner)
    # 6 words at 120 wpm = 3.0 s.
    result = impl.speak("one two three four five six")
    assert result is not None
    assert result["expected_say_s"] == 3.0


# ---------- fallback gate: say -o exits non-zero ----------


def test_render_failure_falls_back_to_streaming() -> None:
    runner = _make_runner(
        say_render_returncode=1,
        say_render_stderr="bad voice",
    )
    result = MacOSSay(TTSConfig(), runner=runner).speak("hello")
    calls = _argvs(runner)
    # First call: ``say -o``; second call: ``say --`` legacy fallback.
    assert calls[0][0] == "say" and "-o" in calls[0]
    assert calls[1][0] == "say" and "-o" not in calls[1]
    assert calls[1][-2:] == ["--", "hello"]
    # No afinfo / afplay attempt.
    assert all(c[0] != "afinfo" for c in calls)
    assert all(c[0] != "afplay" for c in calls)
    assert result is not None
    assert result["say_path"] == "legacy_fallback"
    assert result["fallback_reason"] == "render_failed"
    assert result["synth_elapsed_s"] == -1.0
    assert result["aiff_duration_s"] == -1.0
    assert result["afplay_elapsed_s"] == -1.0


def test_render_failure_then_legacy_failure_raises() -> None:
    runner = _make_runner(
        say_render_returncode=1,
        say_render_stderr="bad voice",
        say_legacy_returncode=2,
        say_legacy_stderr="still bad",
    )
    with pytest.raises(TTSFailed, match="say exited 2"):
        MacOSSay(TTSConfig(), runner=runner).speak("hello")


def test_say_binary_missing_raises() -> None:
    """When ``say`` itself is missing, both render and legacy fallback fail."""

    runner = _make_runner(
        say_render_raises=ProcessFailed(["say"], "binary not found"),
        say_legacy_raises=ProcessFailed(["say"], "binary not found"),
    )
    with pytest.raises(TTSFailed, match="say binary not found"):
        MacOSSay(TTSConfig(), runner=runner).speak("hello")


# ---------- fallback gate: aiff materially shorter than expected ----------


def test_short_aiff_falls_back_to_streaming() -> None:
    # 6 words at default 142 wpm → expected_s ≈ 2.535 s. afinfo
    # reports 0.5 s (≈ 20 % of expected) → trips the < 70 % gate.
    runner = _make_runner(afinfo_stdout="estimated duration: 0.500 sec")
    result = MacOSSay(TTSConfig(), runner=runner).speak("one two three four five six")
    calls = _argvs(runner)
    # render + afinfo ran; afplay was skipped; legacy say ran instead.
    assert [c[0] for c in calls if c[0] == "afinfo"] == ["afinfo"]
    assert all(c[0] != "afplay" for c in calls)
    legacy_calls = [c for c in calls if c[0] == "say" and "-o" not in c]
    assert len(legacy_calls) == 1
    assert legacy_calls[0][-2:] == ["--", "one two three four five six"]
    assert result is not None
    assert result["say_path"] == "legacy_fallback"
    assert result["fallback_reason"] == "short_aiff"
    assert result["aiff_duration_s"] == 0.5
    assert result["afplay_elapsed_s"] == -1.0


def test_aiff_just_above_threshold_does_not_fall_back() -> None:
    # 6 words / 142 wpm = ~2.535 s expected; 70 % of that = ~1.775 s.
    # 2.0 s is comfortably above the threshold.
    runner = _make_runner(afinfo_stdout="estimated duration: 2.000 sec")
    result = MacOSSay(TTSConfig(), runner=runner).speak("one two three four five six")
    assert result is not None
    assert result["say_path"] == "two_stage"
    assert result["aiff_duration_s"] == 2.0


def test_short_aiff_with_legacy_failure_raises() -> None:
    runner = _make_runner(
        afinfo_stdout="estimated duration: 0.500 sec",
        say_legacy_returncode=1,
        say_legacy_stderr="legacy also bad",
    )
    with pytest.raises(TTSFailed, match="say exited 1"):
        MacOSSay(TTSConfig(), runner=runner).speak("one two three four five six")


# ---------- fallback gate: afplay non-zero ----------


def test_afplay_failure_falls_back_to_streaming() -> None:
    runner = _make_runner(afplay_returncode=2, afplay_stderr="device unavailable")
    result = MacOSSay(TTSConfig(), runner=runner).speak("playback dies")
    calls = _argvs(runner)
    # render + afinfo + afplay (failed) + legacy say.
    assert [c[0] for c in calls] == ["say", "afinfo", "afplay", "say"]
    assert calls[-1][-2:] == ["--", "playback dies"]
    assert result is not None
    assert result["say_path"] == "legacy_fallback"
    assert result["fallback_reason"] == "afplay_failed"


def test_afplay_binary_missing_falls_back_to_streaming() -> None:
    runner = _make_runner(afplay_raises=ProcessFailed(["afplay"], "binary not found"))
    result = MacOSSay(TTSConfig(), runner=runner).speak("hello")
    assert result is not None
    assert result["say_path"] == "legacy_fallback"
    assert result["fallback_reason"] == "afplay_failed"


def test_afplay_failure_with_legacy_failure_raises() -> None:
    runner = _make_runner(
        afplay_returncode=2,
        say_legacy_returncode=3,
        say_legacy_stderr="and legacy too",
    )
    with pytest.raises(TTSFailed, match="say exited 3"):
        MacOSSay(TTSConfig(), runner=runner).speak("hello")


# ---------- afinfo edge cases ----------


def test_afinfo_missing_returns_neg_one_sentinel() -> None:
    """afinfo absent → aiff_duration_s=-1.0; the short-aiff gate (which
    checks ``> 0.0``) doesn't trip, so playback still runs."""

    runner = _make_runner(afinfo_raises=ProcessFailed(["afinfo"], "binary not found"))
    result = MacOSSay(TTSConfig(), runner=runner).speak("playable")
    assert result is not None
    assert result["say_path"] == "two_stage"
    assert result["aiff_duration_s"] == -1.0


def test_afinfo_unparseable_returns_neg_one() -> None:
    runner = _make_runner(afinfo_stdout="garbage from afinfo, no duration here")
    result = MacOSSay(TTSConfig(), runner=runner).speak("hello")
    assert result is not None
    assert result["say_path"] == "two_stage"
    assert result["aiff_duration_s"] == -1.0


def test_afinfo_nonzero_returns_neg_one() -> None:
    runner = _make_runner(afinfo_returncode=1)
    result = MacOSSay(TTSConfig(), runner=runner).speak("hello")
    assert result is not None
    assert result["aiff_duration_s"] == -1.0


# ---------- aiff cleanup on every exit path ----------


def test_aiff_cleaned_up_on_render_failure() -> None:
    runner = _make_runner(say_render_returncode=1, say_render_stderr="render bad")
    MacOSSay(TTSConfig(), runner=runner).speak("hi")
    say_argv = _argvs(runner)[0]
    aiff = say_argv[say_argv.index("-o") + 1]
    assert not os.path.exists(aiff)


def test_aiff_cleaned_up_on_short_aiff_fallback() -> None:
    runner = _make_runner(afinfo_stdout="estimated duration: 0.100 sec")
    MacOSSay(TTSConfig(), runner=runner).speak("one two three four five six")
    say_argv = _argvs(runner)[0]
    aiff = say_argv[say_argv.index("-o") + 1]
    assert not os.path.exists(aiff)


def test_aiff_cleaned_up_when_legacy_fallback_raises() -> None:
    runner = _make_runner(afplay_returncode=1, say_legacy_returncode=1, say_legacy_stderr="x")
    with pytest.raises(TTSFailed):
        MacOSSay(TTSConfig(), runner=runner).speak("hi")
    say_argv = _argvs(runner)[0]
    aiff = say_argv[say_argv.index("-o") + 1]
    assert not os.path.exists(aiff)


# ---------- afinfo duration parser ----------


def test_parse_afinfo_duration_canonical_line() -> None:
    out = """File:           /tmp/foo.aiff
File type ID:   AIFF
Num Tracks:     1
estimated duration: 4.567 sec
"""
    assert _parse_afinfo_duration(out) == 4.567


def test_parse_afinfo_duration_missing_returns_none() -> None:
    assert _parse_afinfo_duration("garbage with no duration") is None


def test_parse_afinfo_duration_tolerates_whitespace() -> None:
    assert _parse_afinfo_duration("estimated duration:    0.500   sec") == 0.5


# ---------- render / play split (v2: pre-render outside the playback lock) ----------


def test_render_synthesises_without_playing() -> None:
    """render() runs ``say -o`` + ``afinfo`` but NEVER ``afplay`` — no audio yet."""
    runner = _make_runner()
    impl = MacOSSay(TTSConfig(), runner=runner)

    rendered = impl.render("a sentence")

    assert rendered is not None
    assert rendered.use_legacy is False
    binaries = [c[0][0] for c in runner.calls]
    assert "say" in binaries
    assert "afinfo" in binaries
    assert "afplay" not in binaries  # playback deferred to play()


def test_render_empty_text_returns_none() -> None:
    runner = _make_runner()
    impl = MacOSSay(TTSConfig(), runner=runner)
    assert impl.render("") is None
    assert impl.render("   \n\t ") is None
    assert runner.calls == []


def test_play_runs_afplay_on_the_rendered_aiff() -> None:
    """play() plays exactly the AIFF render() produced, then cleans it up."""
    runner = _make_runner()
    impl = MacOSSay(TTSConfig(), runner=runner)
    rendered = impl.render("a sentence")
    assert rendered is not None
    aiff = rendered.handle

    metrics = impl.play(rendered)

    assert metrics is not None
    assert metrics["say_path"] == "two_stage"
    assert ["afplay", aiff] in _argvs(runner)
    assert not os.path.exists(aiff)  # play() owns cleanup


def test_render_then_play_matches_speak_metrics() -> None:
    """The split path yields the same metrics shape as the composed speak()."""
    runner = _make_runner()
    impl = MacOSSay(TTSConfig(), runner=runner)
    rendered = impl.render("two words here")
    assert rendered is not None
    split_metrics = impl.play(rendered)

    composed = MacOSSay(TTSConfig(), runner=_make_runner()).speak("two words here")

    assert split_metrics is not None and composed is not None
    assert set(split_metrics.keys()) == set(composed.keys())
    assert split_metrics["say_path"] == composed["say_path"] == "two_stage"


def test_render_failure_defers_streaming_fallback_to_play() -> None:
    """A ``say -o`` failure marks use_legacy; the streaming say runs in play()."""
    runner = _make_runner(say_render_returncode=1, say_render_stderr="bad voice")
    impl = MacOSSay(TTSConfig(), runner=runner)

    rendered = impl.render("hello")
    assert rendered is not None
    assert rendered.use_legacy is True
    assert rendered.fallback_reason == "render_failed"
    # render() attempted only ``say -o`` (no streaming yet).
    assert [c[0][0] for c in runner.calls] == ["say"]

    metrics = impl.play(rendered)
    assert metrics is not None
    assert metrics["say_path"] == "legacy_fallback"
    assert metrics["fallback_reason"] == "render_failed"
    # play() ran the streaming ``say --`` fallback.
    assert any(c[0][0] == "say" and "-o" not in c[0] for c in runner.calls)


def test_render_short_aiff_defers_streaming_fallback_to_play() -> None:
    """A suspiciously short AIFF marks use_legacy=short_aiff; play() streams."""
    # 6 words at 120 wpm expect 3.0s; report 1.0s (< 0.7 * 3.0) to trip the gate.
    runner = _make_runner(afinfo_stdout="estimated duration: 1.000 sec")
    impl = MacOSSay(TTSConfig(baseline_wpm=120), runner=runner)

    rendered = impl.render("one two three four five six")
    assert rendered is not None
    assert rendered.use_legacy is True
    assert rendered.fallback_reason == "short_aiff"

    metrics = impl.play(rendered)
    assert metrics is not None
    assert metrics["say_path"] == "legacy_fallback"
    assert metrics["fallback_reason"] == "short_aiff"


def test_play_afplay_failure_falls_back_to_streaming() -> None:
    """afplay failing in play() falls back to the streaming say under the lock."""
    runner = _make_runner(afplay_returncode=1, afplay_stderr="device busy")
    impl = MacOSSay(TTSConfig(), runner=runner)

    rendered = impl.render("hello")
    assert rendered is not None
    assert rendered.use_legacy is False  # render was clean; afplay fails later

    metrics = impl.play(rendered)
    assert metrics is not None
    assert metrics["say_path"] == "legacy_fallback"
    assert metrics["fallback_reason"] == "afplay_failed"


def test_play_cleans_up_aiff_even_on_legacy_fallback() -> None:
    """The temp AIFF is unlinked by play() even when it streams instead."""
    runner = _make_runner(say_render_returncode=1)
    impl = MacOSSay(TTSConfig(), runner=runner)
    rendered = impl.render("hello")
    assert rendered is not None
    impl.play(rendered)
    assert not os.path.exists(rendered.handle)


def test_discard_unlinks_rendered_aiff_without_playing() -> None:
    """discard() frees a rendered temp AIFF without ever running afplay."""
    runner = _make_runner()
    impl = MacOSSay(TTSConfig(), runner=runner)
    rendered = impl.render("hello")
    assert rendered is not None
    assert os.path.exists(rendered.handle)

    impl.discard(rendered)

    assert not os.path.exists(rendered.handle)
    assert not any(c[0][0] == "afplay" for c in runner.calls)  # never played
