from __future__ import annotations

import json
from pathlib import Path

import pytest

from audio_recap import command
from audio_recap.services import Services
from audio_recap.state import FileStateStore, State
from tests.fakes import FakeProcessRunner, real_services, state_root

# Every test injects a ``Services`` whose ``FileStateStore`` is rooted
# under ``tmp_path`` so the real ~/.claude/audio-recap tree is never
# touched. The ``store`` fixture shares that same root, so a test can
# drive ``command.main`` and then read state back through ``store``.

SID = "sess-test"
CWD = "/proj"


@pytest.fixture
def store(tmp_path: Path) -> FileStateStore:
    """A FileStateStore rooted at the same state dir the injected Services uses."""

    return FileStateStore(state_root(tmp_path))


@pytest.fixture
def services(tmp_path: Path) -> Services:
    """Production Services graph; cwd ``/proj`` (no per-cwd config → defaults)."""

    return real_services(CWD, tmp_path, runner=FakeProcessRunner())


def _run(services: Services, *argv: str) -> int:
    """Invoke command.main with the SID/CWD flags in place by default."""

    return command.main(["command.py", "--session-id", SID, "--cwd", CWD, *argv], services=services)


def _run_no_flags(services: Services, *argv: str) -> int:
    """Invoke command.main without the SID/CWD flags (defaults path)."""

    return command.main(["command.py", *argv], services=services)


# ---------- each verb ----------


