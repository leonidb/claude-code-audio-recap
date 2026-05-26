from __future__ import annotations

import json
from pathlib import Path

import pytest

from audio_recap.state import FileStateStore, State, _encode_cwd, _path_for

# All tests root a ``FileStateStore`` at ``tmp_path`` so the real
# ~/.claude/audio-recap tree is never touched. Corrupt-file tests
# compute the on-disk path via ``_path_for`` and write the bad file
# there directly, then read it back through the store.

SID = "sess1"
CWD = "/x"  # kept for call-site compatibility; ignored by FileStateStore since v0.1.1


def _seed(tmp_path: Path, body: str, *, session_id: str = SID, cwd: str = CWD) -> Path:
    """Write ``body`` at the state path the store will read for session_id."""

    path = _path_for(session_id, tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_missing_file_defaults_to_disabled(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path)
    assert store.load(SID, CWD) == State(enabled=False)


def test_load_enabled_false(tmp_path: Path) -> None:
    _seed(tmp_path, '{"enabled": false}')
    assert FileStateStore(tmp_path).load(SID, CWD) == State(enabled=False)


def test_load_enabled_true(tmp_path: Path) -> None:
    _seed(tmp_path, '{"enabled": true}')
    assert FileStateStore(tmp_path).load(SID, CWD) == State(enabled=True)


def test_missing_enabled_key_is_lenient(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(tmp_path, '{"version": 1}')
    assert FileStateStore(tmp_path).load(SID, CWD) == State(enabled=False)
    # Missing key is the lenient path — no stderr noise.
    assert capsys.readouterr().err == ""


def test_corrupt_json_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _seed(tmp_path, "{not: json")
    assert FileStateStore(tmp_path).load(SID, CWD) == State(enabled=False)
    captured = capsys.readouterr()
    assert "unreadable" in captured.err
    assert str(path) in captured.err


def test_top_level_not_object_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path, "[1, 2, 3]")
    assert FileStateStore(tmp_path).load(SID, CWD) == State(enabled=False)
    assert "not an object" in capsys.readouterr().err


def test_non_bool_enabled_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(tmp_path, '{"enabled": "yes"}')
    assert FileStateStore(tmp_path).load(SID, CWD) == State(enabled=False)
    assert "must be a bool" in capsys.readouterr().err


def test_integer_one_is_not_accepted_as_true(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # bool is a subclass of int; isinstance(1, bool) is False, so we
    # intentionally reject integers that happen to be truthy.
    _seed(tmp_path, '{"enabled": 1}')
    assert FileStateStore(tmp_path).load(SID, CWD) == State(enabled=False)
    assert "must be a bool" in capsys.readouterr().err


# ---------- 069: default_enabled keyword param ----------


def test_missing_file_with_default_enabled_true_returns_enabled(tmp_path: Path) -> None:
    assert FileStateStore(tmp_path).load(SID, CWD, default_enabled=True) == State(enabled=True)


def test_missing_file_with_default_enabled_false_returns_disabled(tmp_path: Path) -> None:
    # Same as the original missing-file behavior — the default value
    # of ``default_enabled`` is False, so call sites that don't pass
    # the kwarg keep their old behavior.
    assert FileStateStore(tmp_path).load(SID, CWD, default_enabled=False) == State(enabled=False)


def test_existing_enabled_false_overrides_default_enabled_true(tmp_path: Path) -> None:
    # Explicit ``/audio-recap:off`` (which writes ``{"enabled": false}``)
    # beats config-level default-on. Once a user has opted out for a
    # session, the persisted choice wins.
    _seed(tmp_path, '{"enabled": false}')
    assert FileStateStore(tmp_path).load(SID, CWD, default_enabled=True) == State(enabled=False)


def test_existing_enabled_true_with_default_enabled_false(tmp_path: Path) -> None:
    # Symmetric: existing-file true survives even when config default
    # is false (the default for the config field itself).
    _seed(tmp_path, '{"enabled": true}')
    assert FileStateStore(tmp_path).load(SID, CWD, default_enabled=False) == State(enabled=True)


def test_corrupt_json_returns_disabled_regardless_of_default_enabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Corrupt files MUST surface as silence — a user with a broken
    # state file shouldn't be misled into thinking narration is live
    # because a config flag is on.
    _seed(tmp_path, "{not: json")
    assert FileStateStore(tmp_path).load(SID, CWD, default_enabled=True) == State(enabled=False)
    assert "unreadable" in capsys.readouterr().err


def test_missing_enabled_key_returns_disabled_regardless_of_default_enabled(
    tmp_path: Path,
) -> None:
    # Wrong-shape file (well-formed JSON object missing the key) also
    # falls back to False — the file exists, so we're past the
    # ``FileNotFoundError`` branch where the default applies.
    _seed(tmp_path, '{"version": 1}')
    assert FileStateStore(tmp_path).load(SID, CWD, default_enabled=True) == State(enabled=False)


def test_non_bool_enabled_returns_disabled_regardless_of_default_enabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(tmp_path, '{"enabled": "yes"}')
    assert FileStateStore(tmp_path).load(SID, CWD, default_enabled=True) == State(enabled=False)
    assert "must be a bool" in capsys.readouterr().err


# ---------- save/load round trip ----------


def test_save_then_load_round_trip_false(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path)
    store.save(State(enabled=False), SID, CWD)
    assert store.load(SID, CWD) == State(enabled=False)


def test_save_then_load_round_trip_true(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path)
    store.save(State(enabled=True), SID, CWD)
    assert store.load(SID, CWD) == State(enabled=True)


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    # Fresh root: the state_root directory doesn't exist yet.
    store = FileStateStore(tmp_path / "nested" / "deeper")
    store.save(State(enabled=True), SID, CWD)
    path = _path_for(SID, tmp_path / "nested" / "deeper")
    assert path.exists()
    with path.open(encoding="utf-8") as f:
        assert json.load(f) == {"enabled": True}


def test_save_overwrites_existing(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path)
    store.save(State(enabled=True), SID, CWD)
    store.save(State(enabled=False), SID, CWD)
    assert store.load(SID, CWD) == State(enabled=False)


def test_save_leaves_no_stray_temp_files(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path)
    store.save(State(enabled=True), SID, CWD)
    path = _path_for(SID, tmp_path)
    siblings = sorted(x.name for x in path.parent.iterdir())
    assert siblings == [path.name]


def test_state_dataclass_defaults_to_disabled() -> None:
    assert State().enabled is False


# ---------- per-session keying ----------


def test_two_sessions_are_isolated(tmp_path: Path) -> None:
    """Two different sessions write to distinct files regardless of cwd."""

    store = FileStateStore(tmp_path)
    store.save(State(enabled=True), "sess-a", "/proj")
    store.save(State(enabled=False), "sess-b", "/proj")
    assert store.load("sess-a", "/proj") == State(enabled=True)
    assert store.load("sess-b", "/proj") == State(enabled=False)


def test_path_layout_is_state_root_session_id_json(tmp_path: Path) -> None:
    """State file lives directly at state_root/<session-id>.json (no cwd subdir)."""

    p = _path_for("abc-123", tmp_path)
    assert p == tmp_path / "abc-123.json"


def test_same_session_different_cwd_shares_one_file(tmp_path: Path) -> None:
    """cwd changes within a session do not create separate state files."""

    store = FileStateStore(tmp_path)
    store.save(State(enabled=True), "sess-x", "/dir-a")
    # A load from a different cwd still reads the same enabled state.
    assert store.load("sess-x", "/dir-b") == State(enabled=True)


def test_global_sentinel_lives_outside_state_dir(tmp_path: Path) -> None:
    """``_global`` is the runtime-sentinel fallback; it escapes ``state/``."""

    p = _path_for("_global", tmp_path / "state")
    assert p == tmp_path / "_global" / "state.json"


# ---------- _encode_cwd ----------


def test_encode_cwd_replaces_separators_with_dashes() -> None:
    assert _encode_cwd("/Users/dev/proj") == "-Users-dev-proj"


def test_encode_cwd_resolves_symlinks_when_possible(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    # Resolved encoding matches the target's resolved path, not the link's.
    assert _encode_cwd(str(link)) == _encode_cwd(str(target))


def test_encode_cwd_handles_empty_cwd() -> None:
    assert _encode_cwd("") == "_unknown-cwd"


def test_encode_cwd_strips_unsafe_characters() -> None:
    """Even if a cwd contains unusual chars, the encoded form stays safe."""

    encoded = _encode_cwd("/proj name with$weird")
    # Must not contain raw ``/`` or shell-meta characters.
    assert "/" not in encoded
    assert "$" not in encoded
