"""Recap backend: ``claude -p`` subprocess against Haiku.

Shells out to ``claude -p --model <model>`` with a one-shot
summarization prompt. Enforces a configurable timeout and signals any
failure via :class:`RecapFailed` — this backend never produces an
empty string; it either returns a usable sentence or raises.

The prompt describes *actions only* (edited X, ran Y); the assistant's
user-facing reply text is spoken verbatim by the TTS stage, so we
don't want it echoed in the recap. The prompt also caps the recap at
20 words target / 30 words hard, biases for subject-omitted past
tense and aggregation over enumeration, and bans preambles, code
fences, quotes, and bullets. Post-processing strips surrounding
quotes / leading bullet markers and enforces the 30-word hard cap if
the model overshoots.
"""

from __future__ import annotations

import json
import re
import subprocess
import traceback

from audio_recap.config import Recap as RecapConfig
from audio_recap.eventlog import EventLog
from audio_recap.payload import TurnPayload
from audio_recap.process import ProcessFailed, ProcessRunner
from audio_recap.recap import RecapFailed

_PROMPT_TEMPLATE = """\
You will receive a JSON array of tool uses from one Claude Code
turn. Reply with exactly one short sentence (max 20 words), past
tense, subject omitted ("Edited 3 files, ran the test suite, 2
failed."). Aggregate similar actions — say "edited 3 files", never
list filenames. Mention only errors that stopped progress; skip
errors Claude retried past or worked around. Do not describe
Claude's user-facing reply — that is spoken separately. Tool names
sometimes follow internal naming conventions (e.g.
``service__action``, ``some_tool_name``) where underscores or
prefixes do not speak naturally and the raw name carries no
information about what was done. Never echo a tool name verbatim
— always describe the action in plain English. For tools whose
role is delivery (sending a reply, posting a message, returning a
result), prefer phrases like "replied" or "sent a reply" over the
tool name itself. Render numbers, currencies, and percentages as
spoken English — e.g., "twenty dollars", "five percent". No
preamble, no code fences, no quotes, no bullet points.

Tool uses:
{tool_uses_json}
"""

# Hard-cap words: PRD targets ≤20 words; 30 is the cap before truncate+ellipsis.
# Set generously enough that a well-formed model response never trips it; set
# tightly enough that a 100-word essay can't slip through to TTS.
_MAX_WORDS_HARD = 30


# Curly quote codepoints (U+201C/D/2018/9) are derived via chr() so the
# literal ambiguous characters never appear in source — ruff's RUF001 rule
# flags them otherwise and we don't want per-line noqa noise.
_LEFT_DOUBLE_QUOTE = chr(0x201C)
_RIGHT_DOUBLE_QUOTE = chr(0x201D)
_LEFT_SINGLE_QUOTE = chr(0x2018)
_RIGHT_SINGLE_QUOTE = chr(0x2019)

# Bullet (U+2022) likewise via chr().
_BULLET = chr(0x2022)
_LEADING_BULLET = re.compile(rf"^[-*{_BULLET}]\s+")

# Ordered pairs of (opener, closer) the model might wrap the recap in. Tested
# in order; the first match wins.
_QUOTE_PAIRS: tuple[tuple[str, str], ...] = (
    ('"', '"'),
    ("'", "'"),
    (_LEFT_DOUBLE_QUOTE, _RIGHT_DOUBLE_QUOTE),
    (_LEFT_SINGLE_QUOTE, _RIGHT_SINGLE_QUOTE),
)


def _strip_quotes_and_bullets(text: str) -> str:
    """Remove a single layer of surrounding quotes and a leading bullet marker.

    Defensive post-processing: the prompt asks for none of these, but a model
    that wraps the answer in `"..."` or prepends `- ` once in a while
    shouldn't bleed those characters into TTS.
    """

    text = text.strip()
    if len(text) >= 2:
        for opener, closer in _QUOTE_PAIRS:
            if text[0] == opener and text[-1] == closer:
                text = text[1:-1].strip()
                break
    return _LEADING_BULLET.sub("", text)


def _truncate_to_one_sentence(text: str) -> str:
    text = " ".join(text.split())
    # Cut at the first sentence boundary (". ") — leaves a trailing period on
    # the remaining clause. If the model produced one sentence that happens
    # to end with just a period at EOL, leave it alone.
    if ". " in text:
        head, _ = text.split(". ", 1)
        return head + "."
    return text


def _enforce_word_cap(text: str, max_words: int = _MAX_WORDS_HARD) -> str:
    """Hard cap. PRD: target ≤20 words, hard 30 with truncate + ellipsis."""

    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + " ..."


class ClaudePRecap:
    def __init__(
        self,
        config: RecapConfig,
        session_id: str,
        runner: ProcessRunner,
        eventlog: EventLog,
    ) -> None:
        self._config = config
        # ``session_id`` is carried only for TRACE entries — the recap
        # logic itself doesn't depend on per-session state. It makes
        # trace lines filterable by ``session_id=<id>``.
        self._session_id = session_id
        self._runner = runner
        self._eventlog = eventlog

    def generate(self, turn: TurnPayload) -> str:
        tool_uses = [{"name": t.name, "id": t.tool_id, "input": t.input} for t in turn.tool_uses]
        prompt = _PROMPT_TEMPLATE.format(tool_uses_json=json.dumps(tool_uses, indent=2))
        self._eventlog.event_trace(
            "trace_recap_prompt",
            session_id=self._session_id,
            backend="claude_p",
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
                "trace_recap_failed",
                session_id=self._session_id,
                backend="claude_p",
                error_type=type(e).__name__,
                traceback=traceback.format_exc(),
            )
            raise RecapFailed(f"claude -p timed out after {self._config.timeout_s}s") from e
        except ProcessFailed as e:
            self._eventlog.event_trace(
                "trace_recap_failed",
                session_id=self._session_id,
                backend="claude_p",
                error_type=type(e).__name__,
                traceback=traceback.format_exc(),
            )
            raise RecapFailed("claude CLI not found on PATH") from e

        self._eventlog.event_trace(
            "trace_recap_response",
            session_id=self._session_id,
            backend="claude_p",
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )

        if result.returncode != 0:
            raise RecapFailed(f"claude -p exited {result.returncode}: {result.stderr.strip()!r}")

        text = result.stdout.strip()
        if not text:
            raise RecapFailed("claude -p produced empty output")

        text = _strip_quotes_and_bullets(text)
        text = _truncate_to_one_sentence(text)
        return _enforce_word_cap(text)
