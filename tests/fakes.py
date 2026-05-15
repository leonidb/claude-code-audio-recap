"""Shared test fakes for unit-injecting Audio Recap dependencies.

The production graph is wired through :class:`audio_recap.services.Services`;
tests construct a ``Services`` instance with selected fakes per slot
instead of patching the underlying ``subprocess`` module. Each fake
is intentionally minimal — counter, capture-list, or canned-return
— so test assertions read directly off public attributes.

Usage shape:

    services = build_services(
        recap_primary=FakeRecap(returns="Edited a file."),
        tts=FakeTTS(),
        state=InMemoryStateStore({("sid", "/proj"): True}),
    )
    rc = hook.main(services=services)

Slots not passed to :func:`build_services` get a sensible default fake
(``FakeRecap`` with a canned string, ``FakeTTS`` that succeeds silently,
etc.) so tests stay short.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from audio_recap.config import Config
from audio_recap.payload import TurnPayload
from audio_recap.process import ProcessFailed, ProcessRunner
from audio_recap.recap import RecapFailed
from audio_recap.services import Services
from audio_recap.state import FileStateStore, State
from audio_recap.summarizer import SummarizerFailed
from audio_recap.tts import TTSFailed

# ---------- Recap ----------


@dataclass
class FakeRecap:
    """In-memory Recap. ``returns`` may be a string OR an exception to raise."""

    returns: str | Exception = "Edited some files and ran tests."
    calls: list[TurnPayload] = field(default_factory=list)

    def generate(self, turn: TurnPayload) -> str:
        self.calls.append(turn)
        if isinstance(self.returns, Exception):
            raise self.returns
        return self.returns

    @property
    def call_count(self) -> int:
        return len(self.calls)


# ---------- Summarizer ----------


@dataclass
class FakeSummarizer:
    """In-memory Summarizer. ``returns`` is a string OR exception."""

    returns: str | Exception = "Short version."
    calls: list[tuple[str, int | None]] = field(default_factory=list)

    def summarize(self, message: str, input_words: int | None = None) -> str:
        self.calls.append((message, input_words))
        if isinstance(self.returns, Exception):
            raise self.returns
        return self.returns

    @property
    def call_count(self) -> int:
        return len(self.calls)


# ---------- TTS ----------


@dataclass
class FakeTTS:
    """Records each speak call. ``raises`` triggers TTSFailed propagation.

    ``metrics`` is the dict :class:`MacOSSay.speak` would return for
    a successful two-stage call; ``calls`` collects the spoken texts
    in order.
    """

    raises: TTSFailed | None = None
    metrics: dict[str, Any] | None = field(
        default_factory=lambda: {
            "say_path": "two_stage",
            "synth_elapsed_s": 0.05,
            "aiff_duration_s": 1.0,
            "afplay_elapsed_s": 1.0,
            "expected_say_s": 1.0,
        }
    )
    calls: list[str] = field(default_factory=list)

    def speak(self, text: str) -> dict[str, Any] | None:
        self.calls.append(text)
        if self.raises is not None:
            raise self.raises
        if not text.strip():
            return None
        return self.metrics


# ---------- StateStore ----------


@dataclass
class InMemoryStateStore:
    """Dict-backed StateStore. Key is ``(session_id, cwd)``; value is bool."""

    initial: dict[tuple[str, str], bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._store: dict[tuple[str, str], bool] = dict(self.initial)

    def load(self, session_id: str, cwd: str, *, default_enabled: bool = False) -> State:
        if (session_id, cwd) in self._store:
            return State(enabled=self._store[(session_id, cwd)])
        return State(enabled=default_enabled)

    def save(self, state: State, session_id: str, cwd: str) -> None:
        self._store[(session_id, cwd)] = state.enabled

    @property
    def saves(self) -> dict[tuple[str, str], bool]:
        return dict(self._store)


# ---------- NarrationCache ----------


@dataclass
class InMemoryNarrationCache:
    """Dict-backed cache keyed by session_id."""

    initial: dict[str, tuple[str | None, str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._store: dict[str, tuple[str | None, str]] = dict(self.initial)
        self.writes: list[tuple[str, str | None, str]] = []
        self.reads: list[str] = []

    def write(self, session_id: str, recap: str | None, message: str) -> None:
        self.writes.append((session_id, recap, message))
        self._store[session_id] = (recap, message)

    def read(self, session_id: str) -> tuple[str | None, str] | None:
        self.reads.append(session_id)
        return self._store.get(session_id)


# ---------- EventLog ----------


@dataclass
class FakeEventLog:
    """Captures every emit in memory.

    Tests assert on the ``events`` list to verify per-fire telemetry
    fields. ``messages`` collects free-form INFO lines (claude -p
    failure warnings, TTS-failed lines, etc.); ``traces`` collects
    TRACE-level emits so trace-content tests can introspect.
    """

    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    traces: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    trace_messages: list[str] = field(default_factory=list)
    fire_deltas: dict[str, list[int | None]] = field(default_factory=dict)

    def event(self, event: str, **fields: Any) -> None:
        self.events.append((event, dict(fields)))

    def message(self, text: str) -> None:
        self.messages.append(text)

    def event_trace(self, event: str, **fields: Any) -> None:
        self.traces.append((event, dict(fields)))

    def message_trace(self, text: str) -> None:
        self.trace_messages.append(text)

    def record_fire_delta(self, event: str) -> int | None:
        # Default: first call returns None, subsequent return 0 ms.
        history = self.fire_deltas.setdefault(event, [])
        delta = None if not history else 0
        history.append(delta)
        return delta


# ---------- TranscriptReader ----------


@dataclass
class FakeTranscriptReader:
    """Returns a canned TurnPayload (or ``None``) for any (session_id, cwd)."""

    turn: TurnPayload | None = None

    def load(self, session_id: str, cwd: str) -> TurnPayload | None:
        return self.turn


# ---------- subprocess fakes ----------


def completed(
    argv: list[str], *, returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Shorthand for the ``CompletedProcess`` a :class:`FakeProcessRunner` handler returns."""

    return subprocess.CompletedProcess(
        args=argv, returncode=returncode, stdout=stdout, stderr=stderr
    )


