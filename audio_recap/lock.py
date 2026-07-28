"""Per-user advisory playback lock.

Serialises audio output across concurrent Claude Code sessions belonging to
the same user. One process holds ``LOCK_EX`` on ``<audio_recap_root>/
playback.lock``; others block until the holder releases it (or dies). Because
the default root lives under ``$HOME`` (``~/.claude/audio-recap``), the lock
is per-user by construction — two humans on one Mac never serialise against
each other.

``fcntl.flock`` auto-releases when the fd is closed on process exit or crash,
so a killed session cannot wedge the queue and no stale-lock GC is needed.

Design points:

- **Bounded wait.** :meth:`FcntlPlaybackLock.acquire` blocks at most
  ``timeout_s`` (default 120s). On timeout it returns ``"timeout"`` and the
  caller plays ANYWAY — an occasional overlap beats a hung/silent session.
  The generous cap barely fires in practice (flock releases on death).
- **Fail-open.** If the lockfile can't be created (unwritable path, network
  FS, container quirks) the composition root substitutes
  :class:`NullPlaybackLock`, which always proceeds. If ``flock`` itself
  raises (e.g. ``ENOLCK`` on an unsupported FS) :meth:`acquire` returns
  ``"error"`` immediately rather than waiting out the timeout. Playback never
  goes silent because of the lock.
- **dry_run skips the lock entirely** — no playback happens, so there is
  nothing to serialise (handled by the caller, not here).

The lock is a DI seam: :class:`PlaybackLock` Protocol + :class:`FcntlPlaybackLock`
(prod) + :class:`NullPlaybackLock` (fail-open) + ``FakePlaybackLock`` (tests,
in ``tests/fakes.py``).
"""

from __future__ import annotations

import contextlib
import fcntl
import time
from pathlib import Path
from typing import Literal, Protocol

# Result of an acquire attempt:
# - "acquired"    — lock held; caller must call release() when done.
# - "timeout"     — bounded wait elapsed; caller plays anyway (overlap risk).
# - "error"       — flock raised (unsupported FS etc.); caller plays anyway.
# - "unavailable" — NullPlaybackLock fallback; caller plays anyway.
AcquireOutcome = Literal["acquired", "timeout", "error", "unavailable"]

# Poll cadence for the bounded blocking wait. Polling a non-blocking LOCK_EX
# (rather than a background blocking flock) keeps lock ownership on the single
# calling thread, so a timed-out acquire can never leak a lock that a
# background thread grabbed after the caller gave up. 100ms adds negligible
# handoff latency against multi-second narrations and the queue is only
# "roughly in order" by design.
_POLL_INTERVAL_S = 0.1


class PlaybackLock(Protocol):
    """Seam for the per-user audio serialisation lock.

    Production: :class:`FcntlPlaybackLock` (or :class:`NullPlaybackLock` when
    the lockfile can't be created). Tests: ``FakePlaybackLock``.
    """

    def acquire(self, timeout_s: float = 120.0) -> AcquireOutcome:
        """Block until acquired or *timeout_s* elapses.

        Returns one of :data:`AcquireOutcome`. Only ``"acquired"`` means the
        lock is held; every other outcome tells the caller to proceed anyway
        (fail-open) — never to go silent.
        """
        ...

    def release(self) -> None:
        """Release the lock if held; safe to call when not held."""
        ...


class FcntlPlaybackLock:
    """Production lock backed by :func:`fcntl.flock` on a shared lockfile.

    The fd is opened at construction and held for the process lifetime;
    ``flock`` ties to the fd, so closing it (on exit, crash, or
    :meth:`__del__`) releases the lock automatically. Construction may raise
    ``OSError`` if the lockfile can't be created — the composition root
    catches that and falls back to :class:`NullPlaybackLock`.
    """

    def __init__(self, lockfile: Path) -> None:
        lockfile.parent.mkdir(parents=True, exist_ok=True)
        # Append mode: creates the file if missing, never truncates.
        self._fd = open(lockfile, "a")  # noqa: SIM115
        self._held = False

    def acquire(self, timeout_s: float = 120.0) -> AcquireOutcome:
        """LOCK_EX with a bounded wait; fail-open on flock error.

        Polls a non-blocking ``LOCK_EX`` until it succeeds or the deadline
        passes. Polling (rather than a background blocking flock) keeps
        ownership on the single calling thread: the lock is held only when
        this returns ``"acquired"``, so :meth:`release` is always correct and
        a timed-out acquire can never leak a lock a background thread grabbed
        after the caller gave up. Blocking the calling thread here is intended
        — the short-lived hook process should wait for its turn to play.

        Returns ``"acquired"`` on success, ``"timeout"`` if the wait elapses
        (caller plays anyway), or ``"error"`` if ``flock`` is unsupported on
        this fd/FS (e.g. ``ENOLCK`` on a network mount — fail open at once
        rather than waiting out the whole timeout).
        """
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._held = True
                return "acquired"
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return "timeout"
                time.sleep(_POLL_INTERVAL_S)
            except OSError:
                return "error"

    def release(self) -> None:
        """Release the lock if held; no-op otherwise."""
        if self._held:
            with contextlib.suppress(OSError):
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            self._held = False

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self._fd.close()


class NullPlaybackLock:
    """Fail-open no-op lock — always proceeds, never serialises.

    Substituted by the composition root when the real lockfile can't be
    created (unwritable path, unusual FS). Keeps narration audible: better an
    occasional overlap than a silenced session.
    """

    def acquire(self, timeout_s: float = 120.0) -> AcquireOutcome:
        return "unavailable"

    def release(self) -> None:
        return None


__all__ = ["AcquireOutcome", "FcntlPlaybackLock", "NullPlaybackLock", "PlaybackLock"]
