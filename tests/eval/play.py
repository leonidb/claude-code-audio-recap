"""Play recap + message for an eval-run turn through macOS ``say``.

A run bundle's ``data.json`` carries every turn's recap_text and
message_text. This is the mini-CLI that converts the harvested data
back into audio, useful when reviewing a quality finding away from
the keyboard.

Usage:
    python -m tests.eval.play <run_dir> <N>
    python -m tests.eval.play <run_dir> <N> --recap
    python -m tests.eval.play <run_dir> <N> --message
    python -m tests.eval.play <run_dir> list
    python -m tests.eval.play <run_dir> all
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def _say(text: str) -> None:
    if not text:
        return
    subprocess.run(["say", "--", text], check=False)


def _play_turn(t: dict[str, Any], *, recap: bool, message: bool) -> None:
    n = t["audio_recap"]
    print(f"\n=== Turn {t['n']} — {t['shape']} ===")
    print(f"Prompt: {t['prompt']}")
    if recap:
        print(f"\nRecap ({n['recap_path']}, {n['recap_words']}w):")
        print(f"  {n['recap_text'] or '(empty)'}")
        _say(n["recap_text"])
    if message:
        print(
            f"\nMessage ({n['summary_path']}, "
            f"{n['summary_words']}w from {n['message_words_pre']}w):"
        )
        print(f"  {n['message_text'] or '(empty)'}")
        _say(n["message_text"])


def _list_turns(data: dict[str, Any]) -> None:
    for t in data["turns"]:
        n = t["audio_recap"]
        recap_preview = (n.get("recap_text") or "")[:60]
        print(f"{t['n']:>2} {t['shape']:30} | recap: {recap_preview}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", type=Path, help="Path to the run bundle directory.")
    p.add_argument("target", help="turn number, or 'list', or 'all'")
    p.add_argument("--recap", action="store_true", help="only play recap")
    p.add_argument("--message", action="store_true", help="only play message")
    args = p.parse_args(argv)

    data_path = args.run_dir / "data.json"
    if not data_path.exists():
        print(f"no data.json under {args.run_dir} — run harvest first", file=sys.stderr)
        return 2
    data = json.loads(data_path.read_text(encoding="utf-8"))

    if args.target == "list":
        _list_turns(data)
        return 0

    play_recap = (not args.recap and not args.message) or args.recap
    play_message = (not args.recap and not args.message) or args.message

    if args.target == "all":
        for t in data["turns"]:
            _play_turn(t, recap=play_recap, message=play_message)
            try:
                input("\nPress Enter for next turn (Ctrl-C to stop)…")
            except (EOFError, KeyboardInterrupt):
                print()
                return 0
        return 0

    try:
        n = int(args.target)
    except ValueError:
        print(f"unknown target: {args.target!r}", file=sys.stderr)
        return 2

    for t in data["turns"]:
        if t["n"] == n:
            _play_turn(t, recap=play_recap, message=play_message)
            return 0
    print(f"no turn {n} found", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