def audio_handlers(*, afinfo_duration_s: float = 999.0) -> dict[str, Any]:
    """say / afinfo / afplay handlers for a :class:`FakeProcessRunner`.

    All three succeed; ``afinfo`` reports ``afinfo_duration_s`` (huge by
    default so the short-aiff fallback gate doesn't trip in tests not
    exercising it). Spread into a runner alongside a ``claude`` handler
    for the recap/summary path:
    ``FakeProcessRunner({**audio_handlers(), "claude": claude})``.
    """

    def say(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return completed(argv)

    def afinfo(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return completed(argv, stdout=f"estimated duration: {afinfo_duration_s:.3f} sec")

    def afplay(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return completed(argv)

    return {"say": say, "afinfo": afinfo, "afplay": afplay}


# ---------- ProcessRunner ----------


class FakeProcessRunner:
    """Dispatches argv shapes to canned :class:`subprocess.CompletedProcess`.

    Construct with handler callables keyed by argv[0] (the binary
    name). Each handler receives the full argv plus ``input`` /
    ``timeout`` kwargs and returns either a ``CompletedProcess`` or
    raises (e.g. :class:`subprocess.TimeoutExpired` /
    :class:`ProcessFailed`).

    Convenience constructors:

    - ``with_claude_p(stdout=, returncode=, raises=)`` — single-binary
      shape for unit tests of recap / summarizer.
    - ``with_say(...)`` — covers the four say-binary shapes
      (say -o / say -- / afinfo / afplay) used by ``MacOSSay``.
    """

    def __init__(
        self,
        handlers: dict[str, Any] | None = None,
        *,
        default: Any = None,
    ) -> None:
        self._handlers: dict[str, Any] = dict(handlers or {})
        self._default = default
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(
        self,
        argv: list[str],
        *,
        input: str | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), {"input": input, "timeout": timeout}))
        if not argv:
            raise AssertionError("FakeProcessRunner: empty argv")
        binary = argv[0]
        handler = self._handlers.get(binary, self._default)
        if handler is None:
            raise AssertionError(f"FakeProcessRunner: no handler for {binary!r}")
        result = handler(argv, input=input, timeout=timeout)
        if isinstance(result, BaseException):
            raise result
        return result

    @classmethod
    def with_claude_p(
        cls,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        raises: BaseException | None = None,
    ) -> FakeProcessRunner:
        """Single-binary fake for tests of ``ClaudePRecap`` / ``ClaudePSummarizer``."""

        def handler(
            argv: list[str], *, input: str | None, timeout: float | None
        ) -> subprocess.CompletedProcess[str] | BaseException:
            if raises is not None:
                return raises
            return subprocess.CompletedProcess(
                args=argv, returncode=returncode, stdout=stdout, stderr=stderr
            )

        return cls({"claude": handler})

    @classmethod
    def with_say_default(cls, *, afinfo_duration_s: float = 999.0) -> FakeProcessRunner:
        """Default fake for ``MacOSSay`` — say / afinfo / afplay all succeed."""

        return cls(audio_handlers(afinfo_duration_s=afinfo_duration_s))


# ---------- Services builder ----------


def build_services(
    *,
    config: Config | None = None,
    recap_primary: Any | None = None,
    recap_fallback: Any | None = None,
    summarizer: Any | None = None,
    tts: Any | None = None,
    state: Any | None = None,
    cache: Any | None = None,
    eventlog: Any | None = None,
    transcript_reader: Any | None = None,
) -> Services:
    """Construct a :class:`Services` with fakes filling unspecified slots.

    Defaults:
    - ``config`` → :meth:`Config.default`
    - ``recap_primary`` → :class:`FakeRecap` with the standard canned line
    - ``recap_fallback`` → :class:`FakeRecap` returning a deterministic
      fallback line so tests can distinguish primary vs fallback paths
    - ``summarizer`` → :class:`FakeSummarizer`
    - ``tts`` → :class:`FakeTTS` (records calls; doesn't raise)
    - ``state`` → empty :class:`InMemoryStateStore`
    - ``cache`` → empty :class:`InMemoryNarrationCache`
    - ``eventlog`` → :class:`FakeEventLog`
    - ``transcript_reader`` → :class:`FakeTranscriptReader` (returns ``None``)
    """

    return Services(
        config=config if config is not None else Config.default(),
        recap_primary=recap_primary if recap_primary is not None else FakeRecap(),
        recap_fallback=recap_fallback
        if recap_fallback is not None
        else FakeRecap(returns="Read a file, edited a file, ran `pytest`."),
        summarizer=summarizer if summarizer is not None else FakeSummarizer(),
        tts=tts if tts is not None else FakeTTS(),
        state=state if state is not None else InMemoryStateStore(),
        cache=cache if cache is not None else InMemoryNarrationCache(),
        eventlog=eventlog if eventlog is not None else FakeEventLog(),
        transcript_reader=transcript_reader
        if transcript_reader is not None
        else FakeTranscriptReader(),
    )


# ---------- real-component integration graph ----------
#
# ``build_services`` (above) fills the graph with fakes. The helpers
# below wire the *real* production components — Pipeline, ClaudePRecap,
# RuleBasedRecap, MacOSSay, FileStateStore, FileNarrationCache,
# FileTranscriptReader, FileEventLog — against a :class:`FakeProcessRunner`
# (no subprocesses) and tmp-rooted paths (no ~/.claude pollution).
# Integration tests (hook / repeat / command end-to-end) use these.
# ``Services.from_config`` is the composition root; passing a tmp
# ``audio_recap_root`` (plus the CC transcript root) is how a test
# isolates the filesystem.


def cache_root(tmp_path: Path) -> Path:
    """Narration-cache root under a test's ``tmp_path``."""

    return tmp_path / "cache"


def projects_root(tmp_path: Path) -> Path:
    """State-store projects root under a test's ``tmp_path``."""

    return tmp_path / "projects"


def transcript_root(tmp_path: Path) -> Path:
    """CC-transcript root under a test's ``tmp_path``."""

    return tmp_path / "cc_projects"


def event_log_path(tmp_path: Path) -> Path:
    """Event-log path under a test's ``tmp_path``.

    Matches what ``Services.from_config`` derives from
    ``audio_recap_root=tmp_path`` — and the ``log_path`` conftest fixture.
    """

    return tmp_path / "logs" / "audio-recap.log"


def real_services(
    cwd: str,
    tmp_path: Path,
    *,
    runner: ProcessRunner,
    session_id: str = "test-sid",
    eventlog: Any = None,
) -> Services:
    """Production component graph wired to ``runner`` + tmp-rooted paths.

    The real :meth:`Services.from_config` runs (so per-cwd
    ``.audio-recap/config.json`` overrides under ``cwd`` apply), but
    every subprocess call goes through the injected ``runner`` and the
    plugin's storage (log / cache / state) is rooted under ``tmp_path``.
    """

    return Services.from_config(
        cwd,
        session_id=session_id,
        runner=runner,
        eventlog=eventlog,
        audio_recap_root=tmp_path,
        transcript_root=transcript_root(tmp_path),
    )


def raw_payload(payload: dict[str, Any]) -> bytes:
    """Serialize a hook payload dict to the bytes ``hook.main`` accepts."""

    return json.dumps(payload).encode("utf-8")


def seed_enabled(tmp_path: Path, payload: dict[str, Any]) -> None:
    """Pre-seed ``enabled=True`` state for the payload's (session_id, cwd).

    Mirrors :class:`audio_recap.payload.PayloadParser`'s defaults —
    missing ``session_id`` → ``"_global"``, missing ``cwd`` → ``""`` — so
    the seed lands at the path the hook reads from, under the tmp-rooted
    projects store.
    """

    sid = payload.get("session_id") or "_global"
    cwd = payload.get("cwd") or ""
    FileStateStore(projects_root(tmp_path)).save(State(enabled=True), sid, cwd)


__all__ = [
    "FakeEventLog",
    "FakeProcessRunner",
    "FakeRecap",
    "FakeSummarizer",
    "FakeTTS",
    "FakeTranscriptReader",
    "InMemoryNarrationCache",
    "InMemoryStateStore",
    "ProcessFailed",
    "RecapFailed",
    "SummarizerFailed",
    "TTSFailed",
    "audio_handlers",
    "build_services",
    "cache_root",
    "completed",
    "event_log_path",
    "projects_root",
    "raw_payload",
    "real_services",
    "seed_enabled",
    "transcript_root",
]
