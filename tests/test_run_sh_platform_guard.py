"""``scripts/run.sh`` off macOS: stop before Python.

Audio Recap speaks with the macOS ``say`` command. On any other platform the
hooks must stay silent (a hook error would show after every turn) and a slash
command must say plainly that the plugin is macOS-only, without running Python.
These tests run on Linux, where the guard fires (CI runs there); on macOS
``run.sh`` goes on to Python and ``tests/test_run_sh_cwd_isolation.py`` covers
it instead. They are not run on Windows, where ``HOME`` and paths differ.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_SH = REPO_ROOT / "scripts" / "run.sh"

pytestmark = [
    pytest.mark.skipif(
        not sys.platform.startswith("linux"),
        reason="the guard fires off macOS; these checks rely on Linux (the CI runner) semantics",
    ),
    pytest.mark.skipif(shutil.which("bash") is None, reason="bash required to run run.sh"),
]


def _run(args: list[str], home: Path, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(RUN_SH), str(REPO_ROOT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=home,
        env={**os.environ, "HOME": str(home)},
        check=False,
    )


def test_stop_hook_exits_silently_without_python(tmp_path: Path) -> None:
    """Invalid stdin would make the Python hook print and exit 1; the guard does neither."""

    result = _run(["hook"], tmp_path, stdin="not json")

    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert not (tmp_path / ".claude").exists()


def test_session_end_hook_exits_silently_without_python(tmp_path: Path) -> None:
    """The Python handler would delete this session's heartbeat; the guard leaves it."""

    heartbeat = tmp_path / ".claude" / "audio-recap" / "active" / "sid-a"
    heartbeat.parent.mkdir(parents=True)
    heartbeat.touch()

    result = _run(["session-end"], tmp_path, stdin=json.dumps({"session_id": "sid-a"}))

    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    assert heartbeat.exists()


def test_slash_command_says_macos_only_and_changes_nothing(tmp_path: Path) -> None:
    """``/audio-recap:on`` off macOS reports the limit and writes no state."""

    result = _run(["command", "--session-id", "sid-a", "--cwd", str(tmp_path), "on"], tmp_path)

    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        "Audio Recap works only on macOS.\n",
        "",
    )
    assert not (tmp_path / ".claude").exists()
