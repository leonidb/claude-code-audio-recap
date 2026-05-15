"""Persistent on/off state for Audio Recap, scoped per CC session.

The :class:`StateStore` Protocol is the seam Pipeline / hook / command
inject; :class:`FileStateStore` is the production implementation that
persists to disk. It takes its ``projects_root`` as a required
constructor argument — the composition root (:mod:`audio_recap.services`)
supplies the production default; tests pass a tmp path or substitute
:class:`InMemoryStateStore` (in ``tests/fakes.py``).

State files live at ``<projects_root>/<encoded-cwd>/<session-id>.json``.
Each holds one JSON object with a single boolean, ``enabled``. The path
mirrors Claude Code's own per-project layout under
``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`` so the on-disk
shape is visually parseable for users.

Audio Recap is **off by default**: a fresh session (no state file), an
unrecoverable failure path (corrupt JSON, wrong shape, non-bool value),
and a missing or empty ``session_id`` all resolve to ``enabled=False``
and log to stderr where appropriate. The user opts in per-session with
``/audio-recap:on``; an unintelligible state file silently turning audio
on would be the worse failure mode because of concurrent-session audio
collision.

A special ``session_id`` value of ``"_global"`` is the runtime-sentinel
fallback used by ``scripts/run.sh`` when CC fails to substitute
``${CLAUDE_SESSION_ID}`` into the slash-command body. State written
under that id collapses back to a single shared file and the run.sh
sentinel logs a loud stderr warning the user will notice.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class State:
    enabled: bool = False


def _encode_cwd(cwd: str) -> str:
    """Encode a cwd path to a single filesystem-safe directory name.

    Mirrors Claude Code's own convention for ``~/.claude/projects/...``:
    canonicalize the path (resolve symlinks, drop the leading slash), then
    replace path separators with dashes. ``/Users/dev/proj`` becomes
    ``-Users-dev-proj``. Non-path-separator characters that would be
    awkward on disk get replaced with ``-`` too, defensively.
    """

    if not cwd:
        # Fall back to a marker that's distinct from the runtime-sentinel
        # ``_global`` and from any real cwd encoding.
        return "_unknown-cwd"
    try:
        resolved = Path(cwd).resolve(strict=False)
    except OSError:
        resolved = Path(cwd)
    encoded = str(resolved).replace("/", "-")
    # Drop characters CC's encoding wouldn't produce; defensive.
    encoded = re.sub(r"[^A-Za-z0-9._-]", "-", encoded)
    return encoded or "_unknown-cwd"


def _path_for(session_id: str, cwd: str, projects_root: Path) -> Path:
    """Return the per-session state file path under ``projects_root``.

    A session_id of ``"_global"`` is the run.sh runtime-sentinel fallback;
    its files live under a sibling ``_global/`` directory rather than under
    a project-encoded one. That keeps the global fallback visually distinct
    from real per-project state on disk.
    """

    if session_id == "_global":
        # The runtime-sentinel fallback path. Single shared file.
        return projects_root.parent / "_global" / "state.json"
    return projects_root / _encode_cwd(cwd) / f"{session_id}.json"


def _log_corrupt(path: Path, reason: str) -> None:
    sys.stderr.write(
        f"[audio-recap] state file at {path} is unreadable ({reason}); "
        "defaulting to enabled=False\n"
    )


class StateStore(Protocol):
    """Per-session on/off persistence seam.

    Production: :class:`FileStateStore` (disk-backed). Tests inject an
    in-memory fake. ``default_enabled`` only fills in the missing-file
    branch — once a state file exists it stays authoritative.
    """

    def load(self, session_id: str, cwd: str, *, default_enabled: bool = False) -> State:
        """Return persisted state, or ``State(enabled=default_enabled)`` if absent."""
        ...

    def save(self, state: State, session_id: str, cwd: str) -> None:
        """Persist atomically. Raises on I/O failure (caller decides what to do)."""
        ...


class FileStateStore:
    """Disk-backed state. Default production implementation.

    ``projects_root`` is required — the composition root supplies the
    production default; tests pass a tmp path.
    """

    def __init__(self, projects_root: Path) -> None:
        self._projects_root = projects_root

    def load(self, session_id: str, cwd: str, *, default_enabled: bool = False) -> State:
        return self._load(_path_for(session_id, cwd, self._projects_root), default_enabled)

    def save(self, state: State, session_id: str, cwd: str) -> None:
        self._save(state, _path_for(session_id, cwd, self._projects_root))

    @staticmethod
    def _load(path: Path, default_enabled: bool) -> State:
        """Read the per-session on/off state from ``path``.

        Resolution order:

        - **Missing file** → ``State(enabled=default_enabled)``. The
          ``default_enabled`` knob lets a per-cwd
          ``.audio-recap/config.json`` opt a worktree into auto-on for
          fresh sessions; ``False`` (the standing default) preserves the
          original "fresh session is silent until ``/audio-recap:on``"
          behavior.
        - **Corrupt JSON / wrong-shape payload / non-bool ``enabled`` /
          missing ``enabled`` key** → ``State(enabled=False)`` regardless
          of ``default_enabled``. Explicit failure must surface as silence
          so a user with a broken file isn't misled into thinking
          audio is live.
        - **Existing well-formed file** → returns the persisted value;
          ``default_enabled`` is irrelevant. Once a session has toggled
          (``/audio-recap:on`` / ``/audio-recap:off``) the file wins.
        """

        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return State(enabled=default_enabled)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            _log_corrupt(path, f"invalid JSON: {e.msg}")
            return State(enabled=False)
        if not isinstance(data, dict):
            _log_corrupt(path, "top-level JSON is not an object")
            return State(enabled=False)
        if "enabled" not in data:
            # Lenient: missing key is treated as default-off, no log.
            return State(enabled=False)
        enabled = data["enabled"]
        if not isinstance(enabled, bool):
            _log_corrupt(path, f"'enabled' must be a bool, got {type(enabled).__name__}")
            return State(enabled=False)
        return State(enabled=enabled)

    @staticmethod
    def _save(state: State, path: Path) -> None:
        """Persist ``state`` atomically to ``path``.

        Creates the parent directories if missing. Writes to a sibling
        temp file, fsyncs, then renames into place — a concurrent reader
        never observes a half-written file. If anything fails, the temp
        file is cleaned up and the exception propagates.
        """

        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=str(path.parent))
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                json.dump({"enabled": state.enabled}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
