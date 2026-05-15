"""Unit tests for the ``--session-id`` runtime sentinel rewrite.

The slash-command path passes ``--session-id "${CLAUDE_SESSION_ID}"``
to ``python -m audio_recap``; CC normally substitutes the literal
token with the active session UUID before Claude reads the markdown
body. If a future CC version stops doing the substitution,
``audio_recap.__main__._rewrite_session_sentinel`` sees the raw token
(or an empty string) and rewrites it to ``_global``, returning a
``warned`` flag the caller surfaces to stderr.

Pre-082 these tests shelled out the bash wrapper end-to-end; the
sentinel logic now lives in Python so the tests are pure unit.
"""

from __future__ import annotations

import pytest

from audio_recap.__main__ import (
    _SENTINEL_FALLBACK,
    _SENTINEL_TOKEN,
    _rewrite_session_sentinel,
)


def test_real_uuid_passes_through_unchanged() -> None:
    argv = ["--session-id", "real-session-uuid", "--cwd", "/proj", "on"]
    rewritten, warned = _rewrite_session_sentinel(argv)
    assert rewritten == argv
    assert warned is False


def test_unsubstituted_literal_token_rewrites_to_global() -> None:
    argv = ["--session-id", _SENTINEL_TOKEN, "--cwd", "/proj", "on"]
    rewritten, warned = _rewrite_session_sentinel(argv)
    assert rewritten == ["--session-id", _SENTINEL_FALLBACK, "--cwd", "/proj", "on"]
    assert warned is True


def test_empty_string_rewrites_to_global() -> None:
    argv = ["--session-id", "", "--cwd", "/proj", "on"]
    rewritten, warned = _rewrite_session_sentinel(argv)
    assert rewritten == ["--session-id", _SENTINEL_FALLBACK, "--cwd", "/proj", "on"]
    assert warned is True


def test_no_session_id_flag_no_warning() -> None:
    argv = ["status"]
    rewritten, warned = _rewrite_session_sentinel(argv)
    assert rewritten == argv
    assert warned is False


def test_session_id_at_tail_with_no_value_is_left_alone() -> None:
    """A trailing ``--session-id`` with no following value is treated as a
    spurious arg — left in argv so downstream parsing rejects the form."""

    argv = ["on", "--session-id"]
    rewritten, warned = _rewrite_session_sentinel(argv)
    assert rewritten == argv
    assert warned is False


@pytest.mark.parametrize("verb", ["on", "off", "status"])
def test_session_id_value_is_preserved_for_each_verb(verb: str) -> None:
    """The arg-walk doesn't mangle a real UUID for any of the verbs."""

    argv = ["--session-id", "abc-123-def", "--cwd", "/x", verb]
    rewritten, warned = _rewrite_session_sentinel(argv)
    assert rewritten == argv
    assert warned is False


def test_session_id_can_appear_after_other_flags() -> None:
    """Order-independent: ``--cwd`` before ``--session-id`` works."""

    argv = ["--cwd", "/proj", "--session-id", "uuid-x", "off"]
    rewritten, warned = _rewrite_session_sentinel(argv)
    assert rewritten == argv
    assert warned is False


def test_other_args_pass_through_when_only_session_id_is_rewritten() -> None:
    """Extra positional / flag args around the rewritten ``--session-id`` survive."""

    argv = [
        "--cwd",
        "/proj",
        "--session-id",
        _SENTINEL_TOKEN,
        "--some-future-flag",
        "value",
        "on",
    ]
    rewritten, warned = _rewrite_session_sentinel(argv)
    assert rewritten == [
        "--cwd",
        "/proj",
        "--session-id",
        _SENTINEL_FALLBACK,
        "--some-future-flag",
        "value",
        "on",
    ]
    assert warned is True
