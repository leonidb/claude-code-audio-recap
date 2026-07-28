"""Production filesystem layout — the one place the on-disk paths live.

Kept in its own tiny, dependency-light module (stdlib ``pathlib`` only) so a
caller can resolve the storage root without importing the full
:mod:`audio_recap.services` dependency graph (recap / summarizer / TTS).
:mod:`audio_recap.services` re-exports these names, so a Linux/Windows port
still touches just this block — and nothing outside Python restates the layout.
The SessionEnd hook used to: ``scripts/session-end.sh`` spelled out the
``active/`` path in shell and needed a test pinning it against this module.
It routes through :mod:`audio_recap.session_end` now and imports the layout
like every other caller, so that whole class of drift is gone.
"""

from __future__ import annotations

from pathlib import Path

# The plugin's own storage base. The event log, narration cache, per-session
# on/off state, the cross-session presence heartbeats (``active/``), and the
# playback lock all nest under it.
DEFAULT_AUDIO_RECAP_ROOT = Path.home() / ".claude" / "audio-recap"

# The event log nests one level deeper (under ``logs/``) than the cache and
# state dirs; that asymmetry is the layout's, not a knob.
DEFAULT_LOG_PATH = DEFAULT_AUDIO_RECAP_ROOT / "logs" / "audio-recap.log"

# CC's own per-session transcript dir (``~/.claude/projects/<encoded-cwd>/
# <sid>.jsonl``). The plugin only *reads* from here, so it stays a separate
# knob — it is not the plugin's storage.
DEFAULT_CC_TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"


__all__ = [
    "DEFAULT_AUDIO_RECAP_ROOT",
    "DEFAULT_CC_TRANSCRIPT_ROOT",
    "DEFAULT_LOG_PATH",
]
