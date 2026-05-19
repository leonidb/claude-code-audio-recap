# Audio Recap

[![CI](https://github.com/leonidb/claude-code-audio-recap/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/leonidb/claude-code-audio-recap/actions/workflows/ci.yml) [![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE) [![Python: 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

*Hear what Claude Code is doing without watching the screen.*

<video src="docs/demo.mp4" controls width="720"></video>

Audio Recap speaks a one-sentence recap of every turn — long replies summarized for ears — so you can step away and trust the audio to pull you back when there's something worth your attention.

## What you'll hear

After each assistant turn, Audio Recap speaks at most two segments:

1. **Recap** — a one-sentence summary of the turn's actions, e.g. *"Edited `auth.py` and `tests/test_auth.py`. Ran the test suite. All passing."*
2. **Message** — Claude's reply to you. Code blocks and tables are announced rather than read out loud ("Code block: 12 lines, Python."). Long messages are summarized in plain prose rather than read in full or cut off mid-sentence.

Pure tool-work turns (no user-facing message) speak only the recap. Pure Q&A turns (no tools, no edits) speak only the message. The plugin never narrates silence on a non-empty turn.

## Where Audio Recap fits

Audio plugins for Claude Code split roughly three ways:

- **Heavier multi-platform voice stacks** — bidirectional or multi-engine TTS that installs extra services and runs background daemons, often spanning several agents. Powerful, install-heavy.
- **Lightweight narrate-everything hooks** — pipe every assistant turn directly to TTS, verbatim. Simple, but long messages drag, code blocks read aloud, and you never hear what the turn actually *did*.
- **Audio Recap** sits between them: a one-sentence Haiku-summarized recap of what the turn did, plus the assistant's reply (summarized if long), and nothing else. No mic, no models to download, no daemon, no API key beyond Claude Code's. Just `/plugin install`.

If you run another audio plugin alongside this one, pick one — two will double-narrate every turn and collide on the audio output.

> **Note:** `/voice` is Claude Code's built-in dictation. Audio Recap commands all live under `/audio-recap:` — `/audio-recap:on`, `/audio-recap:off`, `/audio-recap:status`, `/audio-recap:repeat`.

## Quickstart

Requires macOS, for now.

### 1. Install the plugin

From inside any Claude Code session, type:

```
/plugin marketplace add leonidb/claude-code-audio-recap
/plugin install audio-recap@claude-code-audio-recap
/reload-plugins
```

The first command tells Claude Code where to find Audio Recap; the second installs it; the third makes its commands available in the current session (or restart the session — same effect). Type all three at the CC prompt, no need to drop to a separate shell.

> Prefer the shell? `claude plugin marketplace add leonidb/claude-code-audio-recap` and `claude plugin install audio-recap@claude-code-audio-recap` work from any terminal as well. You'll still need `/reload-plugins` (or a session restart) inside CC before the slash commands appear.

### 2. Turn Audio Recap on

Audio Recap is **off by default** in every Claude Code session — running the install above doesn't make any noise yet. To enable it for the session you're in:

```
/audio-recap:on
```

(`audio-recap:` is the plugin's command namespace — Claude Code prefixes every plugin's slash commands this way to keep them collision-free.)

`/audio-recap:on` enables the plugin **for that session only**. Other open Claude Code sessions are unaffected — flip them on or off independently. Ask Claude to do something, and you should hear a one-sentence recap followed by Claude's reply.

To disable: `/audio-recap:off`. To check current state without changing it: `/audio-recap:status`. State applies to the session it was set in and persists for that session indefinitely — `claude --continue` resumes the same session id, so your setting survives the resume.

### Replay the last narration

`/audio-recap:repeat` replays the most recent narration audibly — it works whether Audio Recap is on or off. Only the latest turn is replayable; earlier ones aren't kept, and an empty session gets a short "nothing to repeat" cue.

If nothing's been narrated yet this session, `repeat` narrates the last turn instead, so you still hear it.

## Sound less robotic (1-minute setup)

Default macOS voices (Samantha, Alex, Fred) are functional but obviously synthetic. macOS also ships a set of **Premium** neural voices that sound roughly an order of magnitude better — same engine, much more natural prosody. They're free but have to be downloaded once.

**Install a Premium voice:**

1. Open **System Settings → Accessibility → Spoken Content**.
2. Click the dropdown next to **System voice → Manage Voices…**
3. Find a **Premium** voice (recommended for English: **Ava (Premium)**, **Evan (Premium)**, **Zoe (Premium)**, **Joelle (Premium)**, **Jamie (Premium)**, or **Samantha (Premium)** — pick one whose sample you like).
4. Click the cloud-download button next to it. Each Premium voice is ~150–200 MB.
5. Wait for the download — usually under a minute.

**Use it as the system voice:**

In the same panel, set **System voice** to your downloaded Premium voice. Audio Recap inherits whatever the system default is.

## Stopping audio mid-speech

Press **Esc** during a narration to cancel both the hook and any in-progress audio. The Stop hook is synchronous — it stays in the foreground until `afplay` finishes — so a single Esc terminates the full chain and Claude Code moves on. Useful when you've already read the message on screen and don't need to hear it.

(`/audio-recap:off` is the persistent counterpart — it disables Audio Recap for the rest of the session. Esc is the one-shot "stop *this* one.")

## How it works

A Claude Code plugin. On every Stop event, the plugin:

1. Reads the persisted on/off flag and exits immediately if Audio Recap is off — no `claude -p`, no `say`, no side effects.
2. Generates a one-sentence recap by calling `claude -p --model claude-haiku-4-5` against the turn's tool-use trace. Falls back to a deterministic rule-based recap if `claude -p` times out or errors.
3. Extracts the user-facing message from the turn payload. Long messages (above the configured threshold) are summarized via `claude -p` rather than truncated; falls back to the verbatim full message on summarizer failure.
4. Renders both segments with `say -o` to a temporary AIFF, then plays them via `afplay` (recap first). Decoupling synthesis from playback dodges a stochastic mid-playback truncation seen in the streaming `say` path; if any stage fails the plugin falls back to streaming `say` so audio still plays.

No daemon, no long-running state, no microphone — each Stop hook is a fresh one-shot process. Full architecture in [`docs/architecture.md`](docs/architecture.md).

## What it costs

Audio Recap calls `claude -p --model claude-haiku-4-5` once per narrated turn — for the recap, and again when a reply is long enough to summarize. That's **additional model usage**: it draws on the same Claude Code subscription or API credits your coding session already uses. It runs on Haiku, though — the cheapest model, far below the cost of the model doing your actual coding — so the per-turn overhead is small next to a normal session. No separate API key or account: it uses your existing Claude Code auth.

## Configuration

Optional per-cwd overrides go in `<cwd>/.audio-recap/config.json` — drop a JSON file and Audio Recap picks it up on the next fire. Example:

```json
{
  "default_enabled": true,
  "log_level": "trace",
  "tts": { "baseline_wpm": 150 }
}
```

The recognized keys:

- **`default_enabled`** (default `false`) — when `true`, a fresh session in this cwd starts with narration on, without running `/audio-recap:on`.
- **`log_level`** (default `"info"`) — set to `"trace"` to turn on the verbose event log (full payloads, `claude -p` prompts/responses, tracebacks).
- **`dry_run`** (default `false`) — when `true`, the hook runs recap + summarization + logging but skips the `say` subprocess. For test runners that dispatch many fires without audible playback.
- **`tts.trace_speech_log`** (default `false`) — when `true` (and `log_level` is `"trace"`), captures a per-fire excerpt of the macOS speech-subsystem unified log. For chasing rare synthesizer-side truncation.
- **`tts.baseline_wpm`** (default `142`) — reference speaking rate the per-fire telemetry compares against; also drives the short-aiff fallback gate.

Defaults live in [`audio_recap/config.py`](audio_recap/config.py); the PRD has the rules behind each knob: [`docs/prd.md`](docs/prd.md).

## Privacy

Audio Recap runs entirely on your machine. The eventlog (`~/.claude/audio-recap/logs/audio-recap.log`) is local-only — never uploaded, never shared, never seen by Anthropic or anyone else. The plugin makes no network calls of its own; it shells out to `claude -p` (which uses your Claude Code authentication and your normal API endpoint) and to macOS `say` and `afplay` (which run locally).

**What lives in the local log:** at the default INFO level the log carries only metadata — the path each fire took, per-stage timings, word counts, and the slash-command verb you invoked. It does **not** record the recap text or the message text. Set `log_level` to `"trace"` and the log additionally captures the full recap and message content for each fire, the full `claude -p` prompts and responses, the inbound CC payload, and full Python tracebacks for any caught exception. If your assistant text contains secrets (API keys pasted into a Claude prompt, credentials in a code block), they appear in the log only when TRACE is on. Treat `~/.claude/audio-recap/logs/` like any other local debug log: don't share screenshots that include it, don't post it to bug reports without redacting, and feel free to delete it (the plugin recreates the file on the next fire).

**No telemetry.** No anonymous usage stats, no error reporting, no model fingerprinting — the only network traffic this plugin originates is the `claude -p` subprocess call you've already authorized for Claude Code itself.

## Known issues

- **Multi-session audio collision.** If you have Audio Recap on in two Claude Code sessions at once, both Stop hooks fire when both sessions reply. They share the macOS audio output, so two narrations can step on each other or play in parallel. Workaround: keep Audio Recap on in one session at a time. We may add cross-session arbitration in a future release.
- **CC dictation collision.** Claude Code's built-in `/voice` listens via the system mic. If you trigger `/voice` while Audio Recap is mid-playback, the dictation will pick up the synthesized speech as input. Workaround: wait for the audio to finish, or press Esc to cancel mid-playback.
- **Cold-start latency.** The first Stop fire after a long idle pays a `claude -p` cold-start cost; subsequent fires within the same window are faster because the API session is warm.

## Debugging

Every hook fire and every `/audio-recap:*` invocation appends a structured line to `~/.claude/audio-recap/logs/audio-recap.log` (key=value, ISO-8601 timestamp, no rotation). `tail -f ~/.claude/audio-recap/logs/audio-recap.log` to watch live; `grep session_id=<id>` to scope to one CC session.

The log defaults to **INFO** — paths, per-stage timings, word counts. **TRACE** adds the recap and message text, the full hook payload, the verbatim `claude -p` recap and summarizer prompts and responses, the speakable-transformed text per segment, and full Python tracebacks for any caught exception — turn it on with `{"log_level": "trace"}` in a `<cwd>/.audio-recap/config.json`. Each entry carries a `since_last_fire_ms` field so a hook firing twice in a row is visible at a glance. Every event stays on one line — newlines and tabs inside long values are escaped to `\n` / `\t` literals so `grep` keeps working.

At INFO the log stays small; TRACE adds tens of KB per turn, so leave it off unless you're actively debugging. There is no rotation — delete the file any time; the plugin recreates it on the next fire.

## Documentation

- [`docs/prd.md`](docs/prd.md) — phase-1 product requirements
- [`docs/architecture.md`](docs/architecture.md) — plugin architecture + hook flow
- [`docs/principles.md`](docs/principles.md) — product and engineering principles
- [`docs/decisions.md`](docs/decisions.md) — decision log
- [`docs/glossary.md`](docs/glossary.md) — vocabulary
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to contribute

## Contributing

PRs welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup, conventions, and how to add a new TTS or recap backend.

## License

[Apache-2.0](LICENSE).

---

*Not affiliated with Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic.*
