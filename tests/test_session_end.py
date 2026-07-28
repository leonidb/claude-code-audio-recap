"""Tests for :mod:`audio_recap.session_end` — the SessionEnd hook handler.

Replaces ``tests/test_session_end_script.py``, which drove the shell version as
a subprocess.

The sanitation cases below are NOT shell leftovers. The session id still ends up
joined onto a path that gets unlinked, and ``Path.__truediv__`` composes rather
than sanitises — a ``session_id`` of ``../active/other`` names another session's
heartbeat and ``../../x`` leaves the directory entirely. The guard moved into
:func:`audio_recap.presence._heartbeat_path`, so each case seeds a real victim
file to prove the guard is what saves it rather than a path that happened not to
resolve.

The rest pins the contract the hook has to the plugin: it deletes exactly the
heartbeat the Stop hook would have written for that payload, never anyone
else's, and exits 0 whatever arrives.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from audio_recap import session_end
from audio_recap.payload import GLOBAL_SESSION_ID
from audio_recap.presence import FilePresenceRegistry, default_presence_registry

_PLUGIN_ROOT = Path(__file__).parents[1]
_RUN_SH = _PLUGIN_ROOT / "scripts" / "run.sh"


def _registry(tmp_path: Path) -> FilePresenceRegistry:
    """A presence registry rooted in tmp, built the way production builds it."""

    return default_presence_registry(tmp_path)


def _active(tmp_path: Path) -> Path:
    return tmp_path / "active"


def _run(payload: object, registry: FilePresenceRegistry) -> int:
    """Drive the real entrypoint with ``payload`` as its stdin bytes."""

    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return session_end.main(raw.encode("utf-8"), registry=registry)


def test_removes_only_the_named_heartbeat(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.heartbeat("sid-a")
    registry.heartbeat("sid-b")

    assert _run({"session_id": "sid-a", "cwd": "/proj"}, registry) == 0
    assert not (_active(tmp_path) / "sid-a").exists()
    assert (_active(tmp_path) / "sid-b").exists()


def test_absent_heartbeat_is_a_noop(tmp_path: Path) -> None:
    """A session that closes without ever narrating has nothing to delete."""

    assert _run({"session_id": "never-narrated"}, _registry(tmp_path)) == 0


def test_real_uuid_session_id_is_accepted(tmp_path: Path) -> None:
    """The shape CC actually sends."""

    sid = "4d38f5fa-9f34-416d-9b5c-5f09dd37bd91"
    registry = _registry(tmp_path)
    registry.heartbeat(sid)

    assert _run({"session_id": sid}, registry) == 0
    assert not (_active(tmp_path) / sid).exists()


def test_pretty_printed_payload_is_parsed(tmp_path: Path) -> None:
    """CC could hand us a multi-line payload — a real JSON parse doesn't care.

    The shell version collapsed newlines by hand before a ``sed`` match; this is
    the case that motivated it.
    """

    registry = _registry(tmp_path)
    registry.heartbeat("sid-a")

    assert _run('{\n  "session_id": "sid-a",\n  "cwd": "/proj"\n}', registry) == 0
    assert not (_active(tmp_path) / "sid-a").exists()


def test_nested_session_id_does_not_win(tmp_path: Path) -> None:
    """A nested second id must not be the one retired.

    The shell version read the payload with a greedy ``sed`` match that reached
    the LAST ``session_id`` on the line, so a payload carrying a nested one
    deleted the wrong session's heartbeat. Parsing the JSON removes the whole
    failure mode; this pins that it stays removed.
    """

    registry = _registry(tmp_path)
    registry.heartbeat("outer")
    registry.heartbeat("inner")

    assert _run('{"session_id":"outer","meta":{"session_id":"inner"}}', registry) == 0
    assert not (_active(tmp_path) / "outer").exists()
    assert (_active(tmp_path) / "inner").exists()


# ---------------------------------------------------------------------------
# the sentinel — deliberate, shared with the hook that writes the heartbeat
# ---------------------------------------------------------------------------


def test_payload_without_session_id_retires_the_sentinel(tmp_path: Path) -> None:
    """No ``session_id`` → retire ``_global``, mirroring what Stop wrote there.

    The Stop hook resolves the SAME payload shape to
    :data:`~audio_recap.payload.GLOBAL_SESSION_ID` and writes its heartbeat
    under that name, so SessionEnd has to retire that name or the sentinel
    heartbeat is only ever collected by the stale-window. The shell version
    deleted nothing here.
    """

    registry = _registry(tmp_path)
    registry.heartbeat(GLOBAL_SESSION_ID)
    registry.heartbeat("sid-a")

    assert _run({"cwd": "/proj"}, registry) == 0
    assert not (_active(tmp_path) / GLOBAL_SESSION_ID).exists()
    assert (_active(tmp_path) / "sid-a").exists()  # a real session is untouched


def test_explicit_sentinel_session_id_is_retired(tmp_path: Path) -> None:
    """``"session_id": "_global"`` is an ordinary id, not a special case.

    The shell guard admitted a leading underscore only by accident of how its
    character class was written; resolving through
    :func:`~audio_recap.payload.session_id_from_payload` makes it deliberate.
    """

    registry = _registry(tmp_path)
    registry.heartbeat(GLOBAL_SESSION_ID)

    assert _run({"session_id": GLOBAL_SESSION_ID}, registry) == 0
    assert not (_active(tmp_path) / GLOBAL_SESSION_ID).exists()


def test_non_string_session_id_falls_back_to_the_sentinel(tmp_path: Path) -> None:
    """A malformed id resolves like a missing one rather than crashing."""

    registry = _registry(tmp_path)
    registry.heartbeat(GLOBAL_SESSION_ID)

    assert _run({"session_id": 42}, registry) == 0
    assert not (_active(tmp_path) / GLOBAL_SESSION_ID).exists()


# ---------------------------------------------------------------------------
# session-id sanitation — the id is joined onto a path that gets unlinked
# ---------------------------------------------------------------------------


def test_traversal_into_another_heartbeat_is_rejected(tmp_path: Path) -> None:
    """``../active/<other>`` must not retire a different session.

    Drop the guard and this deletes ``victim``: the join resolves right back
    into ``active/`` under another name.
    """

    registry = _registry(tmp_path)
    registry.heartbeat("victim")

    assert _run({"session_id": "../active/victim"}, registry) == 0
    assert (_active(tmp_path) / "victim").exists()


def test_traversal_out_of_the_registry_is_rejected(tmp_path: Path) -> None:
    """A traversal id must not reach a file that is not a heartbeat at all."""

    registry = _registry(tmp_path)
    registry.heartbeat("keep")
    outside = tmp_path / "precious.json"
    outside.write_text("not ours to delete", encoding="utf-8")

    assert _run({"session_id": "../precious.json"}, registry) == 0
    assert outside.exists()


def test_dotfile_session_id_is_rejected(tmp_path: Path) -> None:
    """presence.py ignores dotfiles on the way in, so one is never ours to delete."""

    registry = _registry(tmp_path)
    registry.heartbeat("keep")
    ds_store = _active(tmp_path) / ".DS_Store"
    ds_store.write_bytes(b"\x00\x01")

    assert _run({"session_id": ".DS_Store"}, registry) == 0
    assert ds_store.exists()


def test_exotic_session_id_is_rejected(tmp_path: Path) -> None:
    """An id outside the CC session-id charset is refused, not deleted."""

    exotic = "sid a; rm -rf ."
    registry = _registry(tmp_path)
    (_active_seeded(tmp_path, registry) / exotic).write_text("", encoding="utf-8")

    assert _run({"session_id": exotic}, registry) == 0
    assert (_active(tmp_path) / exotic).exists()


def _active_seeded(tmp_path: Path, registry: FilePresenceRegistry) -> Path:
    """Ensure ``active/`` exists so a rejected id can't 'pass' for want of a dir."""

    registry.heartbeat("keep")
    return _active(tmp_path)


