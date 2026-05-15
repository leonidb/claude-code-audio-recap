"""Recap → summarize → speakable orchestration.

Given a :class:`audio_recap.payload.TurnPayload` and a wired-up
:class:`audio_recap.services.Services`, :meth:`Pipeline.run` produces
the speakable text segments and the diagnostic per-step log lines.
Caller is responsible for top-level entry/exit logging, ``state``
resolution, TTS playback, and cache writes.

Fallback handling lives here:

- **Recap**: ``recap_primary`` raises :class:`RecapFailed` →
  catch → call ``recap_fallback`` → catch → return
  ``recap_path="none"``. Two-tier fallback chain is internal to
  the Pipeline.
- **Summarizer**: ``summarizer`` raises :class:`SummarizerFailed`
  → fall back to verbatim (with truncation cue if over the cap).

TTS fallback (two-stage → legacy ``say``) is internal to
:class:`audio_recap.tts.macos_say.MacOSSay`; the Pipeline is
unaware. :class:`TTSRunner` (below) handles the recap-then-message
sequencing and the failure-out exit branch.
"""

from __future__ import annotations

import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from audio_recap.eventlog import EventLog
from audio_recap.payload import TurnPayload
from audio_recap.recap import RecapFailed
from audio_recap.services import Services
from audio_recap.speakable import apply_transforms
from audio_recap.summarizer import SummarizerFailed
from audio_recap.tts import TTSFailed

_VERBATIM_FALLBACK_WORD_CAP = 150
_VERBATIM_FALLBACK_CUE = "… and more — full output on screen."


def _user_warn(log: EventLog, message: str) -> None:
    """Mirror a user-visible warning to stderr AND the persistent log file.

    Recap fallbacks, summarizer fallbacks, and TTS failures all need
    to surface in the CC console (stderr) AND in the eventlog —
    preserves the dual-channel contract previously enforced by the
    pre-082 ``hook._log`` helper.
    """

    sys.stderr.write(f"[audio-recap] {message}\n")
    log.message(f"[audio-recap] {message}")


@dataclass
class PipelineResult:
    """Speakable segments + diagnostic paths from one Pipeline run.

    ``recap_text`` is ``None`` when no recap was produced (skip rules
    fired, or both backends failed). ``message_text`` is empty when
    the turn carried no user-facing reply. The two ``*_path`` fields
    are the structured-log values the caller emits.
    """

    recap_text: str | None
    message_text: str
    recap_path: str
    summary_path: str


def _truncate_verbatim_fallback(
    message: str, cap: int = _VERBATIM_FALLBACK_WORD_CAP
) -> tuple[str, bool]:
    """Cap a fallback-narration message; tag whether it was truncated.

    Above ``cap`` words, trim and append an audible "incomplete" cue.
    Below the cap, return unchanged.
    """

    words = message.split()
    if len(words) <= cap:
        return message, False
    return " ".join(words[:cap]) + " " + _VERBATIM_FALLBACK_CUE, True


