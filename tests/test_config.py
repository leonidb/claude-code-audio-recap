from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from audio_recap.config import TTS, Config, Narration, Recap, Summarizer
from tests.fakes import FakeEventLog


def _load_config(cwd: str) -> Config:
    """Config.load with a throwaway EventLog injected — config tests don't
    assert on log output, they assert on the returned Config."""

    return Config.load(cwd, eventlog=FakeEventLog())


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
    # 077 telemetry fields default off / cluster-median reference rate.
    assert t.trace_speech_log is False
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
    assert c.default_enabled is False
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
        "default_enabled",
        "log_level",
        "presence_window_s",
        "label_max_words",
    }


# ---------- per-cwd .audio-recap/config.json loader ----------


def _write_config(cwd: Path, body: str) -> Path:
    """Helper: drop a config file at ``<cwd>/.audio-recap/config.json``."""

    path = cwd / ".audio-recap" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_load_with_empty_cwd_returns_defaults() -> None:
    assert _load_config("") == Config.default()


def test_load_with_no_config_file_returns_defaults(tmp_path: Path) -> None:
    # Empty cwd directory — no .audio-recap/ at all.
    assert _load_config(str(tmp_path)) == Config.default()


def test_load_with_dry_run_true_overrides_default(tmp_path: Path) -> None:
    _write_config(tmp_path, json.dumps({"dry_run": True}))
    cfg = _load_config(str(tmp_path))
    assert cfg.dry_run is True
    # All other fields untouched.
    assert cfg.recap == Recap()
    assert cfg.tts == TTS()
    assert cfg.language == "en"


def test_load_with_malformed_json_logs_warning_and_falls_back(tmp_path: Path) -> None:
    path = _write_config(tmp_path, "{not valid json")
    log = FakeEventLog()
    cfg = Config.load(str(tmp_path), eventlog=log)
    assert cfg == Config.default()

    assert len(log.events) == 1
    event, fields = log.events[0]
    assert event == "config"
    assert fields["path"] == str(path)
    assert "error" in fields


def test_load_with_non_object_payload_logs_warning_and_falls_back(tmp_path: Path) -> None:
    _write_config(tmp_path, json.dumps([1, 2, 3]))
    log = FakeEventLog()
    assert Config.load(str(tmp_path), eventlog=log) == Config.default()

    assert len(log.events) == 1
    event, fields = log.events[0]
    assert event == "config"
    assert fields["error"] == "non-object payload"


def test_load_ignores_unknown_fields(tmp_path: Path) -> None:
    # Forward-compat: a future-version config file on an older plugin
    # should not crash; unknown keys are silently dropped.
    _write_config(
        tmp_path,
        json.dumps({"dry_run": True, "future_field": "whatever", "language": "fr"}),
    )
    cfg = _load_config(str(tmp_path))
    assert cfg.dry_run is True
    # ``language`` is not (yet) overrideable from the file — defaults win.
    assert cfg.language == "en"


def test_load_ignores_wrong_type_for_dry_run(tmp_path: Path) -> None:
    # ``"dry_run": "yes"`` is a typo waiting to happen. Silently drop
    # the bad value — falling back to the default is safer than
    # producing a Config with a string where a bool belongs.
    _write_config(tmp_path, json.dumps({"dry_run": "yes"}))
    cfg = _load_config(str(tmp_path))
    assert cfg.dry_run is False


# ---------- 069: default_enabled ----------


def test_load_with_default_enabled_true_overrides_default(tmp_path: Path) -> None:
    _write_config(tmp_path, json.dumps({"default_enabled": True}))
    cfg = _load_config(str(tmp_path))
    assert cfg.default_enabled is True
    # Untouched fields.
    assert cfg.dry_run is False


def test_load_ignores_wrong_type_for_default_enabled(tmp_path: Path) -> None:
    # Same shape guard as ``dry_run``: a string typo silently falls
    # back rather than producing a Config with a wrong-type field.
    _write_config(tmp_path, json.dumps({"default_enabled": "yes"}))
    cfg = _load_config(str(tmp_path))
    assert cfg.default_enabled is False


