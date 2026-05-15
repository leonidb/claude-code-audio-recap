"""Message summarizer — shortens long assistant replies for narration.

Listeners can't skim. A 250-word message read at 150 wpm runs about
100 seconds; before this module existed, ``speakable.py`` simply
truncated and appended ``"..."``, which on a real long reply landed
mid-sentence and silently dropped information. The summarizer
replaces that path: when the post-transform message exceeds
``Narration.summarize_message_over_words``, the hook calls
:meth:`ClaudePSummarizer.summarize` instead and speaks the result.

Like :mod:`audio_recap.recap.claude_p`, this is a thin wrapper around
``claude -p``. Failure → :class:`SummarizerFailed`; the hook catches
it and falls back to speaking the verbatim message under the same
speak-don't-skip principle as the recap fallback.

The prompt asserts an explicit compression contract — output ≤ 50% of
the input word count, hard ceiling at :data:`HARD_WORD_CEILING`
(~120 words ≈ 50 spoken seconds). The input word count is
interpolated into the prompt so the model has a concrete target,
and "never lengthen the input" is stated as a hard rule.
"""

from __future__ import annotations

import subprocess
import traceback
from typing import Protocol

from audio_recap.config import Summarizer as SummarizerConfig
from audio_recap.eventlog import EventLog
from audio_recap.process import ProcessFailed, ProcessRunner

# 120 words ≈ 50 seconds at 150 wpm — the upper bound for "could be a
# long acknowledgement, not a monologue". Above this the listener has
# lost the thread regardless of input size, so cap there.
HARD_WORD_CEILING = 120


def _target_words(input_words: int) -> int:
    """Half the input, capped at :data:`HARD_WORD_CEILING`, floor 1.

    Floor at 1 keeps the formatted prompt sensible if a tiny input
    sneaks past the upstream threshold gate; the hook only invokes
    the summarizer above ``Narration.summarize_message_over_words``
    today, but the math should be defined for any positive input.
    """

    return max(1, min(HARD_WORD_CEILING, input_words // 2))


_PROMPT_TEMPLATE = """\
You ARE the assistant. The text below is your own reply to the
user, and it is about to be spoken aloud to them. Rewrite it as a
shorter version of itself — what you would have said if you had
answered out loud the first time. The reply is {input_words} words
long; your rewrite MUST be no more than {target_words} words.
Never lengthen the input — if the reply is already short, return
it nearly verbatim but never longer than the input itself.

Speak in your own voice. Use first-person for yourself ("I",
"I'll", "my") and second-person for the listener ("you", "your").
Never refer to yourself in the third person — do not say "the
assistant", "they", or "it" when you mean yourself.

Use 2 to 6 short sentences. No preamble. No bullet points, no
code fences, no quotation marks. Use plain prose. Render numbers,
currencies, and percentages as spoken English — e.g., "twenty
dollars", "five percent".

Your reply, to be rewritten:
{message}
"""


class SummarizerFailed(Exception):
    """Signal that the summarizer cannot produce a summary.

    Raised on timeout, subprocess error, missing CLI, or empty output.
    The hook catches this and falls back to speaking the verbatim
    message.
    """


class Summarizer(Protocol):
    """One-method seam over the message-summarization step.

    Implementations rewrite ``message`` into a shorter spoken form.
    Always raises :class:`SummarizerFailed` on any unrecoverable
    failure — never returns an empty or sentinel string.
    """

    def summarize(self, message: str, input_words: int | None = None) -> str: ...


class ClaudePSummarizer:
    def __init__(
        self,
        config: SummarizerConfig,
        session_id: str,
        runner: ProcessRunner,
        eventlog: EventLog,
    ) -> None:
        self._config = config
        # ``session_id`` is carried only for TRACE entries — see the
        # parallel comment in :class:`ClaudePRecap`.
        self._session_id = session_id
        self._runner = runner
        self._eventlog = eventlog

    def summarize(self, message: str, input_words: int | None = None) -> str:
        if input_words is None:
            input_words = len(message.split())
        prompt = _PROMPT_TEMPLATE.format(
            input_words=input_words,
            target_words=_target_words(input_words),
            message=message,
        )
        self._eventlog.event_trace(
            "trace_summarizer_prompt",
            session_id=self._session_id,
            prompt=prompt,
        )
        try:
            result = self._runner.run(
                ["claude", "-p", "--model", self._config.model],
                input=prompt,
                timeout=self._config.timeout_s,
            )
        except subprocess.TimeoutExpired as e:
            self._eventlog.event_trace(
                "trace_summarizer_failed",
                session_id=self._session_id,
                error_type=type(e).__name__,
                traceback=traceback.format_exc(),
            )
            raise SummarizerFailed(f"claude -p timed out after {self._config.timeout_s}s") from e
        except ProcessFailed as e:
            self._eventlog.event_trace(
                "trace_summarizer_failed",
                session_id=self._session_id,
                error_type=type(e).__name__,
                traceback=traceback.format_exc(),
            )
            raise SummarizerFailed("claude CLI not found on PATH") from e

        self._eventlog.event_trace(
            "trace_summarizer_response",
            session_id=self._session_id,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

        if result.returncode != 0:
            raise SummarizerFailed(
                f"claude -p exited {result.returncode}: {result.stderr.strip()!r}"
            )

        text = result.stdout.strip()
        if not text:
            raise SummarizerFailed("claude -p produced empty output")
        return text
