"""Text → speakable-text transforms.

Eight transforms, no length truncation:

1. ``code_blocks`` — Markdown fenced code blocks (`````lang
   ... `````) replaced by a short spoken summary
   ("Code block: N lines of lang." or "Code block: N line." when
   language is omitted). Inline backticks are left alone.
2. ``urls`` — raw URLs collapsed to their host (``https://github.com/
   foo/bar/issues/3`` → ``github.com``). Markdown links ``[text](url)``
   collapse to their link text, which is strictly better for
   narration than the URL would be. URLs inside inline-code
   backticks are treated the same as bare URLs.
3. ``time_units`` — ``\\d+(ms|s|m|h|d)`` shorthand expanded to
   spoken form (``15s`` → ``"15 seconds"``, ``200ms`` →
   ``"200 milliseconds"``, ``1.5h`` → ``"1.5 hours"``). Conservative
   unit set; ambiguous suffixes (``b`` / ``B`` for bytes,
   ``K`` / ``M`` for SI scales) are out of scope. Singular when the
   value is exactly 1.
4. ``ratios`` — ``\\b\\d{1,3}/\\d{1,3}\\b`` → ``"N of M"``. Skips
   year-like and version-like contexts (``2024/12/15``,
   ``1.2.3/2.4.5``) by refusing to match when a digit, dot, or slash
   sits adjacent to the candidate.
5. ``paths`` — multi-segment slash-separated tokens (``/events/ack``,
   ``audio_recap/hook.py``, ``repos/owner/repo/issues/123``) get
   their slashes replaced by spaces so ``say`` doesn't read each
   ``/`` as "slash". Single-token paths (``/tmp``) are left alone.
6. ``currency`` — ``$N`` / ``$N.YY`` → ``"N dollars"`` /
   ``"N dollars and Y cents"``, with proper singular/plural
   ("1 dollar", "1 cent") and a pure-cents form ("$0.99" →
   ``"99 cents"``). macOS ``say`` otherwise reads "$20" as
   "dollar sign twenty".
7. ``percent`` — ``N%`` / ``N.M%`` → ``"N percent"``.
8. ``symbols`` — narrative ASCII symbols ``=>``, ``<=``, ``>=``,
   ``!=``, ``->``, ``&``, and ``~`` (when used as an approximation
   marker before a digit). Only expanded when whitespace-bounded so
   code-like identifiers (``e->next``, ``x>=2``) inside inline
   backticks aren't mangled.

Every transform is **idempotent** — running the pass twice produces
the same output as running it once.

Length is handled upstream: the hook routes long messages through
``claude -p`` summarization (see ``audio_recap.summarizer``) instead
of truncating mid-sentence. Listeners can't skim, so chopping a
message to a fixed word count silently strips information and ends
mid-thought; summarizing preserves intent at audio length.

:func:`apply_transforms` is the single entry point; ``enabled`` is a
list of transform names drawn from ``Config.speakable_transforms``.
``None`` enables all known transforms.
"""

from __future__ import annotations

import re

_CODE_BLOCK_PATTERN = re.compile(
    r"```(\w+)?\n(.*?)(?:\n```|```)",
    flags=re.DOTALL,
)

_MARKDOWN_LINK_PATTERN = re.compile(
    r"\[([^\]]+)\]\(https?://[^)]+\)",
)

# Bare URL: match until whitespace, closing bracket/paren, or backtick.
_BARE_URL_PATTERN = re.compile(r"https?://[^\s)\]`]+")


def _format_code_block(match: re.Match[str]) -> str:
    lang = match.group(1)
    body = match.group(2)
    lines = body.splitlines()
    # An empty block body is still "1 line" to a listener — they hear
    # an opened-and-closed block, not zero content. Round up.
    n = max(1, len(lines))
    line_word = "line" if n == 1 else "lines"
    if lang:
        return f"Code block: {n} {line_word} of {lang}."
    return f"Code block: {n} {line_word}."


def _domain(url: str) -> str:
    """Extract the host from a URL string."""

    after_scheme = url.split("://", 1)[1] if "://" in url else url
    return after_scheme.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]


def _transform_code_blocks(text: str) -> str:
    return _CODE_BLOCK_PATTERN.sub(_format_code_block, text)


_URL_TRAILING_PUNCT = ".,;:!?"