def test_on_persists_enabled_true(
    services: Services, store: FileStateStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(services, "on") == 0
    assert store.load(SID, CWD).enabled is True
    assert capsys.readouterr().out == "Audio Recap enabled.\n"


def test_off_persists_enabled_false(
    services: Services, store: FileStateStore, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(services, "off") == 0
    assert store.load(SID, CWD).enabled is False
    assert capsys.readouterr().out == "Audio Recap disabled.\n"


def test_status_reads_without_changing(
    services: Services, store: FileStateStore, capsys: pytest.CaptureFixture[str]
) -> None:
    store.save(State(enabled=False), SID, CWD)
    assert _run(services, "status") == 0
    assert store.load(SID, CWD).enabled is False
    assert capsys.readouterr().out == "Audio Recap is disabled.\n"


def test_status_when_enabled(
    services: Services, store: FileStateStore, capsys: pytest.CaptureFixture[str]
) -> None:
    store.save(State(enabled=True), SID, CWD)
    assert _run(services, "status") == 0
    assert capsys.readouterr().out == "Audio Recap is enabled.\n"


def test_status_default_off_for_fresh_session(
    services: Services, capsys: pytest.CaptureFixture[str]
) -> None:
    """No state file + no config override → ``status`` reads disabled."""

    assert _run(services, "status") == 0
    assert capsys.readouterr().out == "Audio Recap is disabled.\n"


# ---------- persistence across calls ----------


def test_on_then_status_sees_enabled(
    services: Services, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(services, "on") == 0
    capsys.readouterr()  # discard "enabled" line
    assert _run(services, "status") == 0
    assert capsys.readouterr().out == "Audio Recap is enabled.\n"


def test_off_then_on_round_trip(services: Services, store: FileStateStore) -> None:
    assert _run(services, "off") == 0
    assert store.load(SID, CWD).enabled is False
    assert _run(services, "on") == 0
    assert store.load(SID, CWD).enabled is True


def test_on_is_idempotent(services: Services, store: FileStateStore) -> None:
    """Calling ``on`` twice still leaves the state enabled."""

    assert _run(services, "on") == 0
    assert _run(services, "on") == 0
    assert store.load(SID, CWD).enabled is True


def test_off_is_idempotent(services: Services, store: FileStateStore) -> None:
    assert _run(services, "off") == 0
    assert _run(services, "off") == 0
    assert store.load(SID, CWD).enabled is False


# ---------- presence: off retires the session ----------


def test_off_removes_the_presence_heartbeat(services: Services, tmp_path: Path) -> None:
    """Symmetric with SessionEnd: switching narration off stops counting now.

    A heartbeat means "an enabled session that is open". Leaving one behind
    would keep other sessions announcing their names against a session that has
    been silenced on purpose — and since nothing ages it out on idleness, it
    would do so until that session closed.
    """
    services.presence_registry.heartbeat(SID)
    assert (tmp_path / "active" / SID).exists()

    assert _run(services, "off") == 0

    assert not (tmp_path / "active" / SID).exists()


def test_off_removes_only_this_sessions_heartbeat(services: Services, tmp_path: Path) -> None:
    """A session that never narrated turns off cleanly, and leaves others alone."""
    services.presence_registry.heartbeat("someone-else")

    assert _run(services, "off") == 0

    assert not (tmp_path / "active" / SID).exists()
    assert (tmp_path / "active" / "someone-else").exists()


def test_on_registers_the_session_immediately(services: Services, tmp_path: Path) -> None:
    """Switching narration on counts from that moment, not from the first recap.

    A heartbeat means "an enabled session that is open". Waiting for the first
    narration would leave two sessions switched on side by side unable to name
    each other until the SECOND of them had spoken — the first narration, when
    the listener has the least context about who is talking, would be the one
    that went unlabeled.
    """

    assert _run(services, "on") == 0

    assert (tmp_path / "active" / SID).exists()


# ---------- per-session keying ----------


def test_two_sessions_in_same_project_are_independent(
    services: Services, store: FileStateStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """Enabling /audio-recap:on for session A doesn't enable session B."""

    cmd = ["command.py", "--cwd", CWD]
    assert command.main([*cmd, "--session-id", "A", "on"], services=services) == 0
    capsys.readouterr()
    assert command.main([*cmd, "--session-id", "B", "status"], services=services) == 0
    assert capsys.readouterr().out == "Audio Recap is disabled.\n"
    assert store.load("A", CWD).enabled is True
    assert store.load("B", CWD).enabled is False


def test_default_session_id_is_global_sentinel(
    services: Services, store: FileStateStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --session-id, the command writes to the _global fallback."""

    assert _run_no_flags(services, "on") == 0
    capsys.readouterr()
    # The _global fallback is the runtime sentinel's namespace; reading
    # back via load("_global", any) should reflect the write.
    assert store.load("_global", "/anything").enabled is True


# ---------- invalid forms ----------


def test_unknown_verb_exits_2_with_usage(
    services: Services, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(services, "bogus") == 2
    captured = capsys.readouterr()
    assert "usage:" in captured.err
    assert captured.out == ""


def test_no_verb_exits_2(services: Services, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run_no_flags(services) == 2
    assert "usage:" in capsys.readouterr().err


def test_legacy_narrate_keyword_is_rejected(
    services: Services, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pre-V1 ``narrate on`` form is gone; new shape is bare ``on``."""

    assert _run(services, "narrate") == 2
    assert _run(services, "narrate", "on") == 2
    assert "usage:" in capsys.readouterr().err


def test_extra_args_exits_2(services: Services, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(services, "on", "extra") == 2
    assert "usage:" in capsys.readouterr().err


# ---------- flag parsing ----------


def test_session_id_flag_can_appear_after_cwd(services: Services, store: FileStateStore) -> None:
    """The flag parser is order-independent for --session-id and --cwd."""

    rc = command.main(["command.py", "--cwd", CWD, "--session-id", SID, "on"], services=services)
    assert rc == 0
    assert store.load(SID, CWD).enabled is True


def test_session_id_flag_without_value_falls_back_to_default(
    services: Services, capsys: pytest.CaptureFixture[str]
) -> None:
    """A trailing --session-id with no value is treated as a spurious arg."""

    # `["--session-id"]` at the tail (no following value) is treated as a
    # spurious arg; the parser leaves it in ``rest``, which then fails the
    # exact-one-verb check. Guards against truncated argv shapes.
    rc = command.main(["command.py", "on", "--session-id"], services=services)
    assert rc == 2
    assert "usage:" in capsys.readouterr().err


# ---------- 069: default_enabled config field ----------


def _write_default_enabled_config(cwd: Path, value: bool) -> None:
    """Drop a per-cwd config that toggles ``default_enabled``."""

    cfg = cwd / ".audio-recap" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"default_enabled": value}), encoding="utf-8")


def test_status_reports_enabled_when_default_enabled_and_no_state_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``status`` returns the *effective* state, not just file-or-default-off.

    A user with ``default_enabled: true`` and no prior toggle should
    see the truthful "enabled" report — otherwise the config flag
    appears dead until they explicitly set on/off once.
    """

    cwd = tmp_path / "proj"
    cwd.mkdir()
    _write_default_enabled_config(cwd, True)
    # Services built with the config cwd so ``config.default_enabled`` is True.
    services = real_services(str(cwd), tmp_path, runner=FakeProcessRunner())

    rc = command.main(
        ["command.py", "--session-id", "fresh-sid", "--cwd", str(cwd), "status"],
        services=services,
    )
    assert rc == 0
    assert capsys.readouterr().out == "Audio Recap is enabled.\n"


def test_status_reports_disabled_for_default_off_and_no_state_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    # No config file at all — default_enabled stays False (the field
    # default), so status reflects the unchanged silent-by-default
    # behavior.
    services = real_services(str(cwd), tmp_path, runner=FakeProcessRunner())
    rc = command.main(
        ["command.py", "--session-id", "fresh-sid", "--cwd", str(cwd), "status"],
        services=services,
    )
    assert rc == 0
    assert capsys.readouterr().out == "Audio Recap is disabled.\n"


def test_off_persists_after_default_enabled_true(tmp_path: Path) -> None:
    """An explicit ``off`` writes ``enabled=false`` and survives subsequent
    reads, beating ``default_enabled: true``."""

    cwd = tmp_path / "proj"
    cwd.mkdir()
    _write_default_enabled_config(cwd, True)
    services = real_services(str(cwd), tmp_path, runner=FakeProcessRunner())

    rc = command.main(
        ["command.py", "--session-id", "explicit-off-sid", "--cwd", str(cwd), "off"],
        services=services,
    )
    assert rc == 0

    # Reading state with default_enabled=True must still see the
    # persisted ``false``. (The file beats the config default.)
    store = FileStateStore(state_root(tmp_path))
    s = store.load("explicit-off-sid", str(cwd), default_enabled=True)
    assert s.enabled is False


# ---------- doesn't touch real ~/.claude ----------


def test_main_is_callable() -> None:
    assert callable(command.main)


# ---------- the command surface is on/off/status only ----------


def test_label_verb_is_rejected(services: Services, capsys: pytest.CaptureFixture[str]) -> None:
    """``label`` was removed — CC's own ``/rename`` is the session-naming surface."""
    assert _run(services, "label", "sensei") == 2
    assert "usage" in capsys.readouterr().err


def test_verb_takes_no_trailing_arguments(
    services: Services, capsys: pytest.CaptureFixture[str]
) -> None:
    """The parser has no positional beyond the verb — stray tokens exit 2."""
    assert _run(services, "on", "extra") == 2
    assert "usage" in capsys.readouterr().err
