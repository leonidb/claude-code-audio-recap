"""Harvest Audio Recap log + runner state.json into structured per-turn data.

Reads:
  - ``<run>/state.json`` produced by :mod:`tests.eval.runner`
    (per-turn prompt, session_id, assistant text, block counts, …).
  - ``<run>/logs/audio-recap.log`` produced by the bundle-local hook fires.

Writes:
  - ``<run>/data.json`` — per-turn raw data joining hook-side
    fields (recap_path, recap_text, summary_path, message_text,
    tts_status, …) with runner-side fields (assistant_text,
    text_block_count, …).
  - ``<run>/results.md`` — human-readable rendering: prompt, recap,
    summary, full assistant text per turn.

Scoring + narrative analysis is out of scope for this stage; a
maintainer reads ``results.md`` (or ``data.json``) and adds them in a
separate pass.

The KV parser at the heart of this module handles the eventlog's
single-quoted value escapes (``'\\''`` for an embedded apostrophe
plus ``\\n`` / ``\\t`` / ``\\r`` for control chars), ported verbatim
from the 2026-05-04T07-13Z reference run's ``harvest.py`` so output
shape stays comparable across runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# POSIX-style apostrophe escape inside a single-quoted value:
# ``it's`` round-trips as ``'it'\\''s'``. Walk character by character
# so the parser handles this without a regex.
_ESC_APOS = "'\\''"  # close-quote, backslash, quote, open-quote


def parse_kv(line: str) -> dict[str, str]:
    """Parse one ``key=value`` eventlog line into a dict.

    Accepts both the unquoted value form (``foo=bar``) and the
    single-quoted form (``foo='bar baz'``) the eventlog emits when a
    value contains spaces, ``=``, or apostrophes. Embedded
    apostrophes use the POSIX escape ``'\\''``; embedded newlines /
    tabs / CRs are stored as literal ``\\n`` / ``\\t`` / ``\\r``
    pairs and are unescaped here so the returned values round-trip.

    Tokens that don't fit either form (e.g. the leading ISO-8601
    timestamp and the level word) are skipped.
    """

    out: dict[str, str] = {}
    i = 0
    n = len(line)
    while i < n:
        while i < n and line[i].isspace():
            i += 1
        if i >= n:
            break
        j = i
        while j < n and (line[j].isalnum() or line[j] == "_"):
            j += 1
        if j >= n or line[j] != "=" or j == i:
            # Not a key=value token — skip the next whitespace-bounded word.
            while i < n and not line[i].isspace():
                i += 1
            continue
        key = line[i:j]
        i = j + 1
        if i < n and line[i] == "'":
            i += 1
            buf: list[str] = []
            while i < n:
                if line[i : i + 4] == _ESC_APOS:
                    buf.append("'")
                    i += 4
                elif line[i] == "'":
                    i += 1
                    break
                else:
                    buf.append(line[i])
                    i += 1
            val = "".join(buf)
        else:
            j = i
            while j < n and not line[j].isspace():
                j += 1
            val = line[i:j]
            i = j
        val = val.replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
        out[key] = val
    return out


def fires(log_lines: list[str]) -> list[list[dict[str, str]]]:
    """Group consecutive INFO lines into per-fire bundles.

    A fire starts on the line carrying ``fired=true`` and runs until
    the next such marker. TRACE lines are ignored — quality
    assessment runs at INFO level.
    """

    groups: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    for line in log_lines:
        if " INFO " not in line:
            continue
        kv = parse_kv(line)
        kv["_ts"] = line.split()[0] if line.split() else ""
        if kv.get("fired") == "true" and current:
            groups.append(current)
            current = []
        current.append(kv)
    if current:
        groups.append(current)
    return groups


def collapse(group: list[dict[str, str]]) -> dict[str, str]:
    """Merge a fire's lines into one dict; later values win.

    The hook emits separate INFO lines for state, pre-TTS, and
    post-TTS phases. Merging gives the harvest a single
    self-contained record per fire — later fields (recap_text,
    message_text on the pre-TTS line; tts_status on success or
    failure) overwrite earlier placeholders.
    """

    merged: dict[str, str] = {}
    for kv in group:
        merged.update(kv)
    if group:
        merged["_first_ts"] = group[0].get("_ts", "")
        merged["_last_ts"] = group[-1].get("_ts", "")
    return merged


def _index_fires_by_session(
    grouped: list[dict[str, str]],
) -> dict[str, dict[str, str]]:
    """Return ``session_id → first matching fire`` for join.

    The runner uses a fresh UUID per prompt, so a session_id appears
    at most once in the bundle-local log.
    """

    by_sid: dict[str, dict[str, str]] = {}
    for g in grouped:
        sid = g.get("session_id", "")
        if sid and sid not in by_sid:
            by_sid[sid] = g
    return by_sid


def _int_field(record: dict[str, str], key: str) -> int:
    raw = record.get(key, "")
    try:
        return int(raw) if raw else 0
    except ValueError:
        return 0


def harvest_run(run_dir: Path) -> Path:
    """Produce ``data.json`` + ``results.md`` for ``run_dir``."""

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    log_lines = (run_dir / "logs" / "audio-recap.log").read_text(encoding="utf-8").splitlines()
    grouped = [collapse(g) for g in fires(log_lines) if g]
    by_sid = _index_fires_by_session(grouped)

    turns: list[dict[str, Any]] = []
    for t in state["turns"]:
        sid = t["session_id"]
        n = by_sid.get(sid, {})
        turns.append(
            {
                "n": t["n"],
                "shape": t["shape"],
                "prompt": t["prompt"],
                "session_id": sid,
                "assistant_full_text": t.get("assistant_text", ""),
                "assistant_word_count": t.get("assistant_word_count", 0),
                "text_block_count": t.get("text_block_count", 0),
                "tool_use_count": t.get("tool_use_count", 0),
                "cli_error": t.get("cli_error"),
                "hook_exit": t.get("hook_exit", -1),
                "audio_recap": {
                    "state": n.get("state", ""),
                    "recap_path": n.get("recap_path", ""),
                    "recap_text": n.get("recap_text", ""),
                    "recap_words": _int_field(n, "recap_words"),
                    "recap_error": (
                        n.get("error", "") if n.get("recap_path", "").endswith("_failed") else ""
                    ),
                    "summary_path": n.get("summary_path", ""),
                    "summary_words": _int_field(n, "summary_words"),
                    "message_text": n.get("message_text", ""),
                    "message_words_pre": _int_field(n, "msg_words_pre"),
                    "message_words_post": _int_field(n, "msg_words_post"),
                    "tts_status": n.get("tts_status", ""),
                    "tool_uses": _int_field(n, "tool_uses"),
                    "first_ts": n.get("_first_ts", ""),
                },
            }
        )

    data = {
        "run_id": state["run_id"],
        "turn_count": len(turns),
        "turns": turns,
    }
    out_path = run_dir / "data.json"
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    results_path = run_dir / "results.md"
    results_path.write_text(_render_results_md(data), encoding="utf-8")
    return out_path


def _render_results_md(data: dict[str, Any]) -> str:
    """Produce a Markdown rendering of the harvested data."""

    lines: list[str] = []
    lines.append(f"# Eval run {data['run_id']}")
    lines.append("")
    lines.append(f"{data['turn_count']} turns.")
    lines.append("")
    for t in data["turns"]:
        n = t["audio_recap"]
        lines.append(f"## Turn {t['n']} — {t['shape']}")
        lines.append("")
        lines.append(f"**Prompt:** {t['prompt']}")
        lines.append("")
        lines.append(
            f"**Stats:** {t['text_block_count']} text blocks · "
            f"{t['tool_use_count']} tool uses · "
            f"{t['assistant_word_count']} words assistant text · "
            f"hook exit {t['hook_exit']}"
        )
        if t.get("cli_error"):
            lines.append("")
            lines.append(f"**CLI error:** `{t['cli_error']}`")
        lines.append("")
        lines.append(
            f"**Recap** ({n['recap_path']}, {n['recap_words']}w): {n['recap_text'] or '_(empty)_'}"
        )
        lines.append("")
        lines.append(
            f"**Message** ({n['summary_path']}, "
            f"{n['summary_words']}w from {n['message_words_pre']}w · "
            f"tts_status={n['tts_status']}):"
        )
        lines.append("")
        lines.append(f"> {n['message_text'] or '_(empty)_'}")
        lines.append("")
        lines.append("<details><summary>Assistant full text</summary>")
        lines.append("")
        lines.append("```")
        lines.append(t["assistant_full_text"] or "(empty)")
        lines.append("```")
        lines.append("")
        lines.append("</details>")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:  # pragma: no cover — thin CLI
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", type=Path, help="Path to the run bundle directory.")
    args = p.parse_args(argv)
    out = harvest_run(args.run_dir)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
