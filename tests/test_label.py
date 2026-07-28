"""Tests for the spoken-label naming ladder.

Covers :func:`audio_recap.label.resolve_label` — the four tiers (``customTitle``
→ ``aiTitle`` → last two cwd path segments → short session-id), quote stripping,
the speakable transforms, and the configurable word cap.
"""

from __future__ import annotations

from audio_recap.label import resolve_label


def test_tier1_custom_title_wins() -> None:
    """The user's CC ``/rename`` beats every other tier, including a live aiTitle."""
    label = resolve_label(
        session_id="abc123",
        cwd="/home/user/projects/myapp",
        custom_title="jean builder",
        session_title="Set up local multi-session",
    )
    assert label == "jean builder"


def test_tier2_ai_title_when_not_renamed() -> None:
    """No rename → CC's topic title: what the session is working on."""
    label = resolve_label(
        session_id="abc123",
        cwd="/home/user/projects/myapp",
        custom_title=None,
        session_title="Set up local multi-session",
    )
    assert label == "Set up local multi-session"


def test_tier3_falls_back_to_last_two_path_segments() -> None:
    """No rename and no aiTitle yet (a session's first turns) → the project path.

    Two segments, not one: an agent-per-directory layout repeats the leaf name
    across projects (``goals/sensei``, ``nbrown-clean/sensei``), so a bare
    basename would give two live sessions the same spoken name.
    """
    assert (
        resolve_label(
            session_id="abc123",
            cwd="/Users/leo/dev/nbrown-clean/sensei",
            custom_title=None,
            session_title=None,
        )
        == "nbrown-clean sensei"
    )
    assert (
        resolve_label(
            session_id="abc123",
            cwd="/Users/leo/dev/goals/sensei",
            custom_title=None,
            session_title=None,
        )
        == "goals sensei"
    )


def test_shallow_cwd_uses_what_it_has() -> None:
    label = resolve_label(session_id="abc123", cwd="/proj", custom_title=None)
    assert label == "proj"


def test_tier4_falls_back_to_short_session_id() -> None:
    """Nothing else available → the first 6 chars of the session-id."""
    label = resolve_label(session_id="abcdef012345", cwd="", custom_title=None)
    assert label == "abcdef"


def test_five_word_cap_keeps_an_ai_title_readable() -> None:
    """The reason the cap moved 3 → 5: at 3 words aiTitle produced fragments."""
    label = resolve_label(
        session_id="abc123",
        cwd="/proj/myapp",
        custom_title=None,
        session_title="Review reports with the finance team",
    )
    assert label == "Review reports with the finance"


def test_root_cwd_falls_through_to_session_id() -> None:
    """``/`` has no segments — don't speak an empty label."""
    label = resolve_label(session_id="abcdef012345", cwd="/", custom_title=None)
    assert label == "abcdef"


def test_global_sentinel_session_id() -> None:
    """The ``_global`` sentinel speaks as "global", not "_globa"."""
    label = resolve_label(session_id="_global", cwd="", custom_title=None)
    assert label == "global"


# ---------------------------------------------------------------------------
# quote stripping — CC stores /rename "goals sensei" WITH the quote characters
# ---------------------------------------------------------------------------


def test_surrounding_quotes_stripped_from_rename() -> None:
    """`/rename "goals sensei"` reaches us as '"goals sensei"' — speak the name."""
    label = resolve_label(
        session_id="abc123",
        cwd="/proj/myapp",
        custom_title='"goals sensei"',
    )
    assert label == "goals sensei"


def test_embedded_and_single_quotes_stripped() -> None:
    label = resolve_label(
        session_id="abc123",
        cwd="/proj/myapp",
        custom_title="'jean' \"builder\"",
    )
    assert label == "jean builder"


def test_quotes_do_not_eat_the_word_budget() -> None:
    """A quoted 5-word rename still yields 5 spoken words, not 4 plus junk."""
    label = resolve_label(
        session_id="abc123",
        cwd="/proj/myapp",
        custom_title='"one two three four five six"',
        max_words=5,
    )
    assert label == "one two three four five"


def test_quote_only_rename_falls_through() -> None:
    """A rename of nothing but quotes is not a name."""
    label = resolve_label(
        session_id="abc123",
        cwd="/Users/leo/dev/goals/sensei",
        custom_title='""',
    )
    assert label == "goals sensei"


# ---------------------------------------------------------------------------
# word cap + speakable transforms
# ---------------------------------------------------------------------------


def test_default_cap_is_five_words() -> None:
    label = resolve_label(
        session_id="abc123",
        cwd="/proj/myapp",
        custom_title="one two three four five six seven",
    )
    assert label == "one two three four five"


def test_cap_is_configurable() -> None:
    """The config knob (``label_max_words``) can widen the cue without a code change."""
    label = resolve_label(
        session_id="abc123",
        cwd="/proj/myapp",
        custom_title="one two three four five six seven",
        max_words=7,
    )
    assert label == "one two three four five six seven"


def test_slug_shaped_rename_is_made_speakable() -> None:
    """A branch-shaped rename reaches ``say`` as words, not punctuation."""
    label = resolve_label(
        session_id="abc123",
        cwd="/proj/myapp",
        custom_title="feat/multi-session",
    )
    assert label == "feat multi-session"


def test_trailing_punctuation_stripped() -> None:
    """The caller adds its own separator, so a trailing "!!!" must go."""
    label = resolve_label(
        session_id="abc123",
        cwd="/proj/myapp",
        custom_title="Fix the bug!!!",
    )
    assert label == "Fix the bug"


def test_transforms_config_is_honored() -> None:
    """An empty transform list (user turned them off) leaves the label raw."""
    label = resolve_label(
        session_id="abc123",
        cwd="/proj/myapp",
        custom_title="feat/multi-session",
        transforms=[],
    )
    assert label == "feat/multi-session"


def test_blank_custom_title_falls_through() -> None:
    label = resolve_label(
        session_id="abc123",
        cwd="/Users/leo/dev/goals/sensei",
        custom_title="   ",
    )
    assert label == "goals sensei"
