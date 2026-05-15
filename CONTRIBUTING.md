# Contributing

Thanks for looking. This is **Audio Recap** — a small Claude Code plugin with a clear shape. The architecture is documented in [`docs/architecture.md`](docs/architecture.md) and the load-bearing decisions in [`docs/decisions.md`](docs/decisions.md). Read both before filing a non-trivial PR.

A quick naming note: "Audio Recap" is the user-facing plugin name; the repo slug is `claude-code-audio-recap` and the Python package is `audio_recap`. Commands below use the Python package identifier.

## Setup

This is a Claude Code plugin. Working on it needs a recent Claude Code (which you already have if you're reading this), Python 3.9+, the macOS `say` binary (built in on macOS; `which say` confirms), and `uv` for the dev tooling (ruff / ty / pytest).

End users don't need `uv` — Audio Recap runs under `python3` directly with zero runtime dependencies. `uv` is purely for the contributor inner loop.

```sh
git clone https://github.com/leonidb/claude-code-audio-recap.git
cd claude-code-audio-recap
uv sync            # installs ruff, ty, pytest into a managed .venv
```

No microphone permission, no Accessibility, no Input Monitoring, no helper binary. The only external processes the plugin spawns are `claude -p` for recap generation and `say` for playback.

## Running locally

The plugin has two entry points: the Stop hook and the `/audio-recap:*` slash commands. For local iteration:

```sh
# 1. Install the plugin into your Claude Code and use CC normally.
#    Instructions depend on your CC plugin-install flow; see README.

# 2. Drive the Stop hook directly with a fixture payload — fastest inner loop.
#    Narration is off by default; enable it first with option 3 below, or
#    drop a .audio-recap/config.json with {"default_enabled": true} in the cwd.
cat tests/fixtures/stop_payload.json | python -m audio_recap.hook

# 3. Exercise the /audio-recap:* command handlers directly.
python -m audio_recap.command on
python -m audio_recap.command off
python -m audio_recap.command status
python -m audio_recap.repeat
```

Options 2 and 3 are what you'll want during development: option 2 exercises the full recap + TTS path without waiting on a real Claude turn, and option 3 flips the persistent state file without going through the CC TUI. The two paths share `state.py` — flipping state via option 3 and then running option 2 is the fastest way to test the early-exit branch.

Option 2 (and `repeat` on a cold cache) makes real `claude -p` calls — cheap on Haiku, but it does spend tokens against your Claude Code auth. The `on`/`off`/`status` commands don't touch a model.

The structured event log at `~/.claude/audio-recap/logs/audio-recap.log` defaults to **INFO** — paths, per-stage timings, word counts. **TRACE** adds the recap and message text, the full hook payload, the verbatim `claude -p` recap and summarizer prompts and responses, the speakable-transformed text per segment, and full Python tracebacks at every catch site. Turn it on while debugging with `{"log_level": "trace"}` in a `<cwd>/.audio-recap/config.json`. The log lives only on your local machine and is not rotated — delete it any time; the plugin recreates it on the next fire.

## Tests

```sh
pytest                          # all tests
pytest tests/test_recap_claude_p.py   # one file
pytest tests/test_tts_macos_say.py    # another
```

Tests cover the Protocol contracts with in-memory fakes and injected `Services` graphs — no test shells out to `claude -p` / `say` or hits the network, so `pytest` runs clean and fast everywhere.

For end-to-end narration-quality assessment, see `tests/eval/README.md` — a `claude`-CLI-driven tool that dispatches a fixed prompt set, fires the hook in-process, harvests recap/summary/event data into a per-turn `results.md`, and exits non-zero if any turn broke. Auth is your logged-in `claude` CLI session (no API key); burns tokens, so run it deliberately with `python -m tests.eval`.

## Style and checks

Before you open a PR:

```sh
ruff check .
ruff format --check .
ty check .             # strict mode; see pyproject.toml
pytest
```

CI runs all four. PRs that fail any of them won't merge. We use [`ty`](https://github.com/astral-sh/ty) — Astral's typechecker — to keep the toolchain consistent with `ruff` and `uv`.

- **Typing:** strict. Every public function has annotations; every Protocol has concrete types, no `Any` leaking into interfaces.
- **Formatting:** `ruff format` (Black-compatible). Don't hand-format.
- **Linting:** `ruff check` with the rules in `pyproject.toml`. Fix warnings or explain them in the PR.
- **Tests:** every public surface has at least one test. Hook behavior gets tested against in-memory fakes for `Recap` and `TTS`, not real providers.

## Adding a new Recap or TTS backend

Phase 1 has two swappable slots: `Recap` (generates the one-sentence turn summary) and `TTS` (speaks text). The hook composes them; concrete backends are drop-in.

1. Read the relevant Protocol in [`docs/architecture.md`](docs/architecture.md).
2. Add `audio_recap/<capability>/<backend_name>.py` (where `<capability>` is `recap` or `tts`). Implement the Protocol. Do not import from `audio_recap/hook.py` — the hook depends on you, not the other way around.
3. Register the backend in `audio_recap/config.py` under its config key (e.g. `"elevenlabs"`, `"piper"`, `"local_llm"`).
4. Add a module-level test file with in-memory or cassette-based tests. If the backend requires network or external processes, mark those tests `slow`.

**Do not** add provider-specific branches to `hook.py`. If you find yourself wanting to, the Protocol probably needs to widen — open an issue first and we'll talk it through.

Phase-2 capabilities (STT, VAD, voice-reply orchestration) are not in this repo today. The phase-1 design is the entire repo. If you want to discuss phase-2 directions, open an issue rather than a PR.

## Commit and PR conventions

- One logical change per commit. Reviewable commits are more important than reviewable PRs.
- PRs: one paragraph of context, one of test plan. No checkboxed templates; we trust you.
- Rebase-merge to main. Keep history linear.

### Conventional Commits

We follow [Conventional Commits](https://www.conventionalcommits.org/) strictly — CI checks every commit on a pull request and fails the build if one doesn't parse.

**Format:**

```
<type>[optional scope]: <description>

[optional body]

[optional footer]
```

**Allowed types:** `feat`, `fix`, `docs`, `chore`, `refactor`, `test`, `perf`, `build`, `ci`, `revert`.

**Subject line:**

- Imperative mood (`add X`, not `added X` or `adds X`).
- ≤ 72 characters.
- No trailing period.

**Body (optional):** Explains *why* when the *what* isn't obvious from the diff. Separated from the subject by a blank line.

**Breaking changes:** append `!` after the type/scope (`feat(tts)!: drop legacy sync API`), or add a `BREAKING CHANGE:` footer that describes the break. Either form triggers a major-version bump.

**Examples:**

```
feat(tts): add piper backend
fix(recap): fall back cleanly when claude -p exits non-zero
docs: clarify the Stop-hook fixture path in CONTRIBUTING
chore: bump claude CLI minimum to 2.1.120
refactor(hook)!: extract speakable transforms into their own module

BREAKING CHANGE: TTS.speak() now takes voice as a keyword-only arg.
```

### Branch naming

Feature branches match commit types: `<type>/<slug>`.

- `feat/piper-tts`
- `fix/recap-timeout`
- `docs/readme-tweaks`
- `refactor/speakable-transforms`

The slug is short-kebab-case. One branch, one logical change, merged by rebase.

### Releasing changes

Every commit (or PR) that changes user-visible behavior or fixes a runtime bug **must** bump the plugin version. Claude Code keys its plugin cache on the version string — without a bump, `claude plugin update` is a no-op for already-installed users and they keep running the previous build no matter how many times they refresh. Two files carry the version and both must move together:

- `pyproject.toml` — `[project] version`
- `.claude-plugin/plugin.json` — top-level `version`

`.claude-plugin/marketplace.json` does **not** carry a per-plugin `version` — Claude Code resolves the plugin version from `plugin.json` first, and the docs warn that setting it in both places lets a stale marketplace value silently mask `plugin.json`. The marketplace's own `metadata.version` versions the catalog file itself and is independent of the plugin.

Use the smallest semver step that fits the change: `0.0.x` for fixes and additive tweaks while we're pre-1.0, `0.x.0` once we start labeling releases, `x.0.0` for breaking changes (which also need the `!` or `BREAKING CHANGE:` footer per Conventional Commits). Version-only commits go in as `chore(release): bump plugin version to <new>`.

## Reporting bugs

Include: macOS version, Claude Code version (`claude --version`), Python version, config file (with secrets redacted), and a log excerpt from the failing run. Minimal repro if you can get one — a Stop-hook JSON payload that reliably reproduces is gold.

For TTS issues specifically, include the output of `say -v '?' | head` (to confirm the voice list) and whether the failure is at recap generation, message extraction, or `say` playback.

## License

This project is licensed under Apache-2.0. By submitting a contribution you agree that your work is licensed under the same terms; see [`LICENSE`](LICENSE) for the full text. You do not need to add per-file copyright headers — the repo-level `LICENSE` file covers the tree.
