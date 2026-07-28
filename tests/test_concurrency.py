"""Concurrency tests — the coordination actually serialises under contention.

A concurrency feature needs concurrency tests. These drive the REAL
:class:`audio_recap.lock.FcntlPlaybackLock` and
:class:`audio_recap.presence.FilePresenceRegistry` from multiple threads (each
thread standing in for a separate CC session, with its own lock fd) against
tmp paths — never the real ``~/.claude`` tree.

``fcntl.flock`` locks are tied to the open file description, so two separate
``open()`` calls contend even within one process — which is exactly how two
short-lived hook processes behave on one machine.
"""

from __future__ import annotations

import threading
from pathlib import Path

from audio_recap.lock import FcntlPlaybackLock, NullPlaybackLock
from audio_recap.presence import FilePresenceRegistry


def test_flock_serialises_playback_across_threads(tmp_path: Path) -> None:
    """Concurrent holders never overlap — max observed concurrency is 1."""
    lockfile = tmp_path / "playback.lock"

    counter_lock = threading.Lock()
    concurrent = 0
    max_concurrent = 0
    start = threading.Barrier(4)

    def worker() -> None:
        nonlocal concurrent, max_concurrent
        lock = FcntlPlaybackLock(lockfile)
        start.wait()  # release all workers at once to force contention
        for _ in range(5):
            outcome = lock.acquire(timeout_s=10.0)
            assert outcome == "acquired"
            try:
                with counter_lock:
                    concurrent += 1
                    max_concurrent = max(max_concurrent, concurrent)
                # Hold the critical section briefly so overlap would be observable.
                threading.Event().wait(0.005)
                with counter_lock:
                    concurrent -= 1
            finally:
                lock.release()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not any(t.is_alive() for t in threads), "a worker hung — lock deadlocked"
    assert max_concurrent == 1, f"playback overlapped: max_concurrent={max_concurrent}"


def test_acquire_times_out_when_held_then_frees_on_release(tmp_path: Path) -> None:
    """Bounded wait returns 'timeout' when contended, and release() truly frees it.

    Regression guard for the daemon-thread leak: the old design could leave a
    lock held (with _held=False) after a timeout, so release() no-op'd and the
    lock never freed. Here, after the holder releases, a waiter MUST be able to
    acquire — proving release() actually issued LOCK_UN.
    """
    lockfile = tmp_path / "playback.lock"
    a = FcntlPlaybackLock(lockfile)
    b = FcntlPlaybackLock(lockfile)

    assert a.acquire(timeout_s=1.0) == "acquired"
    # B can't acquire while A holds → bounded timeout, no hang.
    assert b.acquire(timeout_s=0.3) == "timeout"

    a.release()  # if release() no-op'd (the old bug), the next acquire would time out
    assert b.acquire(timeout_s=1.0) == "acquired"
    b.release()


def test_timed_out_acquire_does_not_hold_the_lock(tmp_path: Path) -> None:
    """A timed-out acquire leaves the lock with the original holder, not the waiter."""
    lockfile = tmp_path / "playback.lock"
    a = FcntlPlaybackLock(lockfile)
    b = FcntlPlaybackLock(lockfile)

    assert a.acquire(timeout_s=1.0) == "acquired"
    assert b.acquire(timeout_s=0.2) == "timeout"
    b.release()  # no-op — B never held it

    # A still holds, so a fresh contender still can't acquire quickly.
    c = FcntlPlaybackLock(lockfile)
    assert c.acquire(timeout_s=0.2) == "timeout"
    a.release()


def test_null_playback_lock_is_fail_open(tmp_path: Path) -> None:
    """NullPlaybackLock always proceeds ('unavailable') and release() never raises."""
    lock = NullPlaybackLock()
    assert lock.acquire(timeout_s=1.0) == "unavailable"
    lock.release()  # no-op, must not raise


def test_second_session_sees_the_first_as_active(tmp_path: Path) -> None:
    """Real registry: two live sessions each count the other as active."""
    reg = FilePresenceRegistry(tmp_path / "active")
    reg.heartbeat("session-a")
    reg.heartbeat("session-b")

    assert reg.active_others("session-a", window_s=900) == 1
    assert reg.active_others("session-b", window_s=900) == 1


def test_closed_session_stops_counting_after_remove(tmp_path: Path) -> None:
    """SessionEnd delete drops the closed session from the live count."""
    reg = FilePresenceRegistry(tmp_path / "active")
    reg.heartbeat("session-a")
    reg.heartbeat("session-b")
    assert reg.active_others("session-a", window_s=900) == 1

    reg.remove("session-b")  # session B closes cleanly (SessionEnd)
    assert reg.active_others("session-a", window_s=900) == 0


def test_concurrent_heartbeats_are_thread_safe(tmp_path: Path) -> None:
    """Many sessions heartbeating at once → each other session is counted once."""
    reg = FilePresenceRegistry(tmp_path / "active")
    ids = [f"session-{i}" for i in range(12)]
    start = threading.Barrier(len(ids))

    def beat(sid: str) -> None:
        start.wait()
        reg.heartbeat(sid)

    threads = [threading.Thread(target=beat, args=(sid,)) for sid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    # Every session sees the other 11 as live.
    assert reg.active_others("session-0", window_s=900) == 11