def _replace_bare_url(match: re.Match[str]) -> str:
    url = match.group(0)
    # Preserve sentence-end punctuation that grammatically follows the URL
    # ("See https://example.com." → "See example.com."). The URL regex
    # can't distinguish a period inside a path from one ending a sentence,
    # so strip trailing punctuation after the fact and put it back outside
    # the substitution.
    trailing = ""
    while url and url[-1] in _URL_TRAILING_PUNCT:
        trailing = url[-1] + trailing
        url = url[:-1]
    return _domain(url) + trailing


def _transform_urls(text: str) -> str:
    # Markdown links first: [label](https://...) → label. Otherwise the bare-URL
    # pass below would see the URL inside the parens and replace it with
    # the domain, leaving "[label](domain)".
    text = _MARKDOWN_LINK_PATTERN.sub(lambda m: m.group(1), text)
    text = _BARE_URL_PATTERN.sub(_replace_bare_url, text)
    return text


# Currency: $N or $N.YY. The leading `$` is the only literal anchor, so
# once expanded ("20 dollars") the regex no longer matches — idempotent.
_CURRENCY_PATTERN = re.compile(r"\$(\d+)(?:\.(\d{2}))?")


def _format_currency(match: re.Match[str]) -> str:
    dollars = int(match.group(1))
    cents_str = match.group(2)
    cents = int(cents_str) if cents_str else 0
    if dollars == 0 and cents > 0:
        # "$0.99" → "99 cents" reads better aloud than "0 dollars and 99 cents".
        cent_word = "cent" if cents == 1 else "cents"
        return f"{cents} {cent_word}"
    dollar_word = "dollar" if dollars == 1 else "dollars"
    if cents == 0:
        return f"{dollars} {dollar_word}"
    cent_word = "cent" if cents == 1 else "cents"
    return f"{dollars} {dollar_word} and {cents} {cent_word}"


def _transform_currency(text: str) -> str:
    return _CURRENCY_PATTERN.sub(_format_currency, text)


# Percent: integer or decimal followed by `%`. Idempotent — `%` is gone
# after substitution.
_PERCENT_PATTERN = re.compile(r"(\d+(?:\.\d+)?)%")


def _transform_percent(text: str) -> str:
    return _PERCENT_PATTERN.sub(lambda m: f"{m.group(1)} percent", text)


# Time-unit shorthand. Word-boundary anchored so ``var15s`` and
# ``b35a460`` don't trigger; the unit set stays conservative
# (ms/s/m/h/d) so ``5b`` / ``5B`` (bytes / scale) and bare ``5K``
# don't collide with size-or-scale interpretations the user actually
# means. Idempotent — the unit letter is gone after substitution.
_TIME_UNIT_PATTERN = re.compile(r"\b(\d+(?:\.\d+)?)(ms|s|m|h|d)\b")
_TIME_UNIT_SINGULAR: dict[str, str] = {
    "ms": "millisecond",
    "s": "second",
    "m": "minute",
    "h": "hour",
    "d": "day",
}


def _format_time_unit(match: re.Match[str]) -> str:
    num = match.group(1)
    unit = match.group(2)
    singular = _TIME_UNIT_SINGULAR[unit]
    try:
        is_one = float(num) == 1.0
    except ValueError:
        is_one = False
    return f"{num} {singular if is_one else singular + 's'}"


def _transform_time_units(text: str) -> str:
    return _TIME_UNIT_PATTERN.sub(_format_time_unit, text)


# Ratios. The guards reject candidates where the adjacent context
# would make this a longer numeric / dotted / slashed run:
# - ``(?<![\d./])`` rejects a digit, dot, or slash immediately before.
#   Catches a 4+-digit year (``2024/12/15`` — the ``12/15`` candidate
#   has ``/`` before), a version-string segment (``1.2.3/2.4.5`` —
#   ``.`` before), and a path-internal ratio (``repos/owner/3/5`` —
#   ``/`` before, path transform takes over).
# - ``(?!\d)`` rejects a digit immediately after (number continues).
# - ``(?![./]\d)`` rejects ``.\d`` / ``/\d`` after — but ALLOWS a
#   sentence-end ``.`` or trailing space since those don't carry a
#   digit. Without this the lookahead would decline ``"Coverage 3/5."``
#   for looking version-like, which is wrong on its own.
_RATIO_PATTERN = re.compile(r"(?<![\d./])(\d{1,3})/(\d{1,3})(?!\d)(?![./]\d)")