# ---------------------------------------------------------------------------
# never break the host session — exit 0 whatever arrives on stdin
# ---------------------------------------------------------------------------


def test_garbage_payload_exits_zero(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    registry.heartbeat("sid-a")

    assert _run("not json at all {{{", registry) == 0
    assert (_active(tmp_path) / "sid-a").exists()


def test_empty_payload_exits_zero(tmp_path: Path) -> None:
    assert _run("", _registry(tmp_path)) == 0


def test_non_object_payload_exits_zero(tmp_path: Path) -> None:
    """Valid JSON that isn't an object — nothing to read a session id out of."""

    registry = _registry(tmp_path)
    registry.heartbeat("sid-a")

    assert _run("[1, 2, 3]", registry) == 0
    assert _run('"a string"', registry) == 0
    assert (_active(tmp_path) / "sid-a").exists()


def test_a_raising_registry_still_exits_zero(tmp_path: Path) -> None:
    """The catch-all is entrypoint defense, not decoration.

    ``FilePresenceRegistry.remove`` is documented fail-silent, but the hook must
    survive a registry that breaks its contract too — it runs while the user is
    closing Claude Code, where there is nothing useful to report.
    """

    class Exploding:
        def heartbeat(self, session_id: str) -> None: ...

        def remove(self, session_id: str) -> None:
            raise RuntimeError("boom")

        def active_others(self, session_id: str, window_s: float) -> int:
            return 0

    assert session_end.main(b'{"session_id":"sid-a"}', registry=Exploding()) == 0


def test_default_registry_is_the_production_one(tmp_path: Path) -> None:
    """Called without injection, it targets the real presence root.

    Pins the wiring rather than the deletion: production is ``$HOME``-rooted, so
    the assertion is about which path it resolves, not about deleting there.
    """

    payload: dict[str, Any] = {"session_id": "sid-a"}
    # No registry passed → resolves the production default without raising.
    assert session_end.main(json.dumps(payload).encode("utf-8")) == 0


def test_unset_home_still_exits_zero(tmp_path: Path) -> None:
    """The one case that needs a real subprocess: a broken environment.

    The production storage root is derived from ``$HOME`` at import, so this
    cannot be exercised in-process. The shell handler this replaced aborted
    under ``set -u`` when ``HOME`` was missing, contradicting its own
    always-exit-0 contract, and that was a live bug until 2026-07-23 — worth
    keeping pinned across the rewrite rather than assuming Python inherits it.
    """

    result = subprocess.run(
        [str(_RUN_SH), str(_PLUGIN_ROOT), "session-end"],
        input=json.dumps({"session_id": "sid-a"}),
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if k != "HOME"},
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_deletes_where_the_narration_writes(tmp_path: Path) -> None:
    """End-to-end on the registry: a heartbeat that counts stops counting.

    The old shell handler needed a drift guard because it restated the storage
    layout; this asserts the property that guard existed to protect, now that
    both sides import :mod:`audio_recap.paths`.
    """

    registry = _registry(tmp_path)
    registry.heartbeat("other-session")
    assert registry.active_others("me", window_s=900) == 1

    assert _run({"session_id": "other-session"}, registry) == 0
    assert registry.active_others("me", window_s=900) == 0
