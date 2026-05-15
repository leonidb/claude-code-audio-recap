from __future__ import annotations

import pytest

from audio_recap.speakable import apply_transforms

# ---------- code_blocks ----------


def test_code_block_with_language() -> None:
    text = "Here's the fix:\n```python\nprint('hi')\n```\nRun it."
    assert apply_transforms(text) == ("Here's the fix:\nCode block: 1 line of python.\nRun it.")


def test_code_block_without_language() -> None:
    text = "Output was:\n```\nline one\nline two\n```\nDone."
    assert apply_transforms(text) == "Output was:\nCode block: 2 lines.\nDone."


def test_multiple_code_blocks_independent() -> None:
    text = "```py\nfoo\n```\nand\n```js\nbar\nbaz\n```"
    assert apply_transforms(text) == "Code block: 1 line of py.\nand\nCode block: 2 lines of js."


def test_empty_code_block_counts_as_one_line() -> None:
    text = "Nothing:\n```\n\n```\nend"
    assert apply_transforms(text) == "Nothing:\nCode block: 1 line.\nend"


def test_inline_backticks_are_left_alone() -> None:
    text = "Use `foo()` to call the function."
    assert apply_transforms(text) == "Use `foo()` to call the function."


# ---------- urls ----------


def test_bare_url_is_collapsed_to_host() -> None:
    assert (
        apply_transforms("See https://github.com/foo/bar/issues/123 for details.")
        == "See github.com for details."
    )


def test_url_with_query_and_fragment_is_host_only() -> None:
    assert (
        apply_transforms("Docs at https://example.com/path?q=1#section.") == "Docs at example.com."
    )


def test_markdown_link_collapses_to_text() -> None:
    assert (
        apply_transforms("See [the issue](https://github.com/foo/bar/issues/3).")
        == "See the issue."
    )


def test_markdown_link_before_bare_url() -> None:
    text = "[docs](https://example.com) and also https://other.com/page"
    assert apply_transforms(text) == "docs and also other.com"


def test_url_inside_inline_code_is_collapsed() -> None:
    text = "Run `curl https://api.example.com/v1/status` for the probe."
    assert apply_transforms(text) == "Run `curl api.example.com` for the probe."


def test_trailing_paren_after_url_is_preserved() -> None:
    text = "(see https://example.com/x)"
    assert apply_transforms(text) == "(see example.com)"


# ---------- length: never truncated ----------


def test_short_text_unchanged() -> None:
    text = "Just a short message."
    assert apply_transforms(text) == "Just a short message."


def test_long_text_passes_through_verbatim() -> None:
    # Length truncation moved to the summarizer path in hook.py; the
    # speakable transforms must never silently drop content. A 200-word
    # input is returned word-for-word here, even though the hook will
    # decide to summarize it before TTS.
    words = ["word"] * 200
    text = " ".join(words)
    assert apply_transforms(text) == text


# ---------- combinations and toggling ----------


def test_code_block_and_url_together() -> None:
    text = "Try this from https://example.com:\n```bash\nls\n```"
    assert apply_transforms(text) == "Try this from example.com:\nCode block: 1 line of bash."


def test_can_disable_code_blocks_selectively() -> None:
    text = "```py\nfoo\n```"
    assert apply_transforms(text, enabled=["urls"]) == text


def test_can_disable_urls_selectively() -> None:
    text = "See https://example.com/foo"
    assert apply_transforms(text, enabled=["code_blocks"]) == text


def test_empty_enabled_applies_nothing() -> None:
    text = "```py\nfoo\n```\nhttps://example.com"
    # No code_blocks or urls pass → text is unchanged.
    assert apply_transforms(text, enabled=[]) == text


def test_unknown_transform_name_is_silently_ignored() -> None:
    text = "plain prose"
    assert apply_transforms(text, enabled=["not_a_transform"]) == text


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_blank_inputs_round_trip(text: str) -> None:
    assert apply_transforms(text) == text


# ---------- currency ----------


def test_currency_whole_dollars() -> None:
    assert apply_transforms("It costs $20.") == "It costs 20 dollars."


def test_currency_one_dollar_singular() -> None:
    assert apply_transforms("Just $1 today.") == "Just 1 dollar today."


