"""Boundary types — parse CC's hook JSON once at the entry edge.

Downstream code consumes :class:`TurnPayload` instances; the raw
``dict[str, Any]`` payload from Claude Code stops at the parser. This
gives ty something concrete to typecheck and lets tests build payloads
directly without mocking CC's JSON shape.

CC's Stop hook sends one of two payload shapes:

- **Inline transcript** — older / fixture-driven shape. ``transcript``
  is an array of ``{"role": ..., "content": ...}`` messages.
- **transcript_path** — the production shape. ``transcript_path`` points
  to ``~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`` which the
  parser walks back to find the most recent user prompt + every
  assistant block since.

Both shapes flow through :func:`load_turn_content_from_jsonl` (when the
inline transcript is missing) into a synthesized inline transcript that
the rest of the parsing logic treats uniformly.

Slash-command detection happens here too: if the most recent user
content begins with one of Audio Recap's own command tags
(``<command-name>/audio-recap:on</command-name>`` etc.), we tag the
:class:`TurnPayload` with ``own_command="confirm"`` or ``"repeat"`` so
``hook.main()`` can branch without re-parsing the user content.
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, cast

from audio_recap.eventlog import EventLog

# Slash-command tags CC injects into the user message when the
# Audio Recap commands fire.
_OWN_COMMAND_TAGS = {
    "<command-name>/audio-recap:on</command-name>": "confirm",
    "<command-name>/audio-recap:off</command-name>": "confirm",
    "<command-name>/audio-recap:status</command-name>": "confirm",
    "<command-name>/audio-recap:repeat</command-name>": "repeat",
}


@dataclass(frozen=True)
class ToolUse:
    """One tool invocation from a Claude turn.

    Frozen because it's a value type — input dict is captured by reference,
    but the wrapper itself doesn't mutate.
    """

    name: str
    tool_id: str
    input: dict[str, Any]


@dataclass
class TurnPayload:
    """Typed, parsed view of one Claude Code Stop event.

    Mutable on purpose: ``hook.py`` rewrites ``message`` in place after
    the speakable transforms run, and the summarizer may overwrite it
    again. The cached_property fields below are derived from
    ``assistant_blocks`` and would invalidate cleanly if blocks were
    reassigned, but we don't do that — they're populated once at parse
    time.
    """

    session_id: str
    cwd: str
    user_content: str
    assistant_blocks: list[dict[str, Any]]
    own_command: str | None = None
    transcript_path: str | None = None
    last_assistant_message_field: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @cached_property
    def tool_uses(self) -> list[ToolUse]:
        """Tool-use blocks in the most recent assistant message."""

        return [
            ToolUse(
                name=str(b.get("name", "")),
                tool_id=str(b.get("id", "")),
                input=cast("dict[str, Any]", b.get("input") or {}),
            )
            for b in self.assistant_blocks
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]

    @cached_property
    def tool_use_count(self) -> int:
        return len(self.tool_uses)

    @cached_property
    def text_blocks(self) -> list[str]:
        return [
            b["text"]
            for b in self.assistant_blocks
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ]

    @cached_property
    def message(self) -> str:
        """Aggregated user-facing text from this turn.

        A single assistant turn can carry multiple ``text`` blocks
        separated by ``tool_use`` blocks (agent-style "announce → tool
        calls → confirmation"). Joins every ``text`` block in original
        order with paragraph breaks, so the whole spoken half of the
        turn reaches the listener.

        Falls back to the ``last_assistant_message`` convenience field
        when ``assistant_blocks`` carries no text — CC populates that
        field when no inline transcript or transcript_path is provided.
        """

        if self.text_blocks:
            return "\n\n".join(self.text_blocks)
        return self.last_assistant_message_field or ""


def _detect_own_slash_command(content: str) -> str | None:
    """Return ``"confirm"`` / ``"repeat"`` if ``content`` starts with our tag.

    CC marks slash-command invocations by prefixing the user message
    with ``<command-name>/path</command-name>``. Tolerant of leading
    whitespace; tags are matched case-sensitively because CC normalizes
    slash-command names.
    """

    if not content:
        return None
    head = content.lstrip()
    for tag, kind in _OWN_COMMAND_TAGS.items():
        if head.startswith(tag):
            return kind
    return None


def load_turn_content_from_jsonl(
    transcript_path: str,
) -> tuple[str, list[dict[str, Any]]] | None:
    """Aggregate the last user prompt + assistant blocks from a JSONL transcript.

    Real CC Stop payloads carry ``transcript_path`` (a JSONL file) instead
    of an inline ``transcript`` array. Walk back to the most recent
    user-typed prompt (string content, not ``tool_result`` blocks) and
    collect every assistant content block since then.

    Returns ``(user_content, assistant_blocks)``: the user content is the
    raw string the JSONL carries (slash-command detection parses the
    leading ``<command-name>...</command-name>`` tag from it). Returns
    ``None`` on read / parse errors or when no string-content user prompt
    exists, so callers can fall back to message-only narration.
    """

    try:
        with open(transcript_path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
    except (OSError, json.JSONDecodeError):
        return None

    last_user_idx: int | None = None
    for i in range(len(rows) - 1, -1, -1):
        row = rows[i]
        if not isinstance(row, dict) or row.get("type") != "user":
            continue
        msg = row.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            last_user_idx = i
            break

    if last_user_idx is None:
        return None

    user_row = rows[last_user_idx]
    user_msg = user_row.get("message") if isinstance(user_row, dict) else None
    user_content = (
        user_msg.get("content", "")
        if isinstance(user_msg, dict) and isinstance(user_msg.get("content"), str)
        else ""
    )

    aggregated: list[dict[str, Any]] = []
    for row in rows[last_user_idx + 1 :]:
        if not isinstance(row, dict) or row.get("type") != "assistant":
            continue
        msg = row.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            aggregated.extend(c for c in content if isinstance(c, dict))
    return user_content, aggregated


def _last_user_content_from_inline(
    transcript: list[dict[str, Any]],
) -> str:
    """Pull the most recent ``role=user`` string content from an inline transcript."""

    for raw in reversed(transcript):
        if not isinstance(raw, dict):
            continue
        if raw.get("role") != "user":
            continue
        content = raw.get("content")
        if isinstance(content, str):
            return content
        return ""
    return ""


def _last_assistant_blocks_from_inline(
    transcript: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pull the most recent assistant message's content blocks."""

    for raw in reversed(transcript):
        if not isinstance(raw, dict):
            continue
        if raw.get("role") != "assistant":
            continue
        content = raw.get("content")
        if isinstance(content, str):
            # Older transcript shape — wrap as a single text block.
            return [{"type": "text", "text": content}]
        if isinstance(content, list):
            return [c for c in content if isinstance(c, dict)]
        return []
    return []


class PayloadParser:
    """Parse CC's hook JSON payload into a :class:`TurnPayload`.

    Owns JSON decode, JSONL transcript loading, session_id/cwd
    extraction, and slash-command tag detection. All log lines for
    parse failures (``invalid_json``, ``non_object_payload``) are
    emitted here so ``hook.main()`` doesn't have to repeat them.

    Parsing runs before the ``Services`` graph (and its ``EventLog``)
    exists, so the parser takes its own ``EventLog``; the entrypoint
    constructs one and threads the same instance into ``Services``.

    Only :meth:`parse` and :meth:`log_payload_trace` log — :meth:`from_dict`
    is a pure transform and is a ``staticmethod`` so log-free callers
    (``repeat.py``'s transcript reader) can use it without an
    ``EventLog``.
    """

    def __init__(self, eventlog: EventLog) -> None:
        self._eventlog = eventlog

    def parse(self, raw: bytes) -> TurnPayload | None:
        """Return a :class:`TurnPayload` or ``None`` if the bytes are unparseable.

        ``None`` returns log their own diagnostic event line; the caller
        just exits 1.
        """

        log = self._eventlog
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            sys.stderr.write(f"[audio-recap] invalid hook JSON on stdin: {e.msg}\n")
            log.message(f"[audio-recap] invalid hook JSON on stdin: {e.msg}")
            log.event("stop", outcome="invalid_json", error=e.msg)
            log.event_trace(
                "trace_payload_invalid",
                payload_raw=raw.decode("utf-8", errors="replace"),
            )
            return None
        if not isinstance(payload, dict):
            sys.stderr.write("[audio-recap] hook payload is not a JSON object; skipping\n")
            log.message("[audio-recap] hook payload is not a JSON object; skipping")
            log.event("stop", outcome="non_object_payload")
            log.event_trace(
                "trace_payload_invalid",
                payload_raw=raw.decode("utf-8", errors="replace"),
            )
            return None
        return PayloadParser.from_dict(cast("dict[str, Any]", payload))

    @staticmethod
    def log_payload_trace(eventlog: EventLog, turn: TurnPayload) -> None:
        """Emit the full payload as a single TRACE line for fixture replay.

        Takes the event log explicitly: it runs after the Services graph
        is built, so the caller passes the config-derived log (which
        respects ``log_level``), not the parser's pre-config bootstrap.
        """

        try:
            payload_json = json.dumps(turn.raw)
        except (TypeError, ValueError):
            payload_json = "<<unserializable>>"
            eventlog.event_trace(
                "trace_payload_serialize_failed",
                session_id=turn.session_id,
                traceback=traceback.format_exc(),
            )
        eventlog.event_trace(
            "trace_payload",
            session_id=turn.session_id,
            payload_json=payload_json,
        )

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> TurnPayload:
        """Build a :class:`TurnPayload` from an already-decoded dict.

        Handles both inline-transcript and ``transcript_path`` shapes;
        synthesizes the inline transcript from JSONL when only the path
        is provided. Pure transform — no logging — so it's a
        ``staticmethod`` callable without an ``EventLog``.
        """

        sid_raw = payload.get("session_id")
        session_id = sid_raw if isinstance(sid_raw, str) and sid_raw else "_global"
        cwd_raw = payload.get("cwd")
        cwd = cwd_raw if isinstance(cwd_raw, str) else ""

        transcript_path_raw = payload.get("transcript_path")
        transcript_path = transcript_path_raw if isinstance(transcript_path_raw, str) else None
        last_assistant_field = payload.get("last_assistant_message")
        last_assistant_message_field = (
            last_assistant_field if isinstance(last_assistant_field, str) else None
        )

        inline_transcript = payload.get("transcript")
        if isinstance(inline_transcript, list):
            user_content = _last_user_content_from_inline(
                cast("list[dict[str, Any]]", inline_transcript)
            )
            assistant_blocks = _last_assistant_blocks_from_inline(
                cast("list[dict[str, Any]]", inline_transcript)
            )
        elif transcript_path is not None:
            loaded = load_turn_content_from_jsonl(transcript_path)
            if loaded is not None:
                user_content, assistant_blocks = loaded
            else:
                user_content = ""
                assistant_blocks = []
        else:
            user_content = ""
            assistant_blocks = []

        own_command = _detect_own_slash_command(user_content)

        return TurnPayload(
            session_id=session_id,
            cwd=cwd,
            user_content=user_content,
            assistant_blocks=assistant_blocks,
            own_command=own_command,
            transcript_path=transcript_path,
            last_assistant_message_field=last_assistant_message_field,
            raw=payload,
        )


__all__ = [
    "PayloadParser",
    "ToolUse",
    "TurnPayload",
    "load_turn_content_from_jsonl",
]
