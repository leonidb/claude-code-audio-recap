# Test fixtures

Development fixtures for unit and integration tests. Two shapes are
covered:

- **Inline-transcript shape** (`stop_payload.json`,
  `stop_payload_qa.json`, `stop_payload_failed_command.json`,
  `stop_payload_read_only.json`) — the synthesized shape used by the
  hook's recap pipeline. The transcript array carries the user prompt
  and the assistant's tool-use + text content directly.

- **Real CC shape** (`stop_payload_real.json`) — the shape Claude
  Code actually emits to Stop hooks: top-level `last_assistant_message`
  (the user-facing reply text) plus `transcript_path` (a JSONL file on
  disk that the hook reads to synthesize tool-use context for the
  recap). The fixture committed here has paths sanitized to
  `/tmp/example-project/...` and is derived from a real captured CC
  Stop payload. The committed `transcript_path` does not point at a
  real file, which exercises the JSONL-load fallback (recap skipped,
  message still narrates).

The hook accepts both shapes — the real-shape JSONL synthesis path is
unit-tested with an in-memory JSONL written to `tmp_path`.