def test_currency_with_cents() -> None:
    assert apply_transforms("Price: $20.50") == "Price: 20 dollars and 50 cents"


def test_currency_one_dollar_one_cent_both_singular() -> None:
    assert apply_transforms("Owe $1.01.") == "Owe 1 dollar and 1 cent."


def test_currency_zero_dollars_is_pure_cents() -> None:
    assert apply_transforms("Saved $0.99.") == "Saved 99 cents."


def test_currency_zero_dollars_one_cent() -> None:
    assert apply_transforms("Found $0.01.") == "Found 1 cent."


def test_currency_with_round_dollars_and_zero_cents_drops_cents() -> None:
    assert apply_transforms("Total $5.00 due.") == "Total 5 dollars due."


def test_currency_multiple_in_one_string() -> None:
    assert (
        apply_transforms("Charged $5 plus $20.50 more.")
        == "Charged 5 dollars plus 20 dollars and 50 cents more."
    )


def test_currency_idempotent() -> None:
    text = "It costs $20.50 and $1."
    once = apply_transforms(text)
    twice = apply_transforms(once)
    assert once == twice
    assert "$" not in once


# ---------- percent ----------


def test_percent_integer() -> None:
    assert apply_transforms("Up 5%.") == "Up 5 percent."


def test_percent_decimal() -> None:
    assert apply_transforms("Margin of 12.5%.") == "Margin of 12.5 percent."


def test_percent_one() -> None:
    # "1 percent" not "1 percents" — English uses bare "percent" for any number.
    assert apply_transforms("Only 1% off.") == "Only 1 percent off."


def test_percent_multiple() -> None:
    assert apply_transforms("From 5% to 25%.") == "From 5 percent to 25 percent."


def test_percent_idempotent() -> None:
    text = "5% improvement, 12.5% spread."
    once = apply_transforms(text)
    twice = apply_transforms(once)
    assert once == twice
    assert "%" not in once


# ---------- symbols ----------


def test_symbol_arrow_to() -> None:
    assert apply_transforms("355w -> 130w improvement.") == "355w to 130w improvement."


def test_symbol_double_arrow_becomes() -> None:
    assert apply_transforms("X => Y mapping.") == "X becomes Y mapping."


def test_symbol_lte() -> None:
    assert apply_transforms("Need x <= 10.") == "Need x less than or equal to 10."


def test_symbol_gte() -> None:
    assert apply_transforms("Need x >= 10.") == "Need x greater than or equal to 10."


def test_symbol_neq() -> None:
    assert apply_transforms("Check a != b.") == "Check a not equal to b."


def test_symbol_ampersand_word_separator() -> None:
    assert apply_transforms("Foo & Bar working.") == "Foo and Bar working."


def test_symbol_arrow_inside_identifier_is_left_alone() -> None:
    # No surrounding whitespace → not the "narrative arrow" shape; leave it
    # so inline-code identifiers like `e->next` aren't mangled.
    assert apply_transforms("Use `e->next` to walk.") == "Use `e->next` to walk."


def test_symbol_ampersand_inside_identifier_is_left_alone() -> None:
    assert apply_transforms("Pass `&ptr` along.") == "Pass `&ptr` along."


def test_symbol_tilde_before_digit_is_approximately() -> None:
    # ``time_units`` runs before ``symbols``, so the unit shorthand
    # is expanded first and the tilde still narrates as
    # "approximately" before the digit.
    assert apply_transforms("Took ~50ms.") == "Took approximately 50 milliseconds."


def test_symbol_tilde_in_path_is_left_alone() -> None:
    # ~/path is a home-directory marker, not an approximation. The pattern
    # only matches `~` directly followed by a digit, so this stays intact.
    assert apply_transforms("Logs at ~/.claude/x") == "Logs at ~/.claude/x"


def test_symbol_idempotent() -> None:
    text = "X => Y, 355w -> 130w, Foo & Bar, ~50ms, x <= 10"
    once = apply_transforms(text)
    twice = apply_transforms(once)
    assert once == twice


# ---------- combinations and order ----------


def test_currency_and_percent_in_one_string() -> None:
    assert apply_transforms("Saved $20 (about 5%).") == "Saved 20 dollars (about 5 percent)."


