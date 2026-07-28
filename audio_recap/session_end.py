"""SessionEnd hook entrypoint — retire this session's presence heartbeat.

A heartbeat is a file at ``~/.claude/audio-recap/active/<session-id>``, marking
a session that is open with narration switched on (see
:mod:`audio_recap.presence`). Closing Claude Code is one of the two events that
retire one — ``/audio-recap:off`` is the other — and it carries most of the
weight, because nothing retires a heartbeat on idleness. Without this hook a
closed session would keep other sessions announcing their names until
``presence_window_s``, which is a day.

Deleting one file is all this does, so it was shell (``scripts/session-end.sh``)
for as long as speed mattered. It stopped mattering when the SessionStart and
UserPromptSubmit heartbeats were dropped: those fired before every turn, with CC
blocking on them, and a ~0.4s interpreter spawn was a real per-turn tax.
SessionEnd fires ONCE, at teardown, where interpreter startup is invisible — and
the Stop hook already pays it through the same ``run.sh``.

What the shell cost instead was a second, weaker implementation of things that
already have a home here: it re-derived the storage layout that lives in
:mod:`audio_recap.paths`, and re-implemented session-id resolution as a ``sed``
extraction plus a character-class guard. That guard admitted
:data:`~audio_recap.payload.GLOBAL_SESSION_ID` only by accident of how the class
was written — tightening it would have silently stopped collecting sentinel
heartbeats. Both concerns are now imported rather than restated.

**Fail-silent, always exit 0.** A hook that fires while the user is closing
Claude Code must not report anything, whatever arrives on stdin. The failure it
guards is already covered elsewhere: a SessionEnd that never runs at all (hard
kill, crash) leaves a heartbeat that ``presence_window_s`` ages out, so there is
nothing here worth retrying or reporting.
"""

from __future__ import annotations

import json
from typing import Any

from audio_recap.payload import session_id_from_payload
from audio_recap.presence import PresenceRegistry, default_presence_registry


def main(raw: bytes, *, registry: PresenceRegistry | None = None) -> int:
    """SessionEnd entry. Deletes the heartbeat named by ``raw``. Always 0.

    ``raw`` is CC's SessionEnd JSON payload — the ``__main__`` dispatcher reads
    it from stdin and passes it in, so this is pure-args and unit-testable
    without patching ``sys.stdin``. ``registry`` defaults to the production
    presence registry; tests inject one rooted at a tmp path.

    A payload with no usable ``session_id`` resolves to
    :data:`~audio_recap.payload.GLOBAL_SESSION_ID` and retires THAT heartbeat —
    the deliberate mirror of the Stop hook, which writes under the same sentinel
    from the same payload shape. It can never reach another session's file,
    because a real session's heartbeat is named by its real id.
    """

    try:
        payload: Any = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        target = registry if registry is not None else default_presence_registry()
        target.remove(session_id_from_payload(payload))
    except Exception:
        # Entrypoint-level defense for a standalone subprocess: unparseable
        # stdin, an unreadable HOME, a registry that cannot resolve its root.
        # None of it is worth surfacing while the user is closing the session.
        return 0
    return 0


__all__ = ["main"]
