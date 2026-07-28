# Audio Recap

[![CI](https://github.com/leonidb/claude-code-audio-recap/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/leonidb/claude-code-audio-recap/actions/workflows/ci.yml) [![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE) [![Python: 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](pyproject.toml)

*Hear what Claude Code is doing without watching the screen.*

Audio Recap speaks a Haiku-summarized recap of every turn - long replies condensed into spoken prose, not read verbatim - so you can step away and trust the audio to pull you back when there's something worth your attention. No mic, no models to download, no daemon, no API key beyond what Claude Code already uses. Just `/plugin install`.

> 🔊 **Click unmute to hear the recap - the audio plays after Claude finishes outputting.**

<video src="https://github.com/user-attachments/assets/4c4eb7ab-a8a8-43c1-b269-5b006328101d" controls width="720"></video>

## What you'll hear

After each assistant turn, Audio Recap speaks at most two segments:

1. **Recap** - a one-sentence summary of the turn's actions, e.g. *"Edited `auth.py` and `tests/test_auth.py`. Ran the test suite. All passing."*
2. **Message** - Claude's reply to you. Long messages are summarized for listening, not read in full.

## Quickstart

Requires macOS, for now.

### 1. Install the plugin

From inside any Claude Code session, type:

```
/plugin marketplace add leonidb/claude-code-audio-recap
/plugin install audio-recap@claude-code-audio-recap
/reload-plugins
```

The first command tells Claude Code where to find Audio Recap; the second installs it; the third makes its commands available in the current session (or restart the session - same effect). Type all three at the CC prompt, no need to drop to a separate shell.

> Using the Claude desktop app? Open the **Code** tab, click **+** next to the prompt, and choose **Plugins → Add plugin**. Add the marketplace `leonidb/claude-code-audio-recap`, then install Audio Recap from it. Installs from the app are active right away, no reload needed.

> Note: in the desktop app you also need the Claude Code CLI installed - `curl -fsSL https://claude.ai/install.sh | bash`

### 2. Turn Audio Recap on

Audio Recap is **off by default** in every Claude Code session - running the install above doesn't make any noise yet. To enable it for the session you're in:

```
/audio-recap:on      # enable narration for this session
/audio-recap:off     # disable narration for this session
/audio-recap:status  # check current state
```

> **Highly recommended:** default macOS voices sound robotic. Spend a minute installing a Premium voice via [Sound less robotic](#sound-less-robotic-1-minute-setup---highly-recommended) - it's an order-of-magnitude quality jump.

### 3. Other commands

```
/audio-recap:repeat  # replay the most recent narration
Esc                  # stop a narration mid-playback
```

## Where Audio Recap fits

Audio plugins for Claude Code split roughly three ways:

- **Heavier multi-platform voice stacks** - bidirectional or multi-engine TTS that installs extra services and runs background daemons, often spanning several agents. Powerful, install-heavy.
- **Lightweight narrate-everything hooks** - pipe every assistant turn directly to TTS, verbatim. Simple, but long messages drag, code blocks read aloud, and you never hear what the turn actually *did*.
- **Audio Recap** sits between them: a one-sentence Haiku-summarized recap of what the turn did, plus the assistant's reply (summarized if long), and nothing else. No mic, no models to download, no daemon, no API key beyond Claude Code's. Just `/plugin install`.

If you run another audio plugin alongside this one, pick one - two will double-narrate every turn and collide on the audio output.

> **Note:** `/voice` is Claude Code's built-in dictation. Audio Recap commands all live under `/audio-recap:` - `/audio-recap:on`, `/audio-recap:off`, `/audio-recap:status`, `/audio-recap:repeat`.

## Sound less robotic (1-minute setup) - highly recommended!

Default macOS voices (Samantha, Alex, Fred) are functional but obviously synthetic. macOS also ships a set of **Premium** neural voices that sound roughly an order of magnitude better - same engine, much more natural prosody. They're free but have to be downloaded once.

**Install a Premium voice:**

1. Open **System Settings → Accessibility → Spoken Content**.
2. Click the dropdown next to **System voice → Manage Voices…**
3. Find a **Premium** voice (recommended for English: **Ava (Premium)**, **Evan (Premium)**, **Zoe (Premium)**, **Joelle (Premium)**, **Jamie (Premium)**, or **Samantha (Premium)** - pick one whose sample you like).
4. Click the cloud-download button next to it. Each Premium voice is ~150–200 MB.
5. Wait for the download - usually under a minute.

**Use it as the system voice:**

In the same panel, set **System voice** to your downloaded Premium voice. Audio Recap inherits whatever the system default is.

## How it works

A Claude Code plugin. On every Stop event, the hook:

1. Generates the recap via `claude -p --model claude-haiku-4-5`, with a deterministic rule-based fallback.
2. Summarizes the user-facing message via `claude -p` if it's above the configured threshold; falls back to the verbatim message on failure.
3. Renders both segments with `say -o` to AIFF, plays via `afplay`.

Each Stop hook is a fresh one-shot process. Full architecture in [`docs/architecture.md`](docs/architecture.md).

**Several sessions at once.** Narrations queue instead of talking over each other, and each session says which one it is:

```
builder. Edited three files and ran the tests.
docs. Rewrote the install section.
```

Name your sessions with Claude Code's `/rename` — it's what makes the announcement useful. Otherwise it falls back to the auto-generated title, then the project path. A session running on its own stays unlabeled, and sessions with Audio Recap off never make another one announce itself.

## What it costs

Audio Recap calls `claude -p --model claude-haiku-4-5` once per narrated turn - for the recap, and again when a reply is long enough to summarize. That's **additional model usage**: it draws on the same Claude Code subscription or API credits your coding session already uses. It runs on Haiku, though - the cheapest model, far below the cost of the model doing your actual coding - so the per-turn overhead is small next to a normal session. No separate API key or account: it uses your existing Claude Code auth.

## Privacy

Audio Recap runs entirely on your machine. The messages are never logged, and the log is purely local for debug purposes. No telemetry, no network calls of its own beyond the `claude -p` subprocess Claude Code already uses.

## Contributing

PRs welcome - see [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup, conventions, and how to add a new TTS or recap backend.

## License

[Apache-2.0](LICENSE).

---

*Not affiliated with Anthropic. "Claude" and "Claude Code" are trademarks of Anthropic.*