def test_load_default_enabled_field_absent_leaves_default(tmp_path: Path) -> None:
    # Only ``dry_run`` set in the file; default_enabled stays False.
    _write_config(tmp_path, json.dumps({"dry_run": True}))
    cfg = _load_config(str(tmp_path))
    assert cfg.default_enabled is False
    assert cfg.dry_run is True


def test_load_combined_dry_run_and_default_enabled(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        json.dumps({"dry_run": True, "default_enabled": True}),
    )
    cfg = _load_config(str(tmp_path))
    assert cfg.dry_run is True
    assert cfg.default_enabled is True


# ---------- log_level ----------


def test_load_with_log_level_trace_overrides_default(tmp_path: Path) -> None:
    _write_config(tmp_path, json.dumps({"log_level": "trace"}))
    assert _load_config(str(tmp_path)).log_level == "trace"


def test_load_ignores_unknown_log_level_value(tmp_path: Path) -> None:
    # Only ``"trace"`` turns it on; anything else (typo, wrong type,
    # legacy ``"debug"``) silently leaves the ``"info"`` default.
    for value in ("debug", "DEBUG", "verbose", "", "warn", True, 1):
        _write_config(tmp_path, json.dumps({"log_level": value}))
        assert _load_config(str(tmp_path)).log_level == "info", value


# ---------- 077: nested tts.trace_speech_log / tts.baseline_wpm ----------


def test_load_tts_trace_speech_log_true(tmp_path: Path) -> None:
    _write_config(tmp_path, json.dumps({"tts": {"trace_speech_log": True}}))
    cfg = _load_config(str(tmp_path))
    assert cfg.tts.trace_speech_log is True
    # Other TTS fields untouched (defaults).
    assert cfg.tts.voice is None
    assert cfg.tts.rate_wpm is None
    assert cfg.tts.baseline_wpm == 142


def test_load_tts_baseline_wpm_override(tmp_path: Path) -> None:
    _write_config(tmp_path, json.dumps({"tts": {"baseline_wpm": 130}}))
    cfg = _load_config(str(tmp_path))
    assert cfg.tts.baseline_wpm == 130
    assert cfg.tts.trace_speech_log is False


def test_load_tts_combined_trace_and_baseline(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        json.dumps({"tts": {"trace_speech_log": True, "baseline_wpm": 175}}),
    )
    cfg = _load_config(str(tmp_path))
    assert cfg.tts.trace_speech_log is True
    assert cfg.tts.baseline_wpm == 175


def test_load_tts_ignores_wrong_type_for_trace_speech_log(tmp_path: Path) -> None:
    _write_config(tmp_path, json.dumps({"tts": {"trace_speech_log": "yes"}}))
    cfg = _load_config(str(tmp_path))
    # Same shape guard as the top-level fields: typo silently falls
    # back rather than producing a wrong-type config.
    assert cfg.tts.trace_speech_log is False


def test_load_tts_ignores_non_positive_baseline(tmp_path: Path) -> None:
    # 0 / negative values are nonsensical for a wpm reference and
    # would cause divide-by-zero in expected_say_s. Reject silently.
    for bogus in [0, -50, "fast", True]:
        _write_config(tmp_path, json.dumps({"tts": {"baseline_wpm": bogus}}))
        cfg = _load_config(str(tmp_path))
        assert cfg.tts.baseline_wpm == 142, f"failed for {bogus!r}"


def test_load_tts_with_top_level_fields(tmp_path: Path) -> None:
    # Nested ``tts`` and top-level fields coexist cleanly.
    _write_config(
        tmp_path,
        json.dumps(
            {
                "dry_run": True,
                "default_enabled": True,
                "tts": {"trace_speech_log": True},
            }
        ),
    )
    cfg = _load_config(str(tmp_path))
    assert cfg.dry_run is True
    assert cfg.default_enabled is True
    assert cfg.tts.trace_speech_log is True


def test_load_tts_non_dict_value_is_ignored(tmp_path: Path) -> None:
    # ``"tts": "off"`` is malformed — config has no top-level
    # ``tts`` boolean. Should silently fall back to default TTS.
    _write_config(tmp_path, json.dumps({"tts": "off"}))
    cfg = _load_config(str(tmp_path))
    assert cfg.tts == TTS()
