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

## Concurrent sessions

Each Stop fire is an independent, short-lived process, so several sessions
finishing at once would otherwise narrate on top of each other. Two
mechanisms coordinate them, both file-based — no daemon, no resident state:

- **Serialized playback.** An advisory `fcntl.flock` on
  `~/.claude/audio-recap/playback.lock` is held around `afplay`, so only one
  narration is audible at a time; the rest queue and play in turn. The lock is
  per-user (the path is under `$HOME`) and releases automatically when the
  holding process dies, so a killed session can't wedge the queue. Synthesis
  runs *before* the lock is taken — only playback is serialized. Everything
  about it fails open: an unwritable lockfile, a `flock` the filesystem doesn't
  support, or a wait past the 120s cap all play anyway. An occasional overlap
  beats a silent session.
- **Spoken session labels.** A session with narration switched on writes a
  heartbeat file at `~/.claude/audio-recap/active/<session-id>`; switching it
  off or closing the session retires it. When a narration starts and at least
  one *other* session holds a heartbeat, a short session name is prefixed to the
  first segment it is going to speak — the recap when there is one, otherwise
  the message, so a turn that skips the recap is still attributed. The only
  session narrating stays clean. The name is resolved by a ladder: the user's
  `/rename` (Claude Code's own, read from the transcript) → CC's auto-generated
  title → the last two segments of the cwd (`nbrown-clean sensei` — one segment
  collides when every project has a `sensei/` dir) → a short session-id. It is
  capped to `label_max_words` (default 5). A registry failure degrades to
  *unlabeled*, never to a wrong label.

  The label is a prefix, not a segment of its own: a two-word segment renders
  a ~1s clip that trips the short-audio guard in `tts/macos_say.py`, and it
  would cost a second render-and-play round trip inside the lock.

**A heartbeat means "an enabled session that is open" — not "one that spoke
recently".** `/audio-recap:on` writes it; `/audio-recap:off` and SessionEnd
retire it; the Stop hook refreshes it while narrating, past the `off` gate. So a
session with narration switched off cannot count as somebody's neighbour at all
— which covers the headless agents that prompted this, since they run with
recap off. (One left deliberately on would narrate for
real, and counting it is right.)

Nothing about elapsed time decides whether a session counts. Two sessions open
side by side name each other however long ago either last spoke, which is the
point: the moment you most need to know who is talking is when one of them has
been quiet for a while. `presence_window_s` (default 24h) is therefore an
*orphan horizon*, not an idleness timeout — the two events above retire a
heartbeat in every ordinary case, so one that outlives them belongs to a
hard-killed session, and it is discounted and collected on the next scan.

Two consequences are deliberate: a session enabled by per-cwd `default_enabled`
rather than by `/audio-recap:on` isn't counted until its first narration, since
no code of ours runs before then; and a hard-killed session keeps counting until
the horizon passes, costing at most one unneeded label — the harmless direction.
`/audio-recap:repeat` refreshes the heartbeat via the Stop hook that follows it,
but takes no playback lock, so an explicit "play it now" is the one thing that
can overlap.

That leaves the plugin subscribed to exactly two CC events — `Stop` and
`SessionEnd` — pinned by `tests/test_package.py`. SessionEnd is
[`audio_recap/session_end.py`](../audio_recap/session_end.py), routed through
`scripts/run.sh` like every other entry point. It was shell while a heartbeat
was also refreshed before every turn, where a ~0.4s interpreter spawn was a real
per-turn tax; once that hook was dropped, SessionEnd fired once per session at
teardown and the spawn stopped being worth a second implementation of the
storage layout and of session-id resolution.

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
├── session_end.py # SessionEnd hook entrypoint — retires the presence heartbeat
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
├── lock.py        # playback lock — serializes audio across concurrent sessions
├── presence.py    # heartbeat registry — which other sessions are open and enabled
├── label.py       # naming ladder — what to call this session out loud
├── paths.py       # the on-disk layout, in one dependency-light place
├── atomicwrite.py # temp-file + fsync + rename, for the per-session stores
├── transcript.py  # reads CC's JSONL transcript for on-demand replay
├── recap/         # Recap protocol — claude_p (default) + rule_based (fallback)
└── tts/           # TTS protocol — macos_say (only shipped backend)
```

`scripts/run.sh` is a thin shim: it sets `PYTHONPATH` and execs
`python3 -m audio_recap <subcommand>` (`hook`, `command`, `repeat`,
`session-end`) — it is the only executable the manifest and the slash commands
name. The Stop hook and
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
`default_enabled`, `log_level`, `presence_window_s`, `label_max_words`,
`tts.trace_speech_log`, `tts.baseline_wpm`) can be overridden per-cwd via
`<cwd>/.audio-recap/config.json` — see `Config.load()`.

## Out of scope for V1

V1 is narrate-only: no microphone, no STT, no voice-reply loop. macOS
only — `say` is the sole shipped TTS backend.
