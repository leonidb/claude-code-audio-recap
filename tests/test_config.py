from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from audio_recap.config import TTS, Config, Narration, Recap, Summarizer


def test_recap_defaults() -> None:
    r = Recap()
    assert r.model == "claude-haiku-4-5"
    # Bumped 15 → 60 to match the summarizer; recap and summary share the
    # ``claude -p`` cold-start and dispatch in parallel, so the longer
    # ceiling does not extend total wall time on the typical path.
    assert r.timeout_s == 60.0
    assert r.skip_if_no_tool_use is True
    assert r.skip_if_message_words_lt == 30


def test_summarizer_defaults() -> None:
    s = Summarizer()
    assert s.model == "claude-haiku-4-5"
    assert s.timeout_s == 60.0


def test_narration_defaults() -> None:
    n = Narration()
    # 50 words ≈ 20s at 150 wpm — the spec's upper bound for verbatim playback.
    assert n.summarize_message_over_words == 50


def test_tts_defaults_are_none() -> None:
    t = TTS()
    assert t.voice is None
    assert t.rate_wpm is None
    # 077 telemetry: cluster-median reference rate.
    assert t.baseline_wpm == 142


def test_config_defaults() -> None:
    c = Config()
    assert c.recap == Recap()
    assert c.summarizer == Summarizer()
    assert c.narration == Narration()
    assert c.tts == TTS()
    assert c.speakable_transforms == [
        "code_blocks",
        "urls",
        "time_units",
        "ratios",
        "paths",
        "currency",
        "percent",
        "symbols",
    ]
    assert c.language == "en"
    assert c.dry_run is False
    assert c.log_level == "info"


def test_default_helper_matches_plain_constructor() -> None:
    assert Config.default() == Config()


@pytest.mark.parametrize(
    ("instance", "field"),
    [
        (Config(), "language"),
        (Recap(), "model"),
        (Summarizer(), "model"),
        (Narration(), "summarize_message_over_words"),
        (TTS(), "voice"),
    ],
)
def test_config_dataclasses_are_frozen(instance: object, field: str) -> None:
    # All config dataclasses are ``frozen=True``; mutation must raise.
    # ``setattr`` bypasses the type checker's frozen-field guard — we
    # want to assert the runtime enforcement.
    with pytest.raises(FrozenInstanceError):
        setattr(instance, field, "x")


def test_default_transforms_list_is_not_shared_between_instances() -> None:
    a = Config()
    b = Config()
    assert a.speakable_transforms is not b.speakable_transforms


def test_config_shape_matches_architecture_doc() -> None:
    # Guard against field renames that would silently break docs/architecture.md.
    names = {f.name for f in fields(Config)}
    assert names == {
        "recap",
        "summarizer",
        "narration",
        "tts",
        "speakable_transforms",
        "language",
        "dry_run",
        "log_level",
        "presence_window_s",
        "label_max_words",
    }
