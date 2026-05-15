# Decisions

Decisions that shape the shipped V1 codebase — what was
decided, when, and why.

---

**Narrate-only CC plugin** (2026-04-21) — V1 ships as a Claude Code
plugin: a Stop hook recaps the turn and reads the user-facing message
verbatim through macOS `say`. No mic, STT, daemon, or Agent SDK. *Why:*
the narrate-only slice is useful on its own and ships today — a Stop
hook inherits CC's auth and needs no new permissions. A voice-reply loop
(mic + STT) was explored earlier and set aside; narrate-only is the
product, not a stepping stone.

**Interface-driven module design** (2026-04-20) — every swappable
capability (recap, TTS) is a `typing.Protocol`; the pipeline talks to
the protocol, `services.py` picks the concrete. *Why:* keeps the
contract explicit, lets a contributor add a backend in one file, and
lets tests run against in-memory fakes.

**Recap via `claude -p` + Haiku** (2026-04-21) — the default recap
backend is `claude -p --model claude-haiku-4-5`, run as a subprocess
from the Stop hook; a rule-based fallback derives a recap from the
tool-use trace when it times out or errors. *Why:* `claude -p` is an
officially-supported entry point that inherits CC's auth — no API key,
no separate billing — and Haiku is cheap and good enough for a
one-sentence summary. Timeout is `Recap.timeout_s` (default 60 s);
`skip_if_no_tool_use` and `skip_if_message_words_lt` skip the recap on
cheap turns.

**Long messages summarized, never truncated** (2026-04-27) — a message
over 50 post-transform words is summarized via `claude -p`, not
truncated; the summary is not capped; on summarizer failure the plugin
speaks the full verbatim message. *Why:* truncation ends mid-sentence and drops content
silently — wrong for audio. Speak-don't-skip: reading more than expected
is recoverable, silently dropping content is not.

**Default-off, opt-in, per-session narration** (2026-04-27 default /
2026-04-28 scope) — narration is off on fresh install, opt-in via
`/audio-recap:on`, with state keyed on `(cwd, session_id)`; a missing or
unreadable state file reads as off. *Why:* a typical user runs multiple
concurrent CC sessions on one machine — a machine-wide on-state means
surprise audio from whichever session's turn ends. Per-session opt-in
matches the real use case ("narrate *this* stretch of work"). State
files persist indefinitely and survive `claude --continue`; an
`audio_recap.scope = "user"` knob for machine-wide behavior is
deliberately deferred.

**Session-id sentinel** (2026-04-28) — `audio_recap/__main__.py` detects
when CC fails to substitute `${CLAUDE_SESSION_ID}` into the
slash-command body, rewrites the scope to a machine-wide `_global` state
file, and warns on stderr. *Why:* without the fallback a CC-side
substitution regression would leave the user with no working kill
switch and no visible cause.

**Drop `uv` as a runtime requirement; target system `python3`**
(2026-04-28) — the runtime is the `python3` macOS already ships;
`scripts/run.sh` execs `python3 -m audio_recap` with `PYTHONPATH` set, no
install step. `uv` stays in the contributor toolchain only. *Why:* the
plugin has zero runtime deps (every import is stdlib), so `uv` and a
managed venv bought end users nothing. Stdlib-only is now a permanent
invariant, enforced by a CI matrix across Python 3.9–3.13; end-user
prereqs collapse to "macOS + Claude Code."

**English-only in V1** (2026-04-21) — `Config.language` ships as
`"en"`. *Why:* the speakable transforms and coding-vocabulary phrasing
are English-biased; a tight English-only release beats a multilingual
one with shaky per-language quality.

**Identifier conventions** (2026-04-21) — display name "Audio Recap";
marketplace + repo slug `claude-code-audio-recap`; plugin manifest
`name` and on-disk segment `audio-recap`; Python module `audio_recap`;
four explicit slash verbs (`:on`, `:off`, `:status`, `:repeat`). *Why:*
"Audio Recap" names what the plugin does on first sight; the hyphenated
slug follows standard CC convention; the module underscore is a PEP 8
constraint.
