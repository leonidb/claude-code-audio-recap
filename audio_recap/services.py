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
from dataclasses import dataclass
from pathlib import Path

from audio_recap.cache import FileNarrationCache, NarrationCache
from audio_recap.config import Config
from audio_recap.eventlog import EventLog, FileEventLog
from audio_recap.process import ProcessRunner, SubprocessProcessRunner
from audio_recap.recap import Recap
from audio_recap.recap.claude_p import ClaudePRecap
from audio_recap.recap.rule_based import RuleBasedRecap
from audio_recap.state import FileStateStore, StateStore
from audio_recap.summarizer import ClaudePSummarizer, Summarizer
from audio_recap.transcript import FileTranscriptReader, TranscriptReader
from audio_recap.tts import TTS
from audio_recap.tts.macos_say import MacOSSay

# The plugin's own storage base. The event log, narration cache, and
# per-session on/off state all nest under it — ``from_config`` derives
# each from this one root, so a Linux/Windows port touches just this
# block. The log nests one level deeper (under ``logs/``) than the
# cache and state dirs; that asymmetry is the layout's, not a knob.
DEFAULT_AUDIO_RECAP_ROOT = Path.home() / ".claude" / "audio-recap"
DEFAULT_LOG_PATH = DEFAULT_AUDIO_RECAP_ROOT / "logs" / "audio-recap.log"

# CC's own per-session transcript dir (``~/.claude/projects/<encoded-cwd>/
# <sid>.jsonl``). The plugin only *reads* from here, so it stays a
# separate knob — it is not the plugin's storage.
DEFAULT_CC_TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"

# Env var that redirects the event log without a code change — set it
# to keep a debugging session's hook fires out of the developer's real
# Audio Recap log. Read only here, at the composition root;
# ``FileEventLog`` itself takes an already-resolved path.
_LOG_PATH_ENV = "AUDIO_RECAP_LOG_PATH"


def resolve_log_path(env_value: str | None, default: Path) -> Path:
    """Pick the log path: ``env_value`` if set, else ``default``.

    Pure function — the caller reads ``AUDIO_RECAP_LOG_PATH`` and supplies
    both the env value and the default. An empty / whitespace-only
    ``env_value`` is treated as unset.
    """

    if env_value is not None and env_value.strip():
        return Path(env_value.strip())
    return default


def default_event_log(log_path: Path | None = None) -> FileEventLog:
    """A bootstrap INFO event log.

    Payload parsing and ``Config.load`` run before the per-cwd config is
    known, so they log through this. :meth:`Services.from_config` then
    builds the real event log with ``trace_enabled`` from
    ``Config.log_level``. ``log_path`` defaults to the env-resolved
    production path; callers may pass an explicit path.
    """

    if log_path is None:
        log_path = resolve_log_path(os.environ.get(_LOG_PATH_ENV), DEFAULT_LOG_PATH)
    return FileEventLog(log_path)


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

    @classmethod
    def from_config(
        cls,
        cwd: str,
        session_id: str,
        runner: ProcessRunner | None = None,
        eventlog: EventLog | None = None,
        audio_recap_root: Path = DEFAULT_AUDIO_RECAP_ROOT,
        transcript_root: Path = DEFAULT_CC_TRANSCRIPT_ROOT,
    ) -> Services:
        """Build the production dependency graph for ``cwd``.

        This is the composition root: it resolves the production
        defaults (``SubprocessProcessRunner``, the real ``FileEventLog``,
        the per-service paths under ``audio_recap_root``) and injects
        them into the service classes, which take everything as required
        constructor args. Tests pass a tmp ``audio_recap_root`` so the
        real ``~/.claude`` tree is never touched.

        ``session_id`` is forwarded to the Recap and Summarizer impls so
        their TRACE log lines are filterable by session.

        ``eventlog``: when injected (tests), it is used verbatim. When
        not, the graph's event log is built *after* ``Config.load`` so
        its ``trace_enabled`` reflects ``Config.log_level`` — a bootstrap
        INFO log carries the (INFO-level) config-parse warning until the
        real one is wired.
        """

        log_path = resolve_log_path(
            os.environ.get(_LOG_PATH_ENV), audio_recap_root / "logs" / "audio-recap.log"
        )

        actual_eventlog: EventLog
        if eventlog is not None:
            actual_eventlog = eventlog
            config = Config.load(cwd, eventlog=actual_eventlog)
        else:
            bootstrap = default_event_log(log_path)
            config = Config.load(cwd, eventlog=bootstrap)
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
            state=FileStateStore(audio_recap_root / "projects"),
            cache=FileNarrationCache(audio_recap_root / "cache"),
            eventlog=actual_eventlog,
            transcript_reader=FileTranscriptReader(transcript_root),
        )


__all__ = [
    "DEFAULT_AUDIO_RECAP_ROOT",
    "DEFAULT_CC_TRANSCRIPT_ROOT",
    "DEFAULT_LOG_PATH",
    "Services",
    "default_event_log",
    "resolve_log_path",
]
