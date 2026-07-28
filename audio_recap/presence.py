"""Cross-session presence registry — which other sessions could also speak.

Answers one question at narration time: *is any OTHER Claude Code session open
with narration switched on?* If so, the Stop hook prepends a spoken session
label so the listener knows which session they're hearing; a session speaking
alone stays clean.

**A heartbeat means "an enabled session that is open" — not "a session that
spoke recently", and not "a live process".** Both of those were tried:

- *Any live process* (SessionStart + UserPromptSubmit + Stop, whatever the
  state) counted sessions with narration switched off — including the headless
  ``claude -p`` agents that run that way, making audio for nobody — so a
  genuinely solo narration announced its name.
- *Spoke recently* (the narration path as sole writer, with an idleness window)
  fixed that but under-counted the opposite way: two sessions open side by side
  stopped naming themselves once one had been quiet past the window, which is
  precisely when the listener still needs to know who is talking.

What matters is whether another session **can make a sound**, and that is a
property of being open and enabled — not of when it last did.

Mechanism — a per-session heartbeat file ``<audio_recap_root>/active/
<session-id>``, created and retired by events rather than by elapsed time:

- **Written** by ``/audio-recap:on`` the moment narration is switched on, and
  refreshed by the Stop hook each time an enabled session narrates (past the
  ``off`` gate). The refresh is what keeps a live session ahead of the orphan
  horizon below; it is not what makes it count.
- **Deleted** by ``/audio-recap:off`` and on SessionEnd
  (:mod:`audio_recap.session_end`) — both stop the session counting at once.
- **Collected** past ``presence_window_s``. This is an orphan horizon, NOT an
  idleness timeout: the two events above retire a heartbeat in every ordinary
  case, so the only way one outlives its session is a hard kill that runs no
  SessionEnd. The default is deliberately long (24h) because ageing out an idle
  session is the failure mode being avoided, not the goal.

"Live others" = heartbeat files other than this session's, within the orphan
horizon. A stale heartbeat is harmless — worst case an unneeded label.

:class:`PresenceRegistry` is the DI seam; :class:`FilePresenceRegistry` is the
production impl; ``FakePresenceRegistry`` (in ``tests/fakes.py``) lets tests
model solo / N-others without touching the real ``~/.claude`` tree. Every
operation is best-effort and fail-silent: a heartbeat failure must never
block or error the host session, and a scan failure degrades to "solo" (0
others) so a registry problem yields an unlabeled narration, never a spurious
label.

Accepted trade-offs:

- A session enabled by per-cwd ``default_enabled`` rather than by
  ``/audio-recap:on`` registers on its **first narration**, since no code of
  ours runs before then. It has produced no audio to disambiguate until that
  point, so there is nothing to label around.
- A hard-killed session stays counted until the orphan horizon passes, costing
  at most one unneeded label — the same harmless direction a stale heartbeat
  has always erred in.
- A turn that turns out to have nothing to say, or whose ``say`` fails, still
  refreshes the heartbeat. It still describes an open enabled session, so it
  errs toward an unneeded label rather than a session going unnamed.
- ``/audio-recap:repeat`` refreshes it too, via the Stop hook that follows the
  slash-command turn. It takes no playback lock, though, so an explicit replay
  can overlap. See :mod:`audio_recap.repeat`.
- Collection reads an mtime and then unlinks, so an owner that refreshes its
  heartbeat between those two steps loses it. Reaching that window means a
  session silent for the whole horizon narrating in the microseconds before the
  unlink; the file returns on its next narration, and the cost meanwhile is one
  missing label. Guarding it would reintroduce the deletion delay this design
  removed, for a race that needs a full day of silence to line up.
- Sessions sharing the ``_global`` sentinel (Claude Code failed to substitute
  ``${CLAUDE_SESSION_ID}``) share ONE heartbeat, so the first of them to close
  retires it for all. They are indistinguishable by construction — presence is
  keyed on the id, and they have no distinct one — and the fallback already
  warns loudly on stderr. Again one missing label, until any of them narrates.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path
from typing import Protocol

from audio_recap.paths import DEFAULT_AUDIO_RECAP_ROOT

# Characters a session id may contain to be usable as a heartbeat FILENAME.
# Real Claude Code session ids are UUIDs and the ``_global`` sentinel fits too,
# so this rejects nothing legitimate.
_SAFE_ID_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")


def _heartbeat_path(root: Path, session_id: str) -> Path | None:
    """``root/<session-id>``, or ``None`` if the id is not a safe filename.

    The id arrives from a Claude Code hook payload and is joined onto a path we
    then create or UNLINK, so it has to be confined to a single name inside
    ``root``. ``Path.__truediv__`` composes rather than sanitises: a
    ``session_id`` of ``../active/other`` resolves to another session's
    heartbeat, and ``../../x`` escapes the directory altogether.

    Rejected: empty, anything containing a path separator or an exotic
    character, and anything dot-leading — ``..`` is the traversal case, and
    :func:`_is_heartbeat` skips dotfiles on the way in, so a dot-leading id
    could never be counted but could still name somebody's ``.DS_Store``.
    """

    if not session_id or session_id.startswith("."):
        return None
    if not _SAFE_ID_CHARS.issuperset(session_id):
        return None
    return root / session_id


def _is_heartbeat(entry: Path) -> bool:
    """Is this directory entry one of ours?

    ``active/`` is a plain directory under the user's home, so it can collect
    entries the registry never wrote — macOS drops a fresh-mtime ``.DS_Store``
    into any folder the user opens in Finder. Counting one as a live session
    would label a genuinely solo narration (and the stale-prune would later
    delete the user's file), so only regular non-dot files count.
    """

    if entry.name.startswith("."):
        return False
    try:
        return entry.is_file()
    except OSError:
        return False


class PresenceRegistry(Protocol):
    """Cross-session liveness seam.

    Production: :class:`FilePresenceRegistry`. Tests: ``FakePresenceRegistry``.
    All methods are best-effort — they never raise.
    """

    def heartbeat(self, session_id: str) -> None:
        """Register this session as open and enabled. Fail-silent."""
        ...

    def remove(self, session_id: str) -> None:
        """Delete this session's heartbeat (close / ``off``). Fail-silent."""
        ...

    def active_others(self, session_id: str, window_s: float) -> int:
        """Count OTHER sessions currently open with narration on.

        Excludes ``session_id`` itself. ``window_s`` is the orphan horizon, not
        an idleness timeout — it only discounts heartbeats left behind by a
        hard kill; see the module docstring. Collects those lazily. Returns
        ``0`` on any failure (degrade to unlabeled, never spurious).
        """
        ...


class FilePresenceRegistry:
    """Disk-backed presence registry. Default production implementation.

    ``active_root`` is required — the composition root supplies the production
    default (``~/.claude/audio-recap/active``); tests pass a tmp path. Each
    heartbeat is a zero-byte file named by session-id. Its EXISTENCE is the
    signal; the mtime only dates it for orphan collection.
    """

    def __init__(self, active_root: Path) -> None:
        self._root = active_root

    def heartbeat(self, session_id: str) -> None:
        """Write ``active/<session-id>``, refreshing its mtime. Fail-silent."""
        path = _heartbeat_path(self._root, session_id)
        if path is None:
            return
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            path.touch()
        except OSError:
            # Best-effort: a heartbeat failure must never break the host session.
            pass

    def remove(self, session_id: str) -> None:
        """Delete ``active/<session-id>`` if present. Fail-silent."""
        path = _heartbeat_path(self._root, session_id)
        if path is None:
            return
        with contextlib.suppress(OSError):
            path.unlink()

    def active_others(self, session_id: str, window_s: float) -> int:
        """Count heartbeats other than ``session_id``; collect orphaned ones.

        Best-effort: returns ``0`` if the directory is unreadable so a registry
        problem degrades to an unlabeled narration rather than a spurious label.
        """
        now = time.time()
        try:
            entries = list(self._root.iterdir())
        except OSError:
            return 0
        count = 0
        for entry in entries:
            if entry.name == session_id or not _is_heartbeat(entry):
                continue
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age <= window_s:
                count += 1
            else:
                # Past the orphan horizon: its session was hard-killed, since
                # any ordinary exit would have retired it. Collect it.
                with contextlib.suppress(OSError):
                    entry.unlink()
        return count


def default_presence_registry(
    audio_recap_root: Path = DEFAULT_AUDIO_RECAP_ROOT,
) -> FilePresenceRegistry:
    """The production presence registry rooted at ``<audio_recap_root>/active``.

    One place owns the ``active/`` subdir name, used by the composition root
    (:func:`audio_recap.services.Services.from_config`, which passes the
    injected root) and by tests pinning the layout the SessionEnd shell hook
    deletes from.
    """

    return FilePresenceRegistry(audio_recap_root / "active")


__all__ = ["FilePresenceRegistry", "PresenceRegistry", "default_presence_registry"]