class Pipeline:
    """Orchestrates recap, summary, and speakable transforms.

    Constructed with a :class:`Services` instance; all dependencies
    flow through it. ``Pipeline`` itself owns no state beyond the
    services reference.
    """

    def __init__(self, services: Services) -> None:
        self._s = services

    def run(self, turn: TurnPayload, *, event: str = "stop") -> PipelineResult:
        """Run recap + summary + transforms. Returns a :class:`PipelineResult`."""

        services = self._s
        log = services.eventlog
        config = services.config
        message = turn.message
        msg_words_pre = len(message.split())
        tool_uses = turn.tool_use_count

        # Skip-recap gates — applied before kicking off the parallel
        # recap subprocess so a 0-tool turn doesn't burn `claude -p`.
        skip_path = self._maybe_skip_recap(turn, message, msg_words_pre, tool_uses)
        if skip_path is not None:
            log.event(
                event,
                session_id=turn.session_id,
                recap_path=skip_path,
                tool_uses=tool_uses,
                message_words=msg_words_pre,
            )

        # First-pass speakable transforms run before the summarizer
        # dispatches so the summarizer takes the transformed text.
        msg_words_post = 0
        if message:
            message_pre = message
            message = apply_transforms(message, enabled=config.speakable_transforms)
            msg_words_post = len(message.split())
            log.event(
                event,
                session_id=turn.session_id,
                msg_words_pre=msg_words_pre,
                msg_words_post=msg_words_post,
                tool_uses=tool_uses,
            )
            log.event_trace(
                "trace_segment",
                session_id=turn.session_id,
                segment="message_first_pass",
                pre_transform=message_pre,
                post_transform=message,
            )

        # Recap and summary kicked off in parallel so wall-clock
        # collapses from `recap + summary` to `max(recap, summary)`.
        recap_text: str | None = None
        recap_path = skip_path or "none"
        summary_path = "no_message"
        with ThreadPoolExecutor(max_workers=2) as pool:
            recap_future = (
                None if skip_path is not None else pool.submit(self._generate_recap, turn, event)
            )
            summary_future = (
                pool.submit(self._summarize, message, msg_words_post, turn, event)
                if message
                else None
            )
            if recap_future is not None:
                recap_text, recap_path = recap_future.result()
            if summary_future is not None:
                message, summary_path = summary_future.result()

        # Recap text gets its own transforms pass (post-LLM output).
        if recap_text:
            recap_pre = recap_text
            recap_text = apply_transforms(recap_text, enabled=config.speakable_transforms)
            log.event_trace(
                "trace_segment",
                session_id=turn.session_id,
                segment="recap",
                pre_transform=recap_pre,
                post_transform=recap_text,
            )
        # Only the `claude_p` summary path returns text we haven't already
        # transformed; verbatim/truncated/under-threshold/disabled paths
        # return text the first pass already handled.
        if message and summary_path == "claude_p":
            message_pre = message
            message = apply_transforms(message, enabled=config.speakable_transforms)
            log.event_trace(
                "trace_segment",
                session_id=turn.session_id,
                segment="message_final",
                pre_transform=message_pre,
                post_transform=message,
            )

        return PipelineResult(
            recap_text=recap_text,
            message_text=message,
            recap_path=recap_path,
            summary_path=summary_path,
        )

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _maybe_skip_recap(
        self, turn: TurnPayload, message: str, msg_words_pre: int, tool_uses: int
    ) -> str | None:
        """Return ``"skipped_<reason>"`` if a skip rule applies, else ``None``."""

        recap_cfg = self._s.config.recap
        if recap_cfg.skip_if_no_tool_use and tool_uses == 0:
            return "skipped_no_tool_use"
        if (
            recap_cfg.skip_if_message_words_lt > 0
            and 0 < msg_words_pre < recap_cfg.skip_if_message_words_lt
        ):
            return "skipped_message_too_short"
        return None

    def _generate_recap(self, turn: TurnPayload, event: str) -> tuple[str | None, str]:
        """Try primary, fall back to rule-based. Returns ``(text, path)``."""

        services = self._s
        log = services.eventlog
        started = time.monotonic()
        try:
            text = services.recap_primary.generate(turn)
            log.event(
                event,
                session_id=turn.session_id,
                recap_path="claude_p",
                recap_words=len(text.split()),
                elapsed_s=round(time.monotonic() - started, 3),
            )
            return text, "claude_p"
        except RecapFailed as e:
            _user_warn(log, f"claude -p recap failed ({e}); using rule-based fallback")
            log.event(
                event,
                session_id=turn.session_id,
                recap_path="claude_p_failed",
                error=str(e),
                elapsed_s=round(time.monotonic() - started, 3),
            )
            log.event_trace(
                "trace_exception",
                session_id=turn.session_id,
                site="recap_claude_p",
                traceback=traceback.format_exc(),
            )
        started = time.monotonic()
        try:
            text = services.recap_fallback.generate(turn)
            log.event(
                event,
                session_id=turn.session_id,
                recap_path="rule_based",
                recap_words=len(text.split()),
                elapsed_s=round(time.monotonic() - started, 3),
            )
            return text, "rule_based"
        except RecapFailed as e:
            _user_warn(log, f"rule-based recap also failed ({e}); skipping recap")
            log.event(
                event,
                session_id=turn.session_id,
                recap_path="none",
                error=str(e),
            )
            log.event_trace(
                "trace_exception",
                session_id=turn.session_id,
                site="recap_rule_based",
                traceback=traceback.format_exc(),
            )
            return None, "none"

    def _summarize(
        self, message: str, msg_words_post: int, turn: TurnPayload, event: str
    ) -> tuple[str, str]:
        """Summarize when over threshold; return ``(text, path)``."""

        services = self._s
        log = services.eventlog
        threshold = services.config.narration.summarize_message_over_words
        if threshold <= 0:
            log.event(
                event,
                session_id=turn.session_id,
                summary_path="disabled",
                threshold=threshold,
                message_words=msg_words_post,
            )
            return message, "disabled"
        if msg_words_post <= threshold:
            log.event(
                event,
                session_id=turn.session_id,
                summary_path="under_threshold_verbatim",
                threshold=threshold,
                message_words=msg_words_post,
            )
            return message, "under_threshold_verbatim"
        started = time.monotonic()
        try:
            summary = services.summarizer.summarize(message, input_words=msg_words_post)
            log.event(
                event,
                session_id=turn.session_id,
                summary_path="claude_p",
                threshold=threshold,
                message_words=msg_words_post,
                summary_words=len(summary.split()),
                elapsed_s=round(time.monotonic() - started, 3),
            )
            return summary, "claude_p"
        except SummarizerFailed as e:
            fallback, was_truncated = _truncate_verbatim_fallback(message)
            path = "verbatim_truncated" if was_truncated else "verbatim"
            _user_warn(log, f"summarizer failed ({e}); speaking {path} message")
            log.event(
                event,
                session_id=turn.session_id,
                summary_path=path,
                threshold=threshold,
                message_words=msg_words_post,
                fallback_words=len(fallback.split()),
                error=str(e),
                elapsed_s=round(time.monotonic() - started, 3),
            )
            log.event_trace(
                "trace_exception",
                session_id=turn.session_id,
                site="summarizer",
                traceback=traceback.format_exc(),
            )
            return fallback, path