def test_currency_inside_code_block_is_swallowed_by_block_summary() -> None:
    # code_blocks runs before currency, so the `$20` inside a fenced block
    # never reaches the currency transform.
    text = "Output:\n```\nprice: $20.50\n```\nDone."
    assert apply_transforms(text) == "Output:\nCode block: 1 line.\nDone."


def test_url_query_ampersand_does_not_trigger_symbol_rule() -> None:
    # urls runs before symbols, so the query-string `&` is gone before the
    # symbol rule sees it.
    text = "See https://example.com/x?a=1&b=2 for the call."
    assert apply_transforms(text) == "See example.com for the call."


def test_full_stack_idempotent() -> None:
    text = "Saved $20.50 (5%) — see https://example.com — X => Y."
    once = apply_transforms(text)
    twice = apply_transforms(once)
    assert once == twice


def test_can_disable_currency_selectively() -> None:
    text = "$20"
    assert apply_transforms(text, enabled=["urls"]) == text


def test_can_disable_percent_selectively() -> None:
    text = "5%"
    assert apply_transforms(text, enabled=["urls"]) == text


def test_can_disable_symbols_selectively() -> None:
    text = "X => Y"
    assert apply_transforms(text, enabled=["urls"]) == text


# ---------- 068: time_units ----------


def test_time_units_seconds() -> None:
    assert apply_transforms("Build took 15s.") == "Build took 15 seconds."


def test_time_units_milliseconds() -> None:
    assert apply_transforms("Pinged in 200ms.") == "Pinged in 200 milliseconds."


def test_time_units_decimal_value() -> None:
    assert apply_transforms("Slept 1.5h.") == "Slept 1.5 hours."


def test_time_units_singular_for_value_one() -> None:
    # 1 second / 1 minute reads better than "1 seconds".
    assert apply_transforms("Wait 1s.") == "Wait 1 second."
    assert apply_transforms("Wait 1m.") == "Wait 1 minute."
    assert apply_transforms("Wait 1d.") == "Wait 1 day."


def test_time_units_all_supported_letters() -> None:
    assert (
        apply_transforms("ms=200ms s=15s m=5m h=2h d=3d.")
        == "ms=200 milliseconds s=15 seconds m=5 minutes h=2 hours d=3 days."
    )


def test_time_units_word_boundary_var_prefix_skipped() -> None:
    # ``var15s`` must NOT match — the leading ``r`` makes ``\b\d``
    # at position 3 fail; the regex stays anchored to integer
    # boundaries.
    assert apply_transforms("var15s should stay.") == "var15s should stay."


def test_time_units_word_boundary_suffix_skipped() -> None:
    # ``5days`` would otherwise match ``5d`` then leave ``ays``.
    # ``\b`` after the unit letter requires non-word; the trailing
    # ``a`` blocks it.
    assert apply_transforms("Took 5days to ship.") == "Took 5days to ship."


def test_time_units_short_hash_left_alone() -> None:
    # ``b35a460`` is a git hash — the ``5a`` digits don't match a
    # unit letter, so the whole token survives.
    assert apply_transforms("Pushed b35a460 today.") == "Pushed b35a460 today."


def test_time_units_idempotent() -> None:
    text = "Build 15s, ping 200ms, wait 5m."
    once = apply_transforms(text)
    assert apply_transforms(once) == once


def test_time_units_can_disable_selectively() -> None:
    assert apply_transforms("15s", enabled=["urls"]) == "15s"


# ---------- 068: ratios ----------


def test_ratios_simple() -> None:
    assert apply_transforms("Coverage is 3/5 on the path.") == "Coverage is 3 of 5 on the path."


def test_ratios_two_digit() -> None:
    assert apply_transforms("Turn 7/16 fired.") == "Turn 7 of 16 fired."


def test_ratios_three_digit_max() -> None:
    assert apply_transforms("Done 99/100.") == "Done 99 of 100."


def test_ratios_four_plus_digits_skipped() -> None:
    # Year-like (``2024/12/15``) — the negative lookbehind for ``/``
    # rejects the candidate ``12/15`` (preceded by ``/``); the leading
    # ``2024`` exceeds ``\d{1,3}`` so it can't anchor the ratio either.
    assert apply_transforms("Born 2024/12/15.") == "Born 2024 12 15."
    # The result above is from the path transform tokenizing the
    # date; ratio-of-the-date does NOT fire (which is what we want).
    # Idempotent re-run shouldn't introduce ``of``.
    once = apply_transforms("Born 2024/12/15.")
    assert apply_transforms(once) == once
    assert " of " not in once


