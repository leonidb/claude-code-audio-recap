# `tests/eval/` — Audio Recap narration-quality eval

A reproducible loop that exercises the Audio Recap plugin end-to-end on a
fixed prompt set, then captures the per-fire recap / summary / event data
into a bundle a human can read or play back through `say`.

## What it does

For each prompt in [`prompts.py`](./prompts.py):

1. Dispatch it to a fresh **`claude` CLI** invocation
   (`claude -p … --output-format stream-json --setting-sources ""`) —
   per-prompt-isolated, no carry-over context. `--setting-sources ""`
   keeps user plugins from loading, so Audio Recap's own Stop hook does
   not fire inside the eval's subprocesses.
2. Collect every text + tool_use block the agent produces.
3. Aggregate them into one synthetic CC `Stop` payload and fire
   `audio_recap.hook.main` on it **in-process**, with a `Services`
   graph whose eventlog / state / cache roots are all redirected under
   the run bundle. The developer's real `~/.claude` tree is untouched.
4. The hook runs in **dry-run mode** (`<repo>/.audio-recap/config.json`
   carries `{"dry_run": true}` for the duration of the run, then
   restored) so 16 prompts don't equal 16 audible playbacks.

After all prompts finish, the harvest stage joins the eventlog with the
captured assistant text into a structured `data.json` and a
human-readable `results.md`, and `check_run` checks the no-failure
invariants — the runner exits non-zero if any turn broke.

## Why

An earlier MCP-orchestrated loop pushed substantive content through
tool-call arguments, which Audio Recap never sees — it only inspects
`text` blocks. Roughly 40% of the turns in the 2026-05-04T07-13Z
reference run produced narration of the worker's meta-think rather than
the actual answer.

Driving the `claude` CLI directly — the same binary the plugin's own
recap backend shells out to — makes the eval reproducible by any
contributor out of the box, and the assistant's text blocks ARE the
answer: the same shape a typical Claude Code user produces during real
conversations.

## Running it

Prerequisites:
- macOS (TTS targets `say`; dry-run skips the actual call but the
  codebase still imports the macOS backend).
- `uv` for dependency management.
- A logged-in `claude` CLI on `PATH`. Auth is your existing Claude Code
  session — **no `ANTHROPIC_API_KEY` needed**, you never hand Anthropic
  an API key.

Run it:

```sh
uv run python -m tests.eval               # full set (~16 prompts, ~3-5 min)
uv run python -m tests.eval --smoke       # first 3 prompts (~30s)
uv run python -m tests.eval -n 5          # first N prompts
uv run python -m tests.eval --timeout 90  # per-prompt claude CLI timeout (seconds)
```

The runner harvests automatically and prints a one-line verdict:

```
✓ 16/16 turns clean (2 used the rule_based fallback — see results.md)
  bundle: tests/eval/runs/2026-05-14T16-30Z
```

It exits `0` when every turn completed cleanly and `1` when any turn
broke (non-zero hook exit, `claude` CLI error, non-`dry_run` TTS status,
or both recap backends down). A `claude_p` → `rule_based` fallback is a
degraded-but-working path — it's surfaced as a soft signal, not a
failure. Grading narration *quality* is human-driven today (read
`results.md`); automating it is a separate task.

Re-harvest an existing bundle, or listen back to a turn through `say`:

```sh
uv run python -m tests.eval.harvest tests/eval/runs/<UTC-timestamp>
uv run python -m tests.eval.play tests/eval/runs/<UTC-timestamp> 3
uv run python -m tests.eval.play tests/eval/runs/<UTC-timestamp> 3 --recap
uv run python -m tests.eval.play tests/eval/runs/<UTC-timestamp> all
```

## Output bundle

Each run lands under `tests/eval/runs/<UTC-timestamp>/` (gitignored).
Layout:

The run bundle doubles as the hook's `audio_recap_root`, so the
plugin's own storage (`logs/`, `cache/`, `projects/`) nests inside it:

```
runs/2026-05-14T16-30Z/
├── state.json            # prompt list, session_ids, captured assistant text
├── logs/audio-recap.log  # bundle-local eventlog (no global pollution)
├── projects/             # bundle-local on/off state (pre-seeded enabled=true)
├── cache/                # bundle-local narration cache written by the hook
├── data.json             # per-turn structured data (harvested)
├── results.md            # human-readable rendering of data.json
└── config.json.backup    # only if a pre-existing dry-run config was overwritten
```

## Reading `results.md`

For each turn the renderer surfaces:

- **Prompt** — the input.
- **Stats** — text-block / tool-use counts and the hook's exit code.
- **Recap** — what Audio Recap would have spoken first (path, word
  count, text). `claude_p` = primary backend hit; `rule_based`
  = fallback fired; `skipped_*` = the recap-skip rules hit.
- **Message** — what Audio Recap would have spoken second
  (summarizer path, pre/post word counts, `tts_status` confirms
  `dry_run`).
- **Assistant full text** — collapsed by default; expand for the
  raw text the agent produced.

## Unit tests

`test_eval.py` holds fast, no-network unit tests for the harness's pure
helpers — the harvest KV parser, the fire-grouping logic, the runner's
payload synthesis, the stream-json parser, the dry-run config context
manager, the in-process hook fire, and the `check_run` invariant check.
These run in CI via plain `pytest`. The full `claude`-CLI-driven run is
**not** a pytest test — its pass/fail is the runner's exit code, run
deliberately and opt-in.

## Notes

- The runner writes `<repo>/.audio-recap/config.json` for the duration
  of the run and restores any pre-existing file from a backup in the
  bundle. If a run is interrupted (Ctrl-C, OOM), check for an orphaned
  config file and remove or restore it manually.
- Per-prompt timeout defaults to 180s. Prompts requiring multiple Bash
  steps (e.g. `coding-sandbox-scaffold`) can come close to it.
