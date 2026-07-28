"""Tests for the file-backed cross-session presence registry.

Exercise the real :class:`audio_recap.presence.FilePresenceRegistry` against a
tmp ``active/`` dir (never the real ``~/.claude`` tree): heartbeat write /
rewrite, delete, counting other open sessions, self-exclusion, orphan
collection, and fail-silent degradation.

Who *may* write a heartbeat is the Stop hook's business, not the registry's —
see ``tests/test_hook.py`` and ``tests/test_e2e_multi_session.py``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from audio_recap.presence import FilePresenceRegistry


def _registry(tmp_path: Path) -> FilePresenceRegistry:
    return FilePresenceRegistry(tmp_path / "active")


def test_heartbeat_creates_file(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.heartbeat("sid-a")
    assert (tmp_path / "active" / "sid-a").exists()


def test_heartbeat_refreshes_mtime(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.heartbeat("sid-a")
    path = tmp_path / "active" / "sid-a"
    old = path.stat().st_mtime
    # Backdate, then re-heartbeat and confirm mtime advanced.
    os.utime(path, (old - 1000, old - 1000))
    reg.heartbeat("sid-a")
    assert path.stat().st_mtime > old - 1000


def test_remove_deletes_heartbeat(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.heartbeat("sid-a")
    reg.remove("sid-a")
    assert not (tmp_path / "active" / "sid-a").exists()


def test_remove_missing_is_noop(tmp_path: Path) -> None:
    _registry(tmp_path).remove("never-existed")  # must not raise


def test_active_others_counts_other_sessions(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.heartbeat("sid-a")
    reg.heartbeat("sid-b")
    reg.heartbeat("sid-c")
    assert reg.active_others("sid-a", window_s=900) == 2


def test_active_others_excludes_self(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.heartbeat("sid-a")
    assert reg.active_others("sid-a", window_s=900) == 0


def test_active_others_solo_is_zero(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.heartbeat("sid-a")
    assert reg.active_others("sid-a", window_s=900) == 0


def test_stale_heartbeat_not_counted_and_pruned(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.heartbeat("sid-a")
    reg.heartbeat("sid-stale")
    stale = tmp_path / "active" / "sid-stale"
    now = stale.stat().st_mtime
    os.utime(stale, (now - 100_000, now - 100_000))  # far beyond the window

    assert reg.active_others("sid-a", window_s=900) == 0
    # Lazy crash-GC removed the stale file during the scan.
    assert not stale.exists()


def test_heartbeat_past_the_horizon_is_collected(tmp_path: Path) -> None:
    """Past the orphan horizon a heartbeat is both uncounted and deleted.

    A heartbeat is retired by ``/audio-recap:off`` or SessionEnd in every
    ordinary case, so one that outlives its horizon belongs to a hard-killed
    session and there is nothing to preserve it for. Pruning at exactly the
    horizon rather than at a multiple of it is what makes the two agree: a file
    that stopped counting is a file that is gone.
    """
    reg = _registry(tmp_path)
    reg.heartbeat("sid-a")
    reg.heartbeat("sid-b")
    b = tmp_path / "active" / "sid-b"
    now = b.stat().st_mtime
    os.utime(b, (now - 150, now - 150))  # 150s old against a 100s horizon

    assert reg.active_others("sid-a", window_s=100) == 0
    assert not b.exists()


def test_fresh_within_window_survives(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.heartbeat("sid-a")
    reg.heartbeat("sid-b")
    b = tmp_path / "active" / "sid-b"
    now = b.stat().st_mtime
    os.utime(b, (now - 100, now - 100))  # 100s old, well inside the horizon
    assert reg.active_others("sid-a", window_s=900) == 1
    assert b.exists()


def test_active_others_missing_dir_returns_zero(tmp_path: Path) -> None:
    # No heartbeat ever written → active/ doesn't exist → degrade to 0 (solo).
    reg = _registry(tmp_path)
    assert reg.active_others("sid-a", window_s=900) == 0


def test_empty_session_id_heartbeat_is_noop(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.heartbeat("")
    # No stray file created for an empty id.
    assert not (tmp_path / "active").exists() or list((tmp_path / "active").iterdir()) == []


def test_ds_store_is_not_counted_as_a_live_session(tmp_path: Path) -> None:
    """macOS drops a fresh .DS_Store into any folder Finder opens.

    Counting it would label a genuinely solo session — the one thing the
    presence design promises it will never do.
    """
    reg = _registry(tmp_path)
    reg.heartbeat("sid-a")
    (tmp_path / "active" / ".DS_Store").write_bytes(b"\x00\x01")

    assert reg.active_others("sid-a", window_s=900) == 0


def test_stale_dot_file_is_not_pruned(tmp_path: Path) -> None:
    """Not ours to delete: the prune must leave a foreign dotfile alone."""
    reg = _registry(tmp_path)
    reg.heartbeat("sid-a")
    ds_store = tmp_path / "active" / ".DS_Store"
    ds_store.write_bytes(b"\x00\x01")
    old = time.time() - 10_000  # far beyond the orphan horizon
    os.utime(ds_store, (old, old))

    reg.active_others("sid-a", window_s=900)

    assert ds_store.exists()


def test_subdirectory_is_not_counted_as_a_live_session(tmp_path: Path) -> None:
    """Only regular files are heartbeats."""
    reg = _registry(tmp_path)
    reg.heartbeat("sid-a")
    (tmp_path / "active" / "somedir").mkdir()

    assert reg.active_others("sid-a", window_s=900) == 0
