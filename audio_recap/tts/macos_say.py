"""TTS backend: macOS ``say`` binary, two-stage by default.

The default ``speak`` flow renders text to a temporary AIFF via
``say -o`` (synthesis only), inspects the file's true duration with
``afinfo``, then plays it back via ``afplay``. This decouples
synthesis from playback — the canonical workaround for the
synthesizer→playback teardown race that intermittently truncates
long narrations on macOS (077 Phase-1 research). Synthesis on Apple
Silicon Premium voices runs ~24x realtime, so the added pre-audio
latency vs. streaming ``say -- text`` is ~1s on a typical
narration; that's negligible against the 10-30s ``claude -p``
overhead the hook already pays.

Three gates fall back to the streaming ``say -- text`` path so
degraded reliability beats no audio:

- ``say -o`` exits non-zero (synthesizer subprocess failure).
- ``afinfo`` reports the AIFF is more than 30 % shorter than
  ``expected_s = words * 60 / baseline_wpm`` (synthesizer-side
  truncation suspected; streaming sometimes happens to work
  because the bug is intermittent).
- ``afplay`` exits non-zero (playback subprocess failure).

Each ``speak`` call returns a metrics dict — per-stage timings,
the eventual ``say_path`` (``two_stage`` or ``legacy_fallback``),
and a ``fallback_reason`` when applicable — so the hook can fold
per-fire telemetry into the eventlog.

``TTSFailed`` is still the failure-out exception when both the
two-stage AND streaming fallback paths fail. The hook exits 1 and
surfaces the error to stderr; silent TTS failure would be worse
than a loud one for a narration-first product.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import time
from typing import Any

from audio_recap.config import TTS as TTSConfig
from audio_recap.process import ProcessFailed, ProcessRunner
from audio_recap.tts import TTSFailed

_AFINFO_DURATION_RE = re.compile(r"estimated duration:\s*([0-9.]+)\s*sec")

# Aiff-shorter-than-expected ratio that trips the legacy fallback.
# 0.7 = "more than 30 % shorter than expected." Generous enough to
# avoid false positives on voices with tighter prosody than the 142
# wpm baseline, tight enough to catch the 50 %+ truncations seen in
# the eventlog when the synthesizer cuts out.
_SHORT_AIFF_RATIO = 0.7


def _parse_afinfo_duration(stdout: str) -> float | None:
    """Pull the ``estimated duration: <n> sec`` value out of ``afinfo``.

    Returns ``None`` if the line is missing or unparseable so the
    caller can record a sentinel rather than crash on a malformed
    dump.
    """

    match = _AFINFO_DURATION_RE.search(stdout)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


class MacOSSay:
    def __init__(self, config: TTSConfig, runner: ProcessRunner) -> None:
        self._config = config
        self._runner = runner

    def speak(self, text: str) -> dict[str, Any] | None:
        """Speak ``text``. Default path is two-stage; falls back to streaming.

        Returns a metrics dict (or ``None`` when input is empty /
        whitespace-only). Dict shape:

        - ``say_path`` — ``"two_stage"`` (clean) or ``"legacy_fallback"``.
        - ``synth_elapsed_s`` — ``say -o`` wall-clock; ``-1.0`` when
          the render step itself failed.
        - ``aiff_duration_s`` — true file duration via ``afinfo``;
          ``-1.0`` when the file is unreadable / afinfo missing /
          render failed.
        - ``afplay_elapsed_s`` — playback wall-clock; ``-1.0`` if
          playback didn't run (fallback fired before it).
        - ``expected_say_s`` — ``words * 60 / baseline_wpm``.
        - ``fallback_reason`` — present only on legacy fallback;
          one of ``render_failed`` / ``short_aiff`` /
          ``afplay_failed``.

        Raises :class:`TTSFailed` when both the two-stage AND
        streaming fallback fail. The hook surfaces that to stderr.
        """

        if not text.strip():
            return None
        return self._speak_two_stage(text)

    # ------------------------------------------------------------------
    # primary path: render → inspect → play
    # ------------------------------------------------------------------

    def _speak_two_stage(self, text: str) -> dict[str, Any]:
        """Run the three-stage flow with three fallback gates.

        Each fallback path closes over the partial metrics observed
        so far and runs the streaming legacy ``say`` as a
        last-resort. If legacy itself fails, ``TTSFailed`` propagates
        and the hook exits 1.
        """

        expected_s = self._expected_seconds(text)
        # mkstemp is race-free; the random suffix avoids collisions
        # under parallel fires that a manual ``<sid>-<seq>.aiff``
        # naming scheme would have to coordinate.
        fd, aiff_path = tempfile.mkstemp(prefix="audio-recap-", suffix=".aiff")
        os.close(fd)
        try:
            try:
                synth_elapsed_s = self._render_to_aiff(text, aiff_path)
            except TTSFailed:
                # Synthesizer subprocess failed — try streaming as the
                # last resort. If it succeeds, the user gets audio
                # (even if vulnerable to the same daemon truncation
                # we were trying to dodge); if it fails too, the
                # raised TTSFailed propagates up unchanged.
                self._speak_legacy(text)
                return self._fallback_metrics(
                    expected_s,
                    "render_failed",
                    synth_elapsed_s=-1.0,
                    aiff_duration_s=-1.0,
                )

            aiff_duration_s = self._aiff_duration(aiff_path)

            # Short-aiff check: the synthesizer claims success but
            # the rendered audio is materially shorter than the word
            # count predicts. Suspect FB13188396-class engine cut.
            # Streaming path may stochastically dodge it.
            if (
                aiff_duration_s > 0.0
                and expected_s > 0.0
                and aiff_duration_s < expected_s * _SHORT_AIFF_RATIO
            ):
                self._speak_legacy(text)
                return self._fallback_metrics(
                    expected_s,
                    "short_aiff",
                    synth_elapsed_s=synth_elapsed_s,
                    aiff_duration_s=aiff_duration_s,
                )

            try:
                afplay_elapsed_s = self._play_aiff(aiff_path)
            except TTSFailed:
                self._speak_legacy(text)
                return self._fallback_metrics(
                    expected_s,
                    "afplay_failed",
                    synth_elapsed_s=synth_elapsed_s,
                    aiff_duration_s=aiff_duration_s,
                )

            return {
                "say_path": "two_stage",
                "synth_elapsed_s": synth_elapsed_s,
                "aiff_duration_s": aiff_duration_s,
                "afplay_elapsed_s": afplay_elapsed_s,
                "expected_say_s": expected_s,
            }
        finally:
            with contextlib.suppress(OSError):
                os.unlink(aiff_path)

    @staticmethod
    def _fallback_metrics(
        expected_s: float,
        reason: str,
        *,
        synth_elapsed_s: float,
        aiff_duration_s: float,
    ) -> dict[str, Any]:
        return {
            "say_path": "legacy_fallback",
            "fallback_reason": reason,
            "synth_elapsed_s": synth_elapsed_s,
            "aiff_duration_s": aiff_duration_s,
            "afplay_elapsed_s": -1.0,
            "expected_say_s": expected_s,
        }

    def _expected_seconds(self, text: str) -> float:
        """Words / baseline_wpm reference duration. Defensive on bad config."""

        words = len(text.split())
        wpm = self._config.baseline_wpm
        if words <= 0 or wpm <= 0:
            return 0.0
        return round(words * 60.0 / wpm, 3)

    # ------------------------------------------------------------------
    # subprocess wrappers
    # ------------------------------------------------------------------

    def _say_args(self) -> list[str]:
        """Common ``say`` argv prefix shared by both paths."""

        args = ["say"]
        if self._config.voice is not None:
            args.extend(["-v", self._config.voice])
        if self._config.rate_wpm is not None:
            args.extend(["-r", str(self._config.rate_wpm)])
        return args

    def _render_to_aiff(self, text: str, aiff_path: str) -> float:
        args = self._say_args()
        # ``--`` ends option parsing so a leading-dash message (markdown
        # bullet, diff line, etc.) isn't read as an unknown flag. Same
        # invariant as the streaming path below.
        args.extend(["-o", aiff_path, "--", text])
        started = time.monotonic()
        try:
            result = self._runner.run(args)
        except ProcessFailed as e:
            raise TTSFailed("say binary not found on PATH") from e
        if result.returncode != 0:
            raise TTSFailed(f"say -o exited {result.returncode}: {result.stderr.strip()!r}")
        return round(time.monotonic() - started, 3)

    def _aiff_duration(self, aiff_path: str) -> float:
        """Best-effort: returns ``-1.0`` if afinfo is missing or unparseable.

        ``afinfo`` failure shouldn't kill the diagnostic — playback
        is the load-bearing user-facing step. ``-1.0`` is a greppable
        sentinel that won't trip the short-aiff fallback gate
        (which checks ``> 0.0``).
        """

        try:
            result = self._runner.run(["afinfo", aiff_path])
        except ProcessFailed:
            return -1.0
        if result.returncode != 0:
            return -1.0
        duration = _parse_afinfo_duration(result.stdout)
        return duration if duration is not None else -1.0

    def _play_aiff(self, aiff_path: str) -> float:
        started = time.monotonic()
        try:
            result = self._runner.run(["afplay", aiff_path])
        except ProcessFailed as e:
            raise TTSFailed("afplay binary not found on PATH") from e
        if result.returncode != 0:
            raise TTSFailed(f"afplay exited {result.returncode}: {result.stderr.strip()!r}")
        return round(time.monotonic() - started, 3)

    # ------------------------------------------------------------------
    # streaming (legacy / fallback) path
    # ------------------------------------------------------------------

    def _speak_legacy(self, text: str) -> None:
        args = self._say_args()
        args.extend(["--", text])

        try:
            result = self._runner.run(args)
        except ProcessFailed as e:
            raise TTSFailed("say binary not found on PATH") from e

        if result.returncode != 0:
            raise TTSFailed(f"say exited {result.returncode}: {result.stderr.strip()!r}")
