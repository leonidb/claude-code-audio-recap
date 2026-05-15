"""CLI entry point for the Audio Recap eval runner — ``python -m tests.eval``.

Dispatches the prompt set through the ``claude`` CLI, fires the Stop
hook in-process on each turn, harvests the bundle, and checks the
no-failure invariants. Prints a one-line verdict to stdout and exits
non-zero if any turn broke.

    python -m tests.eval               # full prompt set (~16, ~3-5 min)
    python -m tests.eval --smoke       # first 3 prompts (~30s)
    python -m tests.eval -n 5          # first N prompts
    python -m tests.eval --timeout 90  # per-prompt claude CLI timeout (seconds)

Auth is the developer's logged-in ``claude`` CLI session — no
``ANTHROPIC_API_KEY`` needed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .harvest import harvest_run
from .prompts import PROMPTS, smoke_prompts
from .runner import DEFAULT_PROMPT_TIMEOUT_S, check_run, run


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m tests.eval", description=__doc__)
    p.add_argument(
        "--smoke",
        action="store_true",
        help="Run only the first 3 prompts (sanity-check; ~30s, ~$0.05).",
    )
    p.add_argument("-n", type=int, default=None, help="Run only the first N prompts.")
    p.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_PROMPT_TIMEOUT_S,
        help=f"Per-prompt claude CLI timeout (s). Default {DEFAULT_PROMPT_TIMEOUT_S:g}.",
    )
    return p.parse_args(argv)


def _print_verdict(data: dict[str, Any], failures: list[str], run_dir: Path) -> None:
    """Print the run verdict to stdout — the deliberate result of the run.

    Genuine breakage (``failures``) goes red; a ``rule_based`` recap
    fallback is surfaced as a soft signal, not a failure.
    """

    turns = data.get("turns", [])
    total = len(turns)
    fallbacks = sum(1 for t in turns if t.get("audio_recap", {}).get("recap_path") == "rule_based")
    if failures:
        print(f"\n✗ {len(failures)} failure(s) across {total} turn(s):")
        for f in failures:
            print(f"    {f}")
    else:
        soft = f" ({fallbacks} used the rule_based fallback — see results.md)" if fallbacks else ""
        print(f"\n✓ {total}/{total} turns clean{soft}")
    print(f"  bundle: {run_dir}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.smoke and args.n:
        sys.stderr.write("--smoke and -n are mutually exclusive\n")
        return 2

    if args.smoke:
        prompts = smoke_prompts(3)
    elif args.n is not None:
        prompts = PROMPTS[: args.n]
    else:
        prompts = list(PROMPTS)

    run_dir = run(prompts, timeout_s=args.timeout)
    data = json.loads(harvest_run(run_dir).read_text(encoding="utf-8"))
    failures = check_run(data)
    _print_verdict(data, failures, run_dir)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
