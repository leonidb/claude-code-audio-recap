"""TTS capability — speaks text.

Defines the :class:`TTS` Protocol, the :class:`RenderResult` handoff type,
and the :class:`TTSFailed` exception. Concrete implementations live in sibling
modules (``macos_say`` in v0.1; Piper and ElevenLabs are designed-for but not
shipped).

Synchronous: :meth:`TTS.speak` (and :meth:`TTS.play`) block until playback
finishes so the hook can sequence recap-then-message without audio interleave.
Voice and rate live on the implementation's config (passed at construction
time), not on the call — callers pass only the text.

**Render/play split.** To serialise only playback across concurrent sessions
(not the ~1s synthesis), the pipeline renders each segment to audio BEFORE
acquiring the machine-wide playback lock, then plays under the lock:

    rr = tts.render(text)      # synthesis — pre-lock, overlaps the wait
    ...acquire playback lock...
    metrics = tts.play(rr)     # playback only — under the lock

:meth:`TTS.speak` is the composed ``render`` + ``play`` and stays for callers
that don't split (``/audio-recap:repeat``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class TTSFailed(Exception):
    """Signal from a TTS implementation that it cannot play the text.

    Raised on any playback failure — missing binary, nonzero exit,
    unsupported voice, or anything else that means "no sound came
    out." The hook surfaces the failure to stderr and exits non-zero
    so the user notices; silent TTS failure would be worse than a
    loud one for a narration-first product.
    """


@dataclass
class RenderResult:
    """Handoff between :meth:`TTS.render` (pre-lock) and :meth:`TTS.play` (under lock).

    Carries the rendered-audio handle plus the partial metrics observed during
    synthesis, so :meth:`TTS.play` can emit the same telemetry shape a single
    :meth:`TTS.speak` call would. ``use_legacy`` means the render-side gates
    (synthesis failed, or the audio was suspiciously short) already decided to
    fall back to the streaming path — :meth:`TTS.play` runs that fallback under
    the lock and ignores any partial ``handle``.

    Fields other than ``text`` / ``use_legacy`` / ``fallback_reason`` are
    implementation detail (``handle`` is the AIFF path for ``macos_say``); a
    fake fills them with placeholders.
    """

    text: str
    handle: str
    expected_s: float
    synth_elapsed_s: float
    aiff_duration_s: float
    use_legacy: bool
    fallback_reason: str | None


class TTS(Protocol):
    def speak(self, text: str) -> dict[str, Any] | None:
        """Speak ``text`` (composed render + play). Blocks until playback completes.

        Returns ``None`` for empty / whitespace-only ``text`` or a dict of
        per-call metrics otherwise. Raises :class:`TTSFailed` on any playback
        failure.
        """
        ...

    def render(self, text: str) -> RenderResult | None:
        """Synthesise ``text`` to audio WITHOUT playing it.

        Returns a :class:`RenderResult` to hand to :meth:`play`, or ``None``
        for empty / whitespace-only ``text``. Does not raise for a synthesis
        failure — it records that in the result so :meth:`play` can fall back
        to streaming under the lock. Intended to run before the playback lock
        is acquired so synthesis overlaps the queue wait.
        """
        ...

    def play(self, rendered: RenderResult) -> dict[str, Any] | None:
        """Play a :meth:`render` result. Blocks until playback completes.

        Returns the per-call metrics dict (same shape as :meth:`speak`).
        Raises :class:`TTSFailed` if playback — including the streaming
        fallback — cannot produce sound. Intended to run while holding the
        playback lock so only playback is serialised. Releases the rendered
        handle (temp file) on the way out, played or not.
        """
        ...

    def discard(self, rendered: RenderResult) -> None:
        """Release a :meth:`render` result WITHOUT playing it.

        Frees the handle (temp file) for a segment that will never be played —
        e.g. a segment pre-rendered before the lock when an earlier segment's
        :meth:`play` failed. Best-effort; never raises. Completes the
        render → (play | discard) lifecycle so a pre-render can't leak.
        """
        ...
