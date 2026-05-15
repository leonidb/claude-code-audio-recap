"""TTS capability — speaks text.

Defines the :class:`TTS` Protocol and the :class:`TTSFailed`
exception. Concrete implementations live in sibling modules
(``macos_say`` in v0.1; Piper and ElevenLabs are designed-for but not
shipped).

Synchronous: :meth:`TTS.speak` blocks until playback finishes so the
hook can sequence recap-then-message without audio interleave. Voice
and rate live on the implementation's config (passed at construction
time), not on the call — callers pass only the text.
"""

from __future__ import annotations

from typing import Any, Protocol


class TTSFailed(Exception):
    """Signal from a TTS implementation that it cannot play the text.

    Raised on any playback failure — missing binary, nonzero exit,
    unsupported voice, or anything else that means "no sound came
    out." The hook surfaces the failure to stderr and exits non-zero
    so the user notices; silent TTS failure would be worse than a
    loud one for a narration-first product.
    """


class TTS(Protocol):
    def speak(self, text: str) -> dict[str, Any] | None:
        """Speak ``text``. Blocks until playback completes.

        Returns ``None`` for the standard speak path. Implementations
        that support a diagnostic / instrumented path may return a
        dict of per-call metrics (per-stage timings, audio-route
        info, etc.) so the hook can fold it into the eventlog without
        depending on the implementation's internals. Empty /
        whitespace-only ``text`` is a no-op (returns ``None``).

        Raises :class:`TTSFailed` on any playback failure.
        """
        ...
