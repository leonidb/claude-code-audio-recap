# Principles

The opinionated product and engineering rules that shape this project.
Each has a reason that fits in a sentence — if it doesn't, we haven't
thought hard enough about it.

## 1. Zero dependencies, fully local, fully private

The headline constraint the rest of the design bends to. Audio Recap is
stdlib-only Python running under the `python3` macOS already ships — no
third-party packages, no `pip`, no `uv`. No accounts, no API keys, no
paid services. The only outbound call is `claude -p`, which rides Claude
Code's own auth: nothing to sign up for, and no data leaves the machine
beyond what CC already sends. Paid or cloud upgrades (ElevenLabs voices,
larger recap models) are opt-in — never the default, never required to
evaluate the project.

## 2. Install in a few terminal commands

Fresh repo to working setup is three slash commands: `/plugin marketplace
add`, `/plugin install`, `/reload-plugins`. No package manager, no
virtualenv, no build step. A broken install is a bug of the same severity
as a functional one.

## 3. Narrow scope, sharp MVP

One user story done well: narrate Claude's turn. No "while we're at it"
features. Adjacent temptations (voice input, multi-language, screen-state
detection) are left out, not scoped in.

## 4. Graceful degradation over feature richness

Clear failure beats silent breakage. `claude -p` offline → rule-based
recap. Summarizer fails → full verbatim message, never truncation. `say`
missing → exit non-zero with a clear message. No swallowed exceptions.

## 5. Modular, interface-driven

Every swappable capability — recap, TTS — sits behind a thin Python
Protocol. The hook talks to protocols, never concrete implementations.
Defaults are the free, first-party ones (`claude -p`, macOS `say`). A new
backend is a new file, not a hook edit.

## 6. Open-source-grade code from the first commit

Typed throughout (`ty` strict-clean). Tests on all public surfaces.
Linting in CI. Every protocol has a real implementation and a test
double. If it's not shippable, it's not merged.