def _transform_ratios(text: str) -> str:
    return _RATIO_PATTERN.sub(lambda m: f"{m.group(1)} of {m.group(2)}", text)


# Paths. A single segment with no slash (``/tmp``, ``foo``) is left
# alone — only multi-segment slash-joined runs get rewritten. The
# lookbehind keeps the regex from re-entering the middle of an
# already-matched path on idempotent re-runs and from triggering
# inside surrounding identifiers; ``~`` is excluded too so a leading
# ``~/.claude/x`` home-directory marker stays intact (the
# ``symbols`` rule will narrate the ``~`` separately when it sits
# next to a digit). The optional leading and trailing slashes are
# absorbed so ``cd /tmp/run-001/`` reads as ``cd tmp run-001``
# rather than ``cd  tmp run-001 ``.
_PATH_PATTERN = re.compile(r"(?<![\w/~.])(?:/)?[\w.-]+(?:/[\w.-]+)+/?")


def _replace_path(match: re.Match[str]) -> str:
    return match.group(0).replace("/", " ").strip()


def _transform_paths(text: str) -> str:
    return _PATH_PATTERN.sub(_replace_path, text)


_SYMBOL_LOOKUP: dict[str, str] = {
    "=>": "becomes",
    "<=": "less than or equal to",
    ">=": "greater than or equal to",
    "!=": "not equal to",
    "->": "to",
    "&": "and",
}

# Only match when whitespace (or string boundary) sits on both sides.
# Narrative use ("355w -> 130w", "Foo & Bar") expands; code-like uses
# inside inline backticks (`e->next`, `x>=2`, `&ptr`) stay literal.
# The leading boundary is captured (Python's stdlib `re` rejects
# variable-width lookbehinds) and re-emitted in the replacement.
_SYMBOL_PATTERN = re.compile(r"(^|\s)(=>|<=|>=|!=|->|&)(?=\s|$)")

# `~` only as an approximation marker (`~50ms` → "approximately 50ms").
# Standalone `~` and path roots like `~/file` are left alone so the
# transform doesn't mangle home-directory paths in narrated logs.
_TILDE_APPROX_PATTERN = re.compile(r"~(?=\d)")


def _transform_symbols(text: str) -> str:
    text = _SYMBOL_PATTERN.sub(lambda m: m.group(1) + _SYMBOL_LOOKUP[m.group(2)], text)
    text = _TILDE_APPROX_PATTERN.sub("approximately ", text)
    return text


_ALL_TRANSFORMS = (
    "code_blocks",
    "urls",
    "time_units",
    "ratios",
    "paths",
    "currency",
    "percent",
    "symbols",
)


def apply_transforms(text: str, enabled: list[str] | None = None) -> str:
    """Apply configured text-to-speech transforms.

    ``enabled`` is a list of transform names (``"code_blocks"``,
    ``"urls"``, ``"time_units"``, ``"ratios"``, ``"paths"``,
    ``"currency"``, ``"percent"``, ``"symbols"``). ``None`` enables all
    known transforms. Unknown names are ignored silently so a config
    file can carry future transform names without blowing up an older
    binary.

    Order matters: ``code_blocks`` runs first so currency/percent
    inside fenced blocks are not expanded (the block becomes "Code
    block: N lines"). ``urls`` runs before ``paths`` so URL paths are
    already collapsed to a host (``example.com``) and the path rule
    doesn't re-tokenize them. ``ratios`` runs before ``paths`` so a
    standalone ``3/5`` becomes ``"3 of 5"`` while ``repos/owner/3/5``
    is path-tokenized as a whole; the ratio's negative lookbehind for
    ``/`` declines to fire on the path-internal ``3/5``. ``urls``
    runs before ``symbols`` so query-string ampersands inside URLs
    are gone before the ``&`` rule sees them.
    """

    names = list(_ALL_TRANSFORMS) if enabled is None else enabled
    if "code_blocks" in names:
        text = _transform_code_blocks(text)
    if "urls" in names:
        text = _transform_urls(text)
    if "time_units" in names:
        text = _transform_time_units(text)
    if "ratios" in names:
        text = _transform_ratios(text)
    if "paths" in names:
        text = _transform_paths(text)
    if "currency" in names:
        text = _transform_currency(text)
    if "percent" in names:
        text = _transform_percent(text)
    if "symbols" in names:
        text = _transform_symbols(text)
    return text
