"""Composition root: pure log-path / log-level resolution, the bootstrap event log, dry_run."""

from __future__ import annotations

from pathlib import Path

from audio_recap.services import (
    DEFAULT_LOG_PATH,
    Services,
    default_event_log,
    resolve_log_level,
    resolve_log_path,
)
from tests.fakes import FakeProcessRunner, real_services

# ---------- resolve_log_path (pure) ----------


def test_resolve_log_path_uses_env_value() -> None:
    """A non-empty env value wins over the default.

    ``AUDIO_RECAP_LOG_PATH`` is the user escape hatch for redirecting
    the production event log without a code change — e.g. to keep a
    debugging session's hook fires out of the developer's real log.
    """

    target = "/tmp/subdir/audio-recap.log"
    assert resolve_log_path(target, DEFAULT_LOG_PATH) == Path(target)


def test_resolve_log_path_falls_back_to_default_when_env_none() -> None:
    default = Path("/tmp/default.log")
    assert resolve_log_path(None, default) == default


def test_resolve_log_path_empty_and_whitespace_env_treated_as_unset() -> None:
    default = Path("/tmp/default.log")
    assert resolve_log_path("", default) == default
    assert resolve_log_path("   ", default) == default


def test_resolve_log_path_strips_surrounding_whitespace() -> None:
    assert resolve_log_path("  /tmp/x.log  ", DEFAULT_LOG_PATH) == Path("/tmp/x.log")


# ---------- resolve_log_level (pure) ----------


def test_resolve_log_level_turns_trace_on_only_for_trace() -> None:
    """``AUDIO_RECAP_LOG_LEVEL`` turns TRACE on for "trace" in any case; anything else is INFO."""

    for value in ("trace", "TRACE", "  Trace  "):
        assert resolve_log_level(value) == "trace", value
    for value in (None, "", "info", "debug", "tracing"):
        assert resolve_log_level(value) == "info", value


# ---------- default_event_log ----------


def test_default_event_log_writes_to_the_given_path(tmp_path: Path) -> None:
    """``default_event_log`` wires a FileEventLog at the path it's handed."""

    target = tmp_path / "wired.log"
    default_event_log(target).event("stop", session_id="wired")
    assert "session_id=wired" in target.read_text(encoding="utf-8")


def test_default_event_log_is_info_level_unless_traced(tmp_path: Path) -> None:
    """The bootstrap log writes TRACE only when trace is on."""

    target = tmp_path / "info.log"
    log = default_event_log(target, trace_enabled=False)
    log.event("stop", session_id="info-line")
    log.event_trace("trace_x", body="should not appear")
    text = target.read_text(encoding="utf-8")
    assert " INFO " in text
    assert " TRACE " not in text

    traced = tmp_path / "trace.log"
    default_event_log(traced, trace_enabled=True).event_trace("trace_x", body="appears")
    assert " TRACE event=trace_x" in traced.read_text(encoding="utf-8")


# ---------- Services.from_config wiring ----------


def test_from_config_wires_the_production_graph(tmp_path: Path) -> None:
    """``from_config`` builds a fully-populated Services with every slot filled."""

    services = real_services(tmp_path, runner=FakeProcessRunner())
    assert isinstance(services, Services)
    # Every slot is wired — no None left behind by the composition root.
    assert services.recap_primary is not None
    assert services.recap_fallback is not None
    assert services.summarizer is not None
    assert services.tts is not None
    assert services.state is not None
    assert services.cache is not None
    assert services.eventlog is not None
    assert services.transcript_reader is not None
    assert services.playback_lock is not None
    assert services.presence_registry is not None


def test_from_config_roots_file_services_under_the_injected_paths(tmp_path: Path) -> None:
    """The injected roots reach the file-backed services — nothing hits ~/.claude."""

    services = real_services(tmp_path, runner=FakeProcessRunner())
    # A state save lands under the injected state root, not the home dir.
    from audio_recap.state import State

    services.state.save(State(enabled=True), "sid", "/proj")
    assert (tmp_path / "state" / "sid.json").exists()
    # A cache write lands under the injected cache root.
    services.cache.write("sid", "recap", "message")
    assert (tmp_path / "cache" / "sid.txt").exists()
    # Presence heartbeats land under the injected root too.
    services.presence_registry.heartbeat("sid")
    assert (tmp_path / "active" / "sid").exists()


def test_from_config_fails_open_to_null_lock_when_lockfile_uncreatable(tmp_path: Path) -> None:
    """Unwritable lock path → NullPlaybackLock (fail-open), never a crash."""
    from audio_recap.lock import NullPlaybackLock
    from audio_recap.services import Services

    # A FILE where the audio-recap root dir is expected — mkdir under it fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    bad_root = blocker / "audio-recap"

    services = Services.from_config(
        session_id="sid",
        runner=FakeProcessRunner(),
        eventlog=default_event_log(tmp_path / "log", trace_enabled=False),
        audio_recap_root=bad_root,
        transcript_root=tmp_path / "cc",
    )
    assert isinstance(services.playback_lock, NullPlaybackLock)


# ---------- dry_run ----------


def test_from_config_dry_run_comes_only_from_the_keyword(tmp_path: Path) -> None:
    """``dry_run`` is off unless the caller (the eval harness) asks for it."""

    default = Services.from_config(
        session_id="sid",
        runner=FakeProcessRunner(),
        audio_recap_root=tmp_path,
        transcript_root=tmp_path / "cc",
    )
    dry = Services.from_config(
        session_id="sid",
        runner=FakeProcessRunner(),
        audio_recap_root=tmp_path,
        transcript_root=tmp_path / "cc",
        dry_run=True,
    )

    assert default.config.dry_run is False
    assert dry.config.dry_run is True
