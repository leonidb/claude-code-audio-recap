"""Reader for Claude Code's per-session transcript.

The Stop hook gets the turn straight from CC's hook payload. The
``/audio-recap:repeat`` slash command does not — CC doesn't pass
``transcript_path`` to slash-command bodies — so on a cache miss it
re-derives the transcript path from ``(cwd, session_id)`` and reads
CC's own per-session JSONL transcript.

:class:`TranscriptReader` is the seam :class:`audio_recap.services.Services`
injects; :class:`FileTranscriptReader` is the production implementation.
It takes its ``root`` as a required constructor argument — the
composition root (:mod:`audio_recap.services`) supplies the production
default; tests pass a tmp path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from audio_recap.payload import PayloadParser, TurnPayload, load_turn_content_from_jsonl
from audio_recap.state import _encode_cwd


class TranscriptReader(Protocol):
    """Loads the most recent assistant turn from CC's session transcript.

    Returns ``None`` when no transcript exists for the session, so the
    caller can fall back to the empty-transcript cue.
    """

    def load(self, session_id: str, cwd: str) -> TurnPayload | None: ...


class FileTranscriptReader:
    """Default reader. Walks ``<root>/<encoded-cwd>/<sid>.jsonl``.

    ``root`` is required — the composition root supplies the production
    default (``~/.claude/projects``); tests pass a tmp path.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def load(self, session_id: str, cwd: str) -> TurnPayload | None:
        path = self._root / _encode_cwd(cwd) / f"{session_id}.jsonl"
        loaded = load_turn_content_from_jsonl(str(path))
        if loaded is None:
            return None
        user_content, content_blocks = loaded
        if not content_blocks:
            return None
        # Synthesize a payload dict so PayloadParser produces a canonical
        # TurnPayload (slash-command detection, tool-use extraction, etc.
        # all share one code path). from_dict is a log-free staticmethod.
        return PayloadParser.from_dict(
            {
                "session_id": session_id,
                "cwd": cwd,
                "transcript": [
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": content_blocks},
                ],
            }
        )


__all__ = ["FileTranscriptReader", "TranscriptReader"]