class TTSRunner:
    """Sequences recap-then-message playback and aggregates per-fire metrics.

    Returns ``0`` on success, ``1`` when TTS raises :class:`TTSFailed`.
    Aggregates the per-segment metrics dicts into a single per-fire
    eventlog line so the existing telemetry shape (``synth_elapsed_s``,
    ``aiff_duration_s``, ``afplay_elapsed_s``, ``say_path``,
    ``fallback_reason``) survives the refactor.
    """

    def __init__(self, services: Services) -> None:
        self._s = services

    def speak(
        self,
        result: PipelineResult,
        *,
        session_id: str,
        event: str = "stop",
    ) -> int:
        services = self._s
        log = services.eventlog
        recap = result.recap_text
        message = result.message_text
        segments = (1 if recap else 0) + (1 if message else 0)
        config = services.config
        tts_status = "dry_run" if config.dry_run else "spoken"

        # Final spoken text — TRACE only. INFO carries paths/counts/timings
        # so a production log never captures user reply content; readers
        # who need the text turn on ``log_level: trace`` per cwd.
        if recap:
            log.event_trace(
                "trace_segment",
                session_id=session_id,
                segment="recap_spoken",
                text=recap,
            )
        if message:
            log.event_trace(
                "trace_segment",
                session_id=session_id,
                segment="message_spoken",
                text=message,
            )

        info_fields: dict[str, Any] = {
            "session_id": session_id,
            "tts_status": tts_status,
            "segments": segments,
            "recap_path": result.recap_path,
            "summary_path": result.summary_path,
            "recap_words": len((recap or "").split()),
        }
        # Dry-run keeps ``recap_text``/``message_text`` at INFO — that mode
        # is per-cwd developer/testing (``dry_run: true``) and the eval
        # harvest joins on those fields. The production ``spoken`` path
        # never emits content text at INFO.
        if tts_status == "dry_run":
            info_fields["recap_text"] = recap or ""
            info_fields["message_text"] = message or ""
        log.event(event, **info_fields)
        if config.dry_run:
            return 0

        started = time.monotonic()
        diag_synth_s = 0.0
        diag_aiff_s = 0.0
        diag_afplay_s = 0.0
        diag_segments_seen = 0
        diag_fell_back = False
        diag_fallback_reasons: list[str] = []
        try:
            for segment_name, text in (("recap", recap), ("message", message)):
                if not text:
                    continue
                metrics = services.tts.speak(text)
                if metrics is None:
                    continue
                diag_synth_s += max(0.0, float(metrics.get("synth_elapsed_s") or 0.0))
                diag_aiff_s += max(0.0, float(metrics.get("aiff_duration_s") or 0.0))
                diag_afplay_s += max(0.0, float(metrics.get("afplay_elapsed_s") or 0.0))
                diag_segments_seen += 1
                if metrics.get("say_path") == "legacy_fallback":
                    diag_fell_back = True
                    reason = metrics.get("fallback_reason")
                    if isinstance(reason, str):
                        diag_fallback_reasons.append(f"{segment_name}:{reason}")
        except TTSFailed as e:
            _user_warn(log, f"TTS failed: {e}")
            log.event(
                event,
                session_id=session_id,
                outcome="tts_failed",
                tts_status="failed",
                error=str(e),
                elapsed_s=round(time.monotonic() - started, 3),
            )
            log.event_trace(
                "trace_exception",
                session_id=session_id,
                site="tts",
                traceback=traceback.format_exc(),
            )
            return 1

        if diag_segments_seen > 0:
            spoken_words = len((recap or "").split()) + len((message or "").split())
            expected = (
                round(spoken_words * 60.0 / config.tts.baseline_wpm, 3)
                if spoken_words > 0 and config.tts.baseline_wpm > 0
                else 0.0
            )
            observed_wpm = (
                round(spoken_words * 60.0 / diag_afplay_s, 1) if diag_afplay_s > 0 else 0.0
            )
            telemetry: dict[str, Any] = {
                "session_id": session_id,
                "say_path": "legacy_fallback" if diag_fell_back else "two_stage",
                "synth_elapsed_s": round(diag_synth_s, 3),
                "aiff_duration_s": round(diag_aiff_s, 3),
                "afplay_elapsed_s": round(diag_afplay_s, 3),
                "expected_say_s": expected,
                "say_rate_wpm_observed": observed_wpm,
                "spoken_words": spoken_words,
                "baseline_wpm": config.tts.baseline_wpm,
            }
            if diag_fallback_reasons:
                telemetry["fallback_reason"] = ",".join(diag_fallback_reasons)
            log.event(event, **telemetry)

        log.event(
            event,
            session_id=session_id,
            say_done=True,
            elapsed_s=round(time.monotonic() - started, 3),
        )
        return 0


__all__ = ["Pipeline", "PipelineResult", "TTSRunner"]
