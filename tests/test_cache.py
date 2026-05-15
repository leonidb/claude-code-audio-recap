"""Per-session narration cache: write/read round-trip + edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from audio_recap.cache import FileNarrationCache


@pytest.fixture
def cache(tmp_path: Path) -> FileNarrationCache:
    """An isolated cache rooted in the test's tmp dir."""

    return FileNarrationCache(root=tmp_path / "cache")


def test_write_then_read_round_trips(cache: FileNarrationCache) -> None:
    cache.write("sid-1", "Edited a file.", "Done — see the diff for details.")
    got = cache.read("sid-1")
    assert got == ("Edited a file.", "Done — see the diff for details.")


def test_read_missing_session_returns_none(cache: FileNarrationCache) -> None:
    assert cache.read("never-existed") is None


def test_write_overwrites_previous_entry(cache: FileNarrationCache) -> None:
    cache.write("sid-2", "first recap", "first message")
    cache.write("sid-2", "second recap", "second message")
    assert cache.read("sid-2") == ("second recap", "second message")


def test_recap_none_round_trips_as_none(cache: FileNarrationCache) -> None:
    # Skip-recap turns (no_tool_use, message_too_short, claude_p_failed +
    # rule_based_failed) cache an empty recap segment; the reader should
    # return None for that, not an empty string, so callers can branch
    # cleanly on "no recap was ever spoken."
    cache.write("sid-3", None, "message only")
    assert cache.read("sid-3") == (None, "message only")


def test_multi_line_message_survives_round_trip(cache: FileNarrationCache) -> None:
    message = "First sentence.\nSecond sentence with a -- in it.\nThird."
    cache.write("sid-4", "recap", message)
    assert cache.read("sid-4") == ("recap", message)


def test_cross_session_isolation(cache: FileNarrationCache) -> None:
    cache.write("session-A", "recap A", "message A")
    cache.write("session-B", "recap B", "message B")
    assert cache.read("session-A") == ("recap A", "message A")
    assert cache.read("session-B") == ("recap B", "message B")


def test_path_is_under_root(tmp_path: Path) -> None:
    cache = FileNarrationCache(root=tmp_path / "cache_root")
    path = cache.path("sid-x")
    assert path.parent == tmp_path / "cache_root"
    assert path.name == "sid-x.txt"


def test_corrupt_cache_file_reads_as_miss(cache: FileNarrationCache) -> None:
    # A file that exists but doesn't carry the boundary line — e.g.
    # someone touched the file by hand or a partial write happened —
    # is treated as a miss, not a "cache hit with garbage data."
    path = cache.path("sid-corrupt")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("no boundary marker here", encoding="utf-8")
    assert cache.read("sid-corrupt") is None


def test_write_io_failure_does_not_raise() -> None:
    # If the cache root can't be created (permission, missing parent),
    # write returns silently. Same best-effort stance as eventlog.
    cache = FileNarrationCache(root=Path("/dev/null/this-is-not-a-dir"))
    cache.write("sid-y", "recap", "message")