def test_ratios_version_string_skipped() -> None:
    # ``1.2.3/2.4.5`` — adjacent dots disqualify both sides.
    assert apply_transforms("Run 1.2.3/2.4.5.") == "Run 1.2.3 2.4.5."
    assert " of " not in apply_transforms("Run 1.2.3/2.4.5.")


def test_ratios_inside_path_skipped_path_wins() -> None:
    # ``repos/owner/3/5`` — adjacent ``/`` on both sides disqualifies
    # the ratio match; the path transform tokenizes the whole run.
    assert apply_transforms("gh api repos/owner/3/5") == "gh api repos owner 3 5"


def test_ratios_idempotent() -> None:
    once = apply_transforms("3/5 then 7/16")
    assert apply_transforms(once) == once


def test_ratios_can_disable_selectively() -> None:
    assert apply_transforms("3/5", enabled=["urls"]) == "3/5"


# ---------- 068: paths ----------


def test_paths_absolute_two_segments() -> None:
    assert apply_transforms("I hit /events/ack here.") == "I hit events ack here."


def test_paths_with_dashes_and_underscores() -> None:
    assert apply_transforms("cd /tmp/run-001/") == "cd tmp run-001"


def test_paths_relative_multi_segment() -> None:
    assert (
        apply_transforms("gh api repos/owner/repo/issues/123")
        == "gh api repos owner repo issues 123"
    )


def test_paths_file_with_extension_kept() -> None:
    # ``.py`` is a single ``[\w.-]+`` segment; the file path
    # ``audio_recap/hook.py`` is one segment-plus-segment so the
    # slash collapses but the dot+ext stays a single token. The
    # trailing ``:448-470`` is non-path-ish and survives untouched.
    assert (
        apply_transforms("Verified at audio_recap/hook.py:448-470.")
        == "Verified at audio_recap hook.py:448-470."
    )


def test_paths_single_segment_left_alone() -> None:
    # ``/tmp`` alone (no second segment) doesn't trigger.
    assert apply_transforms("cd /tmp") == "cd /tmp"


def test_paths_home_directory_marker_preserved() -> None:
    # Leading ``~/`` is a home-directory marker; the lookbehind
    # excludes ``~`` so the home-dir path stays intact (the user
    # gets to keep the meaning).
    assert apply_transforms("Logs at ~/.claude/x") == "Logs at ~/.claude/x"


def test_paths_url_already_collapsed_by_urls_transform() -> None:
    # The ``urls`` transform runs first and collapses URLs to host;
    # ``paths`` then sees only ``example.com`` which is one segment.
    assert (
        apply_transforms("See https://example.com/a/b for details.")
        == "See example.com for details."
    )


def test_paths_idempotent() -> None:
    once = apply_transforms("Run /events/ack and check /tmp/foo/bar/")
    assert apply_transforms(once) == once


def test_paths_can_disable_selectively() -> None:
    assert apply_transforms("/events/ack", enabled=["urls"]) == "/events/ack"


# ---------- 068: composition (all three new transforms together) ----------


def test_composition_path_with_unit_and_ratio_inside() -> None:
    # The 066 multi-pattern bomb input — exercises time_units,
    # ratios (skipped via lookbehind), and paths in one go.
    assert (
        apply_transforms("I cd'd into /tmp/run-15s/3-5/ and ran pytest for 200ms.")
        == "I cd'd into tmp run-15 seconds 3-5 and ran pytest for 200 milliseconds."
    )


def test_composition_recap_style_message() -> None:
    # Shape of a real recap: a tool-use trace narrating gh + paths.
    assert (
        apply_transforms("Fetched repos/anthropics/claude-code/issues/38620 in 1.5s.")
        == "Fetched repos anthropics claude-code issues 38620 in 1.5 seconds."
    )


def test_composition_ratio_then_path() -> None:
    assert (
        apply_transforms("Turn 7/16 fired on /events/ack.") == "Turn 7 of 16 fired on events ack."
    )
