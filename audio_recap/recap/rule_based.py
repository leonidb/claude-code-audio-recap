"""Recap backend: rule-based fallback derived from the tool-use trace.

No subprocess, no network. Classifies tool uses into read-like,
edit-like, and run-like buckets plus a catch-all; templates a single
sentence. Output is deterministic — the same payload always produces
the same recap, which makes snapshot testing easy and gives users
something consistent when ``claude_p`` is unavailable.

The fallback deliberately does not try to read tool-result content
(pass/fail, stdout, exit codes). Parsing that reliably across Claude
Code's tool-call outputs is the primary backend's job. The fallback's
job is to say something coherent when the primary can't.

Tool names from the catch-all bucket are *never* echoed to TTS:
internal naming conventions (``mcp__service__verb``,
``some_internal_tool``) render as "M C P underscore service
underscore verb" through ``say`` and degrade narration quality. With
no regex / no prefix-matching available — the plugin ships for any
user with any tool set — the only safe move is to aggregate
unrecognized tools as a count rather than name them.
"""

from __future__ import annotations

import json

from audio_recap.eventlog import EventLog
from audio_recap.payload import ToolUse, TurnPayload

# Tool-name classification. Covers the common Claude Code tools; anything
# unrecognized falls into the "other" bucket and is named by its tool
# name in the sentence.
_READ_LIKE = frozenset({"Read", "Glob", "Grep", "WebFetch", "WebSearch"})
_EDIT_LIKE = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_RUN_LIKE = frozenset({"Bash"})


def _short_command(raw: object) -> str:
    """First whitespace-delimited token of a shell command, for speaking."""

    if not isinstance(raw, str):
        return "a command"
    stripped = raw.strip()
    if not stripped:
        return "a command"
    first = stripped.split()[0]
    return f"`{first}`"


class RuleBasedRecap:
    def __init__(self, session_id: str, eventlog: EventLog) -> None:
        # ``session_id`` is carried only for TRACE entries; deterministic
        # output does not depend on it.
        self._session_id = session_id
        self._eventlog = eventlog

    def generate(self, turn: TurnPayload) -> str:
        tool_uses = turn.tool_uses
        self._eventlog.event_trace(
            "trace_recap_prompt",
            session_id=self._session_id,
            backend="rule_based",
            tool_uses=json.dumps(
                [{"name": t.name, "id": t.tool_id, "input": t.input} for t in tool_uses]
            ),
        )
        recap = self._classify(tool_uses)
        self._eventlog.event_trace(
            "trace_recap_response",
            session_id=self._session_id,
            backend="rule_based",
            recap=recap,
        )
        return recap

    def _classify(self, tool_uses: list[ToolUse]) -> str:
        if not tool_uses:
            return "Asked a question."

        reads = [t for t in tool_uses if t.name in _READ_LIKE]
        edits = [t for t in tool_uses if t.name in _EDIT_LIKE]
        runs = [t for t in tool_uses if t.name in _RUN_LIKE]
        others = [t for t in tool_uses if t.name not in _READ_LIKE | _EDIT_LIKE | _RUN_LIKE]

        parts: list[str] = []

        if len(reads) == 1:
            parts.append("read a file")
        elif len(reads) > 1:
            parts.append(f"read {len(reads)} files")

        if len(edits) == 1:
            parts.append("edited a file")
        elif len(edits) > 1:
            parts.append(f"edited {len(edits)} files")

        if len(runs) == 1:
            command_input = runs[0].input
            if isinstance(command_input, dict):
                parts.append(f"ran {_short_command(command_input.get('command'))}")
            else:
                parts.append("ran a command")
        elif len(runs) > 1:
            parts.append(f"ran {len(runs)} commands")

        if others:
            # Aggregate as a count rather than echoing tool names — see
            # module docstring. The well-known CC tools handled by
            # reads/edits/runs above already carry informative phrasing;
            # this branch covers everything else without leaking
            # internal identifiers into spoken output.
            parts.append(f"used {len(others)} other {'tool' if len(others) == 1 else 'tools'}")

        if not parts:
            # Classification produced nothing — shouldn't happen given the
            # buckets cover everything, but be defensive.
            return f"Used {len(tool_uses)} tools."

        sentence = ", ".join(parts)
        return sentence[0].upper() + sentence[1:] + "."
