"""Spoken session label — the naming ladder.

When two+ sessions are live, the Stop hook prepends a short spoken label to the
narration so the listener knows which session they're hearing. This module
resolves that label text.

**Naming ladder** (:func:`resolve_label`, in order):

1. **``customTitle``** — the name the user gave the session with Claude Code's
   own ``/rename``, read from the JSONL transcript
   (:attr:`audio_recap.payload.TurnPayload.custom_title`). The primary source:
   it's the one name the user actually chose.
2. **``aiTitle``** — CC's auto-generated topic title ("Set up local
   multi-session"). Not an identity, but what a session is *working on* is a
   better cue than where it lives, and at the 5-word cap it reads as a phrase
   rather than the fragments a tighter cap produced.
3. **The last two path segments of the cwd** — "goals sensei",
   "nbrown-clean sensei". This tier carries a session's first turns, before CC
   has generated an ``aiTitle``. Two segments, not one: an agent-per-directory
   layout puts the same leaf name (``sensei``, ``builder``) under many
   projects, so a bare basename would give several live sessions the same
   spoken name.
4. **Short session-id** — first 6 chars; last resort, never empty/opaque.

Every tier is made speakable and then capped to ``max_words``
(:attr:`audio_recap.config.Config.label_max_words`, default 5).
"""

from __future__ import annotations

import os
import re

from audio_recap.speakable import apply_transforms

# Fallback cap, used when no config is threaded in (tests, direct callers). The
# real default lives in :attr:`audio_recap.config.Config.label_max_words`.
_DEFAULT_MAX_WORDS = 5

# How many trailing path segments the cwd tier speaks. See the module docstring:
# one segment collides across an agent-per-directory layout.
_CWD_SEGMENTS = 2

# Claude Code stores ``/rename "goals sensei"`` with the quote characters in the
# title, so a rename arrives as '"goals sensei"'. Speak the name, not the
# punctuation — and don't let quotes eat the word budget. Smart quotes are
# spelled as escapes (a terminal paste can carry them).
_QUOTES = re.compile("[\"'\u2018\u2019\u201c\u201d]")

# Trailing punctuation stripped from a resolved label before the caller adds
# its own separator ("myapp" + ". " + recap, never "myapp.." ...).
_TRAILING_PUNCT = re.compile(r"[\s.,;:!?-]+$")


def _cap_words(text: str, max_words: int) -> str:
    """Trim to ``max_words`` words and strip trailing punctuation.

    Returns ``""`` for empty/whitespace input so the caller can fall through
    to the next ladder tier.
    """

    words = text.split()
    if not words:
        return ""
    capped = " ".join(words[:max_words])
    return _TRAILING_PUNCT.sub("", capped)


def _cwd_tail(cwd: str) -> str:
    """The last :data:`_CWD_SEGMENTS` path segments of ``cwd``, space-joined.

    ``/Users/leo/dev/goals/sensei`` → ``"goals sensei"``. A shallower path
    yields whatever segments it has; ``""``/``"/"`` yields ``""`` so the caller
    falls through to the session-id tier.
    """

    segments = [part for part in cwd.split(os.sep) if part]
    return " ".join(segments[-_CWD_SEGMENTS:])


def resolve_label(
    *,
    session_id: str,
    cwd: str,
    custom_title: str | None,
    session_title: str | None = None,
    transforms: list[str] | None = None,
    max_words: int = _DEFAULT_MAX_WORDS,
) -> str:
    """Resolve the spoken label via the naming ladder. Always returns non-empty.

    Order: ``custom_title`` (the user's ``/rename``) → ``session_title`` (CC's
    auto-generated topic title) → the last two cwd path segments → a short
    session-id. Each candidate is de-quoted, made speakable, and capped to
    ``max_words``; the first non-empty candidate wins.

    ``transforms`` is the caller's ``speakable_transforms`` config (``None``
    enables all of them). The narration itself is transformed upstream in the
    pipeline, but the label is prepended after that, so it has to be transformed
    here or a rename like ``feat/multi-session`` reaches ``say`` with its
    punctuation intact. De-quoting and transforming BEFORE the cap keeps the cap
    honest: it counts the words that will actually be spoken.
    """

    for candidate in (custom_title, session_title, _cwd_tail(cwd)):
        if not candidate:
            continue
        spoken = apply_transforms(_QUOTES.sub("", candidate), transforms)
        capped = _cap_words(spoken, max_words)
        if capped:
            return capped
    # Last resort — a short session-id slice, never empty/opaque.
    if session_id == "_global":
        return "global"
    short = session_id[:6]
    return short or "session"


__all__ = ["resolve_label"]
