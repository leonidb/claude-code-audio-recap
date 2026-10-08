"""Composition object — wires up production dependencies in one place.

Three entry points (Stop hook, ``/audio-recap:on/off/status``,
``/audio-recap:repeat``) all build a :class:`Services` instance via
:meth:`Services.from_config` and pass it down. Tests construct a
``Services`` instance with fake collaborators for end-to-end
hook/pipeline tests, or skip ``Services`` entirely and unit-test
individual components with their own fakes.

This module is the **one place** the production filesystem layout
lives: :data:`DEFAULT_AUDIO_RECAP_ROOT` is the plugin's own storage
base (the event log, narration cache, and per-session on/off state all
nest under it), and :data:`DEFAULT_CC_TRANSCRIPT_ROOT` is the separate
Claude Code directory the plugin only reads from. Every file-backed
service takes its own concrete path/root as a required constructor
argument and knows nothing about the shared base;
:meth:`Services.from_config` derives the per-service paths and injects
them. A future Linux/Windows port is a one-file change here.

Adding a new dependency means: declare it on :class:`Services`, wire
the default in :meth:`from_config`, and the entry points pick it up
without further plumbing. New backends (e.g. Linux TTS) plug in
behind the same Protocols and only :meth:`from_config` decides which
concrete to instantiate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from audio_recap.cache import FileNarrationCache, NarrationCache
from audio_recap.config import Config
from audio_recap.eventlog import EventLog, FileEventLog
from audio_recap.lock import FcntlPlaybackLock, NullPlaybackLock, PlaybackLock
from audio_recap.paths import (
    DEFAULT_AUDIO_RECAP_ROOT,
    DEFAULT_CC_TRANSCRIPT_ROOT,
    DEFAULT_LOG_PATH,
)
from audio_recap.presence import PresenceRegistry, default_presence_registry
from audio_recap.process import ProcessRunner, SubprocessProcessRunner
from audio_recap.recap import Recap
from audio_recap.recap.claude_p import ClaudePRecap
from audio_recap.recap.rule_based import RuleBasedRecap
from audio_recap.state import FileStateStore, StateStore
from audio_recap.summarizer import ClaudePSummarizer, Summarizer
from audio_recap.transcript import FileTranscriptReader, TranscriptReader
from audio_recap.tts import TTS
from audio_recap.tts.macos_say import MacOSSay

# The production filesystem layout (``DEFAULT_AUDIO_RECAP_ROOT`` and friends)
# lives in :mod:`audio_recap.paths` — a dependency-light module so a caller can
# resolve the storage root without importing this full graph, and so the layout
# the SessionEnd shell hook hardcodes has exactly one Python counterpart to be
# pinned against. Re-exported here for the existing call sites that import them
# from ``audio_recap.services``.

# Env var that redirects the event log without a code change — set it
# to keep a debugging session's hook fires out of the developer's real
# Audio Recap log. Read only here, at the composition root;
# ``FileEventLog`` itself takes an already-resolved path.
_LOG_PATH_ENV = "AUDIO_RECAP_LOG_PATH"

# Env var that turns on TRACE logging (full text: payloads, prompts, replies)
# while debugging. Users set it in ``~/.claude/settings.json`` under ``env``,
# which Claude Code applies to hooks and to its Bash tool. Read only here, like
# ``AUDIO_RECAP_LOG_PATH``.
_LOG_LEVEL_ENV = "AUDIO_RECAP_LOG_LEVEL"


def resolve_log_path(env_value: str | None, default: Path) -> Path:
    """Pick the log path: ``env_value`` if set, else ``default``.

    Pure function — the caller reads ``AUDIO_RECAP_LOG_PATH`` and supplies
    both the env value and the default. An empty / whitespace-only
    ``env_value`` is treated as unset.
    """

    if env_value is not None and env_value.strip():
        return Path(env_value.strip())
    return default


def resolve_log_level(env_value: str | None) -> str:
    """Pick the log level: ``"trace"`` if ``env_value`` says so, else ``"info"``.

    Pure function — the caller reads ``AUDIO_RECAP_LOG_LEVEL``. The match is
    case-insensitive and ignores surrounding whitespace; any other value,
    including unset, is ``"info"``.
    """

    if env_value is not None and env_value.strip().lower() == "trace":
        return "trace"
    return "info"


def default_event_log(
    log_path: Path | None = None, *, trace_enabled: bool | None = None
) -> FileEventLog:
    """A bootstrap event log, for the Stop hook's payload decoding.

    Decoding runs before :class:`Services` exists, so it logs through this.
    Both arguments default to what the environment says, as the graph's own
    log does: ``log_path`` to the ``AUDIO_RECAP_LOG_PATH``-resolved
    production path, ``trace_enabled`` to ``AUDIO_RECAP_LOG_LEVEL`` (so an
    invalid payload's TRACE line is written when tracing is on). Callers
    may pass either explicitly.
    """

    if log_path is None:
        log_path = resolve_log_path(os.environ.get(_LOG_PATH_ENV), DEFAULT_LOG_PATH)
    if trace_enabled is None:
        trace_enabled = resolve_log_level(os.environ.get(_LOG_LEVEL_ENV)) == "trace"
    return FileEventLog(log_path, trace_enabled)


def _build_playback_lock(audio_recap_root: Path, eventlog: EventLog) -> PlaybackLock:
    """Build the real flock lock, or a fail-open :class:`NullPlaybackLock`.

    If the lockfile can't be created (unwritable path, unusual FS, container
    quirks) the narration must still play — a silenced session is worse than an
    occasional overlap. Log the degradation so it's visible, then proceed with
    the no-op lock.
    """

    try:
        return FcntlPlaybackLock(audio_recap_root / "playback.lock")
    except OSError as e:
        eventlog.event("stop", lock_unavailable=True, error=str(e))
        return NullPlaybackLock()


@dataclass
class Services:
    """Wired-up production dependencies for one run.

    All fields are typed against Protocols so tests can substitute
    fakes per slot. ``config`` is concrete — :class:`Config` is a
    plain dataclass that doesn't benefit from a Protocol.
    """

    config: Config
    recap_primary: Recap
    recap_fallback: Recap
    summarizer: Summarizer
    tts: TTS
    state: StateStore
    cache: NarrationCache
    eventlog: EventLog
    transcript_reader: TranscriptReader
    playback_lock: PlaybackLock
    presence_registry: PresenceRegistry

    @classmethod
    def from_config(
        cls,
        session_id: str,
        runner: ProcessRunner | None = None,
        eventlog: EventLog | None = None,
        audio_recap_root: Path = DEFAULT_AUDIO_RECAP_ROOT,
        transcript_root: Path = DEFAULT_CC_TRANSCRIPT_ROOT,
        dry_run: bool = False,
    ) -> Services:
        """Build the production dependency graph.

        This is the composition root: it resolves the production
        defaults (``SubprocessProcessRunner``, the real ``FileEventLog``,
        the per-service paths under ``audio_recap_root``) and injects
        them into the service classes, which take everything as required
        constructor args. Tests pass a tmp ``audio_recap_root`` so the
        real ``~/.claude`` tree is never touched.

        ``session_id`` is forwarded to the Recap and Summarizer impls so
        their TRACE log lines are filterable by session.

        The config is the shipped defaults with two values resolved here:
        ``log_level`` from ``AUDIO_RECAP_LOG_LEVEL`` and ``dry_run`` from the
        keyword, which only the eval harness sets. Nothing is read from
        disk.

        ``eventlog``: when injected (tests), it is used verbatim. When
        not, the graph's event log is built with ``trace_enabled`` from
        the resolved ``log_level``.
        """

        log_path = resolve_log_path(
            os.environ.get(_LOG_PATH_ENV), audio_recap_root / "logs" / "audio-recap.log"
        )
        config = replace(
            Config.default(),
            log_level=resolve_log_level(os.environ.get(_LOG_LEVEL_ENV)),
            dry_run=dry_run,
        )

        actual_eventlog: EventLog
        if eventlog is not None:
            actual_eventlog = eventlog
        else:
            actual_eventlog = FileEventLog(log_path, config.log_level == "trace")
        actual_runner: ProcessRunner = runner if runner is not None else SubprocessProcessRunner()
        return cls(
            config=config,
            recap_primary=ClaudePRecap(config.recap, session_id, actual_runner, actual_eventlog),
            recap_fallback=RuleBasedRecap(session_id, actual_eventlog),
            summarizer=ClaudePSummarizer(
                config.summarizer, session_id, actual_runner, actual_eventlog
            ),
            tts=MacOSSay(config.tts, actual_runner),
            state=FileStateStore(audio_recap_root / "state"),
            cache=FileNarrationCache(audio_recap_root / "cache"),
            eventlog=actual_eventlog,
            transcript_reader=FileTranscriptReader(transcript_root),
            playback_lock=_build_playback_lock(audio_recap_root, actual_eventlog),
            presence_registry=default_presence_registry(audio_recap_root),
        )


__all__ = [
    "DEFAULT_AUDIO_RECAP_ROOT",
    "DEFAULT_CC_TRANSCRIPT_ROOT",
    "DEFAULT_LOG_PATH",
    "Services",
    "default_event_log",
    "resolve_log_level",
    "resolve_log_path",
]
