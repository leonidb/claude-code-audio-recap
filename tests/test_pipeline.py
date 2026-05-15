"""Pipeline-level tests using fakes — design §9 testability win.

Demonstrates the assertion shape the constructor-injection refactor
unlocked: count primary/fallback invocations directly off the fakes
without needing to patch ``subprocess.run`` or count argv shapes.
"""

from __future__ import annotations

from typing import Any

from audio_recap.payload import PayloadParser
from audio_recap.pipeline import Pipeline
from audio_recap.recap import RecapFailed
from tests.fakes import FakeRecap, build_services


def _turn_with_tool_use() -> Any:
    """Mixed turn: one tool use + a 35-word message (above the 30-word skip floor)."""

    return PayloadParser.from_dict(
        {
            "session_id": "t",
            "cwd": "/proj",
            "transcript": [
                {"role": "user", "content": "do it"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Edit",
                            "input": {"file_path": "/x"},
                        },
                        {"type": "text", "text": " ".join(["w"] * 35)},
                    ],
                },
            ],
        }
    )


def test_recap_primary_called_exactly_once_even_on_fallback() -> None:
    """Regression guard: a primary that raises must not be retried before falling back.

    Today's exception-based shape does not retry the primary; the
    Pipeline catches once and dispatches to ``recap_fallback``. This
    test locks that contract — any future shape that "tries the
    primary twice on transient failure" would silently double the
    claude -p cost without the test catching it. A counting fake makes
    the assertion direct.
    """

    primary = FakeRecap(returns=RecapFailed("simulated timeout"))
    fallback = FakeRecap(returns="Read a file.")
    services = build_services(recap_primary=primary, recap_fallback=fallback)

    Pipeline(services).run(_turn_with_tool_use(), event="stop")

    assert primary.call_count == 1
    assert fallback.call_count == 1


def test_pipeline_prefers_primary_when_it_succeeds() -> None:
    """Primary success short-circuits the fallback."""

    primary = FakeRecap(returns="Edited a file.")
    fallback = FakeRecap(returns="should not fire")
    services = build_services(recap_primary=primary, recap_fallback=fallback)

    result = Pipeline(services).run(_turn_with_tool_use(), event="stop")

    assert primary.call_count == 1
    assert fallback.call_count == 0
    assert result.recap_text == "Edited a file."
    assert result.recap_path == "claude_p"


def test_pipeline_returns_none_recap_when_both_backends_fail() -> None:
    """Both backends raising → ``recap_path="none"``, ``recap_text=None``."""

    primary = FakeRecap(returns=RecapFailed("primary down"))
    fallback = FakeRecap(returns=RecapFailed("fallback also down"))
    services = build_services(recap_primary=primary, recap_fallback=fallback)

    result = Pipeline(services).run(_turn_with_tool_use(), event="stop")

    assert primary.call_count == 1
    assert fallback.call_count == 1
    assert result.recap_text is None
    assert result.recap_path == "none"
