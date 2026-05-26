# Architecture

A Claude Code plugin that narrates each assistant turn through macOS
`say`, so you can listen instead of stare. Stdlib-only Python, zero
runtime dependencies, no accounts or API keys — it ships as a CC plugin
(a Stop hook), not a daemon. On every Stop hook it generates a
one-sentence recap of the turn via `claude -p --model claude-haiku-4-5`,
extracts the user-facing message verbatim from the transcript, and pipes
both through the `say` binary.

## Execution model

Each Stop hook is a fresh, one-shot process — no long-running state, no
session memory in the plugin. CC starts the hook, it reads a small state
file, does its work (or exits early if narration is disabled), and
exits. On failure (recap timeout, `say` missing, malformed payload) it
logs and exits non-zero without blocking the turn.

The hook runs **synchronously, in the foreground**, until `say`
finishes — by design. CC delivers an Esc keypress as `SIGINT` to the
foreground hook, which terminates the hook and its `say` child together:
press Esc to stop a narration mid-speech. Backgrounding `say` would
break that cancel path.

**Per-session state.** Each CC session's on/off flag lives in its own
JSON file at `~/.claude/audio-recap/state/<session-id>.json`, keyed by
session id alone — so a session that changes working directory mid-run
keeps a single state file. The file holds a single `enabled` boolean.
`/audio-recap:on` writes it; the Stop hook reads it. Default — and any missing/unreadable file — is
`enabled: false` (fail-quiet: a broken state file never produces surprise
audio). State persists across `claude --continue` and indefinitely
thereafter.

**Session-id sentinel.** If CC fails to substitute `${CLAUDE_SESSION_ID}`
into the slash-command body, `audio_recap/__main__.py` detects the
unsubstituted token (or empty arg), rewrites it to a machine-wide
`_global` scope so the kill switch still works, and warns on stderr.

**Timing.** State read and disabled-early-exit are sub-millisecond.
Recap generation (when enabled) typically runs 8–24 s — cold-start
dominated; recap and summarizer share the cold-start and dispatch in
parallel. `say` blocks until playback finishes so audio doesn't overlap
the next prompt.

**Graceful degradation** (see the fallback branches in the data flow
below): `claude -p` failure → rule-based recap from the tool-use trace;
summarizer failure → full verbatim message, never a truncated form
(speak-don't-skip).

## Data flow

```
Stop hook fires (CC supplies JSON on stdin: session_id, cwd, transcript)
  │
  ▼
read ~/.claude/audio-recap/state/<session-id>.json
  │
  ├── missing / unreadable / enabled: false ──► exit 0
  │
  └── enabled: true
        │
        ├── extract message ──► verbatim user-facing text block
        │                          └── if words > threshold:
        │                              claude -p (summary)
        │                              └── (on failure) full verbatim message
        │
        └── claude -p (Haiku) ──► one-sentence recap
              └── (on timeout/error) rule-based recap from tool-use trace
        │
        ▼
        say(recap) ; say(message) ──► speaker
        │
        ▼
        exit 0
```

`/audio-recap:on|off|status` is a separate entry point: it just reads or
writes the per-session state file and prints a one-line confirmation —
it never touches `claude -p` or `say`. `/audio-recap:repeat` replays the
last narration from a per-session cache, or regenerates on a cache miss.

Every hook fire and slash-command invocation appends one structured
`key=value` line to `~/.claude/audio-recap/logs/audio-recap.log` (see
[`audio_recap/eventlog.py`](../audio_recap/eventlog.py)) — the surface
for debugging "why did the hook take this path." Append-only, no
rotation.

## Module layout

```
audio_recap/
├── __main__.py    # subcommand dispatcher for `python -m audio_recap`; session-id sentinel rewrite
├── hook.py        # Stop hook entrypoint
├── command.py     # /audio-recap:on, :off, :status handler
├── repeat.py      # /audio-recap:repeat handler — cached or on-demand replay
├── pipeline.py    # recap → summarize → speakable orchestration + the fallback chains
├── services.py    # composition object: wires production deps; tests inject fakes
├── process.py     # single subprocess call site (ProcessRunner protocol)
├── payload.py     # parses CC's hook JSON into a typed TurnPayload at the boundary
├── summarizer.py  # long-message summarizer via claude -p
├── speakable.py   # text → speakable transforms (code blocks, URLs, paths, units…)
├── cache.py       # per-session (recap, message) cache backing /audio-recap:repeat
├── state.py       # per-session on/off flag
├── config.py      # config schema + loading
├── eventlog.py    # append-only debug log
├── recap/         # Recap protocol — claude_p (default) + rule_based (fallback)
└── tts/           # TTS protocol — macos_say (only shipped backend)
```

`scripts/run.sh` is a thin shim: it sets `PYTHONPATH` and execs
`python3 -m audio_recap <subcommand>`. The Stop hook and
`/audio-recap:repeat` share `pipeline.py` so auto-narration and
on-demand replay run an identical recap + summarize + transforms path.

## Protocols

`tts/` and `recap/` each expose a `typing.Protocol`. The pipeline talks
to the protocol, never a concrete implementation; `services.py` decides
which concrete to instantiate. A new backend is a new file implementing
the Protocol — no pipeline change.

```python
class Recap(Protocol):
    async def generate(self, turn: Turn, *, timeout_s: float) -> str:
        """One-sentence spoken recap. Raises on timeout/error; the
        pipeline catches and falls back to the rule-based impl."""
        ...


class TTS(Protocol):
    async def speak(
        self, text: str, *, voice: str | None = None, rate_wpm: int | None = None
    ) -> None:
        """Speak text; return when playback finishes."""
        ...
```

**Config** (`audio_recap/config.py`) is a frozen-dataclass tree —
`Recap`, `Summarizer`, `Narration`, `TTS` — plus top-level fields. Most
are fixed at their shipped defaults; a small subset (`dry_run`,
`default_enabled`, `log_level`, `tts.trace_speech_log`,
`tts.baseline_wpm`) can be overridden per-cwd via
`<cwd>/.audio-recap/config.json` — see `Config.load()`.

## Out of scope for V1

V1 is narrate-only: no microphone, no STT, no voice-reply loop. macOS
only — `say` is the sole shipped TTS backend.
