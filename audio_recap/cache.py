"""Per-session audio cache for ``/audio-recap:repeat``.

Stores the most recent ``(recap, message)`` pair per CC ``session_id``
so the slash command can replay it instantly. The Stop hook writes
after a successful narration; the slash command reads on invocation.
The cache is overwritten each turn — Audio Recap caches **the last
narration**, not history. No rotation, no GC, accumulates forever
(same stance as ``state`` and the eventlog write paths). Cost is
~few KB per session.

File path is ``<root>/<session_id>.txt``. Format is plain text with a
sentinel boundary line:

    <recap text or empty line>
    --- AUDIO RECAP CACHE BOUNDARY ---
    <message text, may be multi-line, runs to EOF>

:class:`NarrationCache` is the Protocol the Pipeline injects;
:class:`FileNarrationCache` is the production implementation. It takes
its ``root`` as a required constructor argument and resolves nothing
itself — the composition root (:mod:`audio_recap.services`) supplies
the production default; tests pass a tmp path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

# Sentinel line separating recap from message. Long enough that no real
# narration line ever matches it; the speakable transforms strip code
# fences and tables, but a literal "---" can still appear in user prose,
# so the verbose form gives us margin.
_BOUNDARY = "--- AUDIO RECAP CACHE BOUNDARY ---"


class NarrationCache(Protocol):
    """Per-session narration cache seam.

    Best-effort: ``write`` swallows ``OSError`` silently; ``read``
    returns ``None`` for any failure (missing, corrupt, unreadable).
    The cache is a convenience surface — its failures must never
    affect the hook's exit code.
    """

    def write(self, session_id: str, recap: str | None, message: str) -> None:
        """Persist the (recap, message) pair. Best-effort; silent on I/O error."""
        ...

    def read(self, session_id: str) -> tuple[str | None, str] | None:
        """Return the cached pair, or ``None`` for any miss / failure."""
        ...


class FileNarrationCache:
    """Disk-backed cache. Default production implementation.

    ``root`` is required — the composition root supplies the production
    default; tests pass a tmp path.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.txt"

    def write(self, session_id: str, recap: str | None, message: str) -> None:
        body = f"{recap or ''}\n{_BOUNDARY}\n{message}"
        try:
            path = self.path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        except OSError:
            pass

    def read(self, session_id: str) -> tuple[str | None, str] | None:
        try:
            body = self.path(session_id).read_text(encoding="utf-8")
        except OSError:
            return None
        sentinel = f"\n{_BOUNDARY}\n"
        if sentinel not in body:
            return None
        recap_part, message = body.split(sentinel, 1)
        recap = recap_part if recap_part else None
        return recap, message


__all__ = [
    "FileNarrationCache",
    "NarrationCache",
]
