"""Recap capability — produces a one-sentence summary of a Claude turn.

Defines the :class:`Recap` Protocol and the :class:`RecapFailed`
exception. Concrete implementations live in sibling modules
(:mod:`audio_recap.recap.claude_p`, :mod:`audio_recap.recap.rule_based`).
The hook composes one of them at runtime and catches :class:`RecapFailed`
to chain onto the fallback.

Implementations consume :class:`audio_recap.payload.TurnPayload` —
the typed boundary parsed once from CC's hook JSON. Tests build
``TurnPayload`` instances directly rather than constructing CC-shaped
dicts.
"""

from __future__ import annotations

from typing import Protocol

from audio_recap.payload import TurnPayload


class RecapFailed(Exception):
    """Signal from a Recap implementation that it cannot produce a recap.

    Raised by the primary backend on timeout, subprocess error, empty
    output, or any condition that means "no usable recap this turn."
    The orchestrator catches this and falls back to the next configured
    implementation. Implementations do not chain internally.
    """


class Recap(Protocol):
    def generate(self, turn: TurnPayload) -> str:
        """Return a one-sentence recap of the turn.

        Raises :class:`RecapFailed` on any failure. Implementations
        never return an empty or sentinel string — a returned string is
        always speakable recap content.
        """
        ...
