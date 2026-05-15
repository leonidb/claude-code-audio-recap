"""CLI-driven eval runner for the Audio Recap plugin.

Dispatches each prompt in :mod:`tests.eval.prompts` to a fresh
``claude`` CLI invocation (per-prompt-isolated — no carry-over
context), aggregates every text + tool_use block the agent produces
into one synthetic CC Stop-event payload, and fires
:func:`audio_recap.hook.main` on it **in-process** with a
:class:`audio_recap.services.Services` graph whose file-backed roots
are all redirected under the run bundle.

The ``claude`` CLI is the same binary the plugin's own recap backend
shells out to; it authenticates via the developer's logged-in Claude
Code session — no ``ANTHROPIC_API_KEY``, no extra dependency.
``--setting-sources ""`` keeps user plugins (and their hooks) from
loading, so the Audio Recap Stop hook does not fire inside the eval's
own subprocesses, and ``--no-session-persistence`` keeps throwaway
session files out of ``~/.claude/projects/``.

The hook fires in-process with a :class:`Services` graph whose
file-backed roots all nest under the run bundle, so the developer's
real ``~/.claude`` tree is never touched. ``claude -p`` for the
recap/summary still runs for real — that is the behavior under
evaluation; ``say`` is skipped via the dry-run config.

Per-prompt-isolated rather than multi-turn: the recap and summary
functions only ever look at the LAST assistant message in the
transcript, so multi-turn context only changes the assistant's replies
(via accumulated history), which muddies signal across runs. One prompt
per fresh session = clean per-turn quality measurement.

Output bundle layout under ``tests/eval/runs/<UTC-timestamp>/`` — the
run bundle doubles as the hook's ``audio_recap_root``, so the plugin's
own storage (``logs/``, ``cache/``, ``projects/``) nests inside it:

- ``state.json`` — run id + per-turn metadata (prompt, session_id,
  assistant_text, tool_use_count, …) for the harvest stage.
- ``logs/audio-recap.log`` — bundle-local eventlog from the hook fires.
- ``projects/`` — bundle-local on/off state (pre-seeded ``enabled=true``).
- ``cache/`` — bundle-local narration cache written by the hook.
- ``data.json`` / ``results.md`` — produced by :mod:`tests.eval.harvest`.

This module is the library: :func:`run` drives the prompt set,
:func:`check_run` checks a harvested ``data.json`` for hard failures.
The CLI entry point lives in :mod:`tests.eval.__main__`
(``python -m tests.eval``).
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from audio_recap import hook
from audio_recap.services import Services
from audio_recap.state import FileStateStore, State

from .prompts import Prompt

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_ROOT = REPO_ROOT / "tests" / "eval" / "runs"

# Tool set the agent is allowed. Read/Glob/Grep/Bash are the bare
# minimum for the prompts that explore the repo or write scratch files
# under ``/tmp``. Write/Edit are intentionally omitted — Bash is enough
# for the /tmp scaffolding prompts and dropping Write avoids accidental
# edits to the real working tree.
DEFAULT_ALLOWED_TOOLS = ["Read", "Glob", "Grep", "Bash"]

# Per-prompt ``claude`` CLI timeout (seconds). Picked generously for the
# few multi-step Bash prompts; one-sentence prompts return in 1-2s.
DEFAULT_PROMPT_TIMEOUT_S = 180.0


def _now_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%MZ")


def _parse_stream_json(stdout: str) -> tuple[list[dict[str, Any]], str | None]:
    """Parse ``claude -p --output-format stream-json`` output (JSONL).

    Returns ``(content_blocks, error)``. ``content_blocks`` aggregates
    every ``text`` and ``tool_use`` block across every ``assistant``
    event, in order, in CC's on-disk transcript shape — so the hook
    walks them as if they came from a real Stop event. ``thinking``
    blocks (and any other block type) are dropped: the hook only reads
    ``text`` and ``tool_use``.

    ``error`` is non-None when the final ``result`` event reports
    ``is_error`` — the caller records it on the turn but does not abort
    the run. Malformed (non-JSON) lines are skipped defensively.
    """

    blocks: list[dict[str, Any]] = []
    error: str | None = None
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "assistant":
            content = event.get("message", {}).get("content", [])
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    blocks.append({"type": "text", "text": b.get("text", "")})
                elif b.get("type") == "tool_use":
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": b.get("id", ""),
                            "name": b.get("name", ""),
                            "input": b.get("input", {}),
                        }
                    )
        elif etype == "result" and event.get("is_error") and not error:
            error = event.get("api_error_status") or "result reported is_error"
    return blocks, error


def _run_claude(
    prompt: str,
    *,
    cwd: Path,
    allowed_tools: list[str],
    timeout_s: float,
) -> tuple[list[dict[str, Any]], str | None]:
    """Dispatch one prompt to the ``claude`` CLI; return (blocks, error).

    ``--setting-sources ""`` skips user plugins (and their hooks);
    ``--no-session-persistence`` keeps the throwaway session off disk;
    auth is the developer's logged-in Claude Code session — no API key.
    A non-zero exit, a timeout, or an ``is_error`` result event all
    surface as the ``error`` string.
    """

    argv = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--setting-sources",
        "",
        "--allowedTools",
        " ".join(allowed_tools),
        "--permission-mode",
        "acceptEdits",
        "--no-session-persistence",
    ]
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [], f"claude CLI timeout after {timeout_s}s"
    except FileNotFoundError:
        return [], "claude CLI not found on PATH"

    blocks, error = _parse_stream_json(proc.stdout)
    if proc.returncode != 0 and not error:
        error = f"claude CLI exited {proc.returncode}: {proc.stderr.strip()!r}"
    return blocks, error


def _synthesize_stop_payload(
    blocks: list[dict[str, Any]], session_id: str, cwd: Path
) -> dict[str, Any]:
    """Build a CC Stop-event payload the hook will accept."""

    return {
        "session_id": session_id,
        "cwd": str(cwd),
        "hook_event_name": "Stop",
        "transcript": [{"role": "assistant", "content": blocks}],
    }


def _aggregate_assistant_text(blocks: list[dict[str, Any]]) -> str:
    """Join all text blocks with the same separator Audio Recap uses."""

    return "\n\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def _fire_hook(payload: dict[str, Any], run_dir: Path) -> tuple[int, str]:
    """Run the Stop hook in-process against ``payload``; return (exit, stderr).

    Builds the production :class:`Services` graph with every file-backed
    root redirected under ``run_dir`` — eventlog, on/off state, and
    narration cache all land in the bundle, never in the developer's
    ``~/.claude`` tree. The real :class:`SubprocessProcessRunner` is
    used (``runner`` defaults to it), so ``claude -p`` runs for real;
    ``say`` is skipped because the dry-run config is active.
    """

    raw = json.dumps(payload).encode("utf-8")
    services = Services.from_config(
        payload["cwd"],
        session_id=payload["session_id"],
        audio_recap_root=run_dir,
        transcript_root=run_dir / "cc_projects",
    )
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            rc = hook.main(raw, services=services)
    except Exception as e:  # defensive — hook.main is designed to return, not raise
        return 1, f"{type(e).__name__}: {e}"
    return rc, stderr.getvalue()


@contextlib.contextmanager
def _dry_run_config(repo_root: Path, run_dir: Path) -> Any:
    """Write ``<repo>/.audio-recap/config.json`` with dry_run=true; restore on exit.

    The hook reads its per-cwd config from the payload's ``cwd`` (the
    repo root), so dry-run has to be on disk there for the duration of
    the run. A pre-existing file is backed up to the run bundle and
    restored on exit, so a developer who manually toggled dry_run on
    this repo before invoking the runner doesn't lose their setting.
    """

    cfg_path = repo_root / ".audio-recap" / "config.json"
    backup: str | None = None
    had_dir = cfg_path.parent.is_dir()
    if cfg_path.exists():
        backup = cfg_path.read_text(encoding="utf-8")
        (run_dir / "config.json.backup").write_text(backup, encoding="utf-8")
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"dry_run": True}) + "\n", encoding="utf-8")
    try:
        yield cfg_path
    finally:
        if backup is not None:
            cfg_path.write_text(backup, encoding="utf-8")
        else:
            cfg_path.unlink(missing_ok=True)
            if not had_dir:
                with contextlib.suppress(OSError):
                    cfg_path.parent.rmdir()


def _run_prompt(
    prompt: Prompt,
    *,
    cwd: Path,
    run_dir: Path,
    allowed_tools: list[str],
    timeout_s: float,
) -> dict[str, Any]:
    """Dispatch one prompt; fire the hook on the aggregated transcript."""

    session_id = str(uuid.uuid4())
    # Pre-seed enabled state in the bundle-local store so the hook
    # doesn't exit on the default-off gate. ``_fire_hook`` wires the
    # hook's StateStore to this same ``run_dir/projects`` root (the
    # ``projects/`` subdir ``Services.from_config`` derives from
    # ``audio_recap_root=run_dir``).
    FileStateStore(run_dir / "projects").save(State(enabled=True), session_id, str(cwd))

    blocks, error = _run_claude(
        prompt["prompt"], cwd=cwd, allowed_tools=allowed_tools, timeout_s=timeout_s
    )

    payload = _synthesize_stop_payload(blocks, session_id, cwd)
    hook_exit, hook_stderr = _fire_hook(payload, run_dir)

    assistant_text = _aggregate_assistant_text(blocks)
    return {
        "n": prompt["n"],
        "shape": prompt["shape"],
        "prompt": prompt["prompt"],
        "session_id": session_id,
        "assistant_text": assistant_text,
        "assistant_word_count": len(assistant_text.split()),
        "text_block_count": sum(1 for b in blocks if b.get("type") == "text"),
        "tool_use_count": sum(1 for b in blocks if b.get("type") == "tool_use"),
        "cli_error": error,
        "hook_exit": hook_exit,
        "hook_stderr": hook_stderr,
    }


def run(
    prompts: list[Prompt],
    *,
    repo_root: Path = REPO_ROOT,
    runs_root: Path = RUNS_ROOT,
    allowed_tools: list[str] | None = None,
    timeout_s: float = DEFAULT_PROMPT_TIMEOUT_S,
) -> Path:
    """Run ``prompts`` through the ``claude`` CLI; return the run bundle dir."""

    if allowed_tools is None:
        allowed_tools = list(DEFAULT_ALLOWED_TOOLS)

    run_id = _now_run_id()
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    with _dry_run_config(repo_root, run_dir):
        turns: list[dict[str, Any]] = []
        for prompt in prompts:
            sys.stderr.write(f"[eval] turn {prompt['n']:>2} ({prompt['shape']}) — dispatching…\n")
            turn = _run_prompt(
                prompt,
                cwd=repo_root,
                run_dir=run_dir,
                allowed_tools=allowed_tools,
                timeout_s=timeout_s,
            )
            turns.append(turn)
            sys.stderr.write(
                f"[eval] turn {prompt['n']:>2} done "
                f"(blocks={turn['text_block_count']}+{turn['tool_use_count']} "
                f"hook_exit={turn['hook_exit']})\n"
            )

    state = {
        "run_id": run_id,
        "repo_root": str(repo_root),
        "turn_count": len(turns),
        "allowed_tools": allowed_tools,
        "turns": turns,
    }
    (run_dir / "state.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    sys.stderr.write(f"[eval] run bundle: {run_dir}\n")
    return run_dir


def check_run(data: dict[str, Any]) -> list[str]:
    """Check a harvested ``data.json`` for hard failures.

    Returns a list of human-readable failure descriptions — an empty
    list means every turn completed cleanly. A failure here is genuine
    breakage, not a quality signal: a non-zero hook exit, a ``claude``
    CLI error, a non-enabled state gate, a non-dry-run TTS status (the
    run is dry-run, so anything else means the TTS stage broke), or both
    recap backends down (``recap_path=none``).

    A ``claude_p`` → ``rule_based`` / verbatim fallback is deliberately
    NOT a failure — it is the system handling a failure gracefully.
    Grading fallback *quality* is the deferred scorer's job; the CLI
    surfaces fallback counts as a separate soft signal.
    """

    failures: list[str] = []
    for t in data.get("turns", []):
        n = t.get("n", "?")
        ar = t.get("audio_recap", {})
        if t.get("hook_exit") != 0:
            failures.append(f"turn {n}: hook_exit={t.get('hook_exit')}")
        if t.get("cli_error"):
            failures.append(f"turn {n}: cli_error={t['cli_error']}")
        if ar.get("state") != "enabled":
            failures.append(f"turn {n}: state={ar.get('state')!r} (expected 'enabled')")
        if ar.get("tts_status") != "dry_run":
            failures.append(f"turn {n}: tts_status={ar.get('tts_status')!r} (expected 'dry_run')")
        if ar.get("recap_path") == "none":
            failures.append(f"turn {n}: recap_path=none (both recap backends failed)")
    return failures
