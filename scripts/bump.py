#!/usr/bin/env python3
"""Bump the Audio Recap version across every surface that carries it.

``uv`` owns ``pyproject.toml`` + ``uv.lock`` and does the semver math, so
this script runs ``uv version --bump`` for that, then propagates the
resulting version into the two surfaces uv does not know about:
``.claude-plugin/plugin.json`` and ``audio_recap/__init__.py``.

The marketplace catalog version (``.claude-plugin/marketplace.json``) is
intentionally left alone — it versions the marketplace, not the plugin.

Usage::

    python scripts/bump.py {major|minor|patch}

It edits files only; committing and tagging the release point stay manual.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_JSON = _ROOT / ".claude-plugin" / "plugin.json"
_INIT = _ROOT / "audio_recap" / "__init__.py"

# Anchored so each pattern matches exactly the one version literal in its
# file; the ``subn`` count check below then fails loudly if a field was
# moved or renamed, rather than silently leaving a surface unbumped.
_PLUGIN_RE = re.compile(r'("version"\s*:\s*")\d+\.\d+\.\d+(")')
_INIT_RE = re.compile(r'(__version__\s*=\s*")\d+\.\d+\.\d+(")')
_SEMVER_RE = re.compile(r"\d+\.\d+\.\d+")


def _assert_one_match(path: Path, pattern: re.Pattern[str]) -> None:
    """Fail before uv mutates anything if a surface can't be synced cleanly."""
    count = len(pattern.findall(path.read_text(encoding="utf-8")))
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one version literal, found {count}")


def _bump_surface(path: Path, pattern: re.Pattern[str], version: str) -> None:
    # Function replacement (not a template) so ``version`` is inserted literally —
    # a stray backslash or ``\g<...>`` in it can't be read as a backreference.
    text = path.read_text(encoding="utf-8")
    new_text, count = pattern.subn(lambda m: f"{m.group(1)}{version}{m.group(2)}", text)
    if count != 1:
        raise SystemExit(f"{path}: expected exactly one version literal, found {count}")
    path.write_text(new_text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bump the Audio Recap version across all version surfaces."
    )
    parser.add_argument("part", choices=["major", "minor", "patch"])
    args = parser.parse_args(argv)

    # Pre-flight: confirm both synced surfaces have exactly one version literal
    # before uv mutates pyproject.toml / uv.lock, so a renamed or reformatted
    # field fails cleanly instead of leaving a half-bumped tree.
    _assert_one_match(_PLUGIN_JSON, _PLUGIN_RE)
    _assert_one_match(_INIT, _INIT_RE)

    # uv (pinned to the repo root, not the caller's cwd) updates pyproject.toml
    # and re-locks uv.lock; --no-sync skips the venv reinstall.
    subprocess.run(["uv", "version", "--bump", args.part, "--no-sync"], check=True, cwd=_ROOT)
    version = subprocess.run(
        ["uv", "version", "--short"],
        check=True,
        capture_output=True,
        text=True,
        cwd=_ROOT,
    ).stdout.strip()
    if not _SEMVER_RE.fullmatch(version):
        raise SystemExit(f"uv returned an unexpected version: {version!r}")

    _bump_surface(_PLUGIN_JSON, _PLUGIN_RE, version)
    _bump_surface(_INIT, _INIT_RE, version)
    print(f"bumped to {version} — pyproject + uv.lock via uv; plugin.json + __init__ synced")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
