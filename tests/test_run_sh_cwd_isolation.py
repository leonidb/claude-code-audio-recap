"""``scripts/run.sh`` must run the installed package, not a cwd decoy.

``python3 -m`` puts the process working directory first on ``sys.path``, so a
stray ``audio_recap/`` directory in the user's working directory (a checkout of
this repo, or a worktree) would shadow the installed plugin and run stale code.
``run.sh`` cd's to the plugin root before exec to stop that; this test locks in
the fix end to end.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_SH = REPO_ROOT / "scripts" / "run.sh"


def _plant_decoy(directory: Path) -> None:
    """Plant an ``audio_recap/`` package that exits 123 loudly if imported."""

    pkg = directory / "audio_recap"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "__main__.py").write_text(
        'import sys\nsys.stdout.write("DECOY\\n")\nsys.exit(123)\n',
        encoding="utf-8",
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required to run run.sh")
def test_run_sh_decoy_in_cwd_does_not_shadow_installed_package(tmp_path: Path) -> None:
    """Invoked from a cwd holding a decoy ``audio_recap/``, run.sh runs the real one."""

    _plant_decoy(tmp_path)

    result = subprocess.run(
        ["bash", str(RUN_SH), str(REPO_ROOT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    # The decoy prints DECOY and exits 123; the real package prints its usage
    # banner and exits 2 when handed no subcommand.
    assert "DECOY" not in output, f"cwd decoy shadowed the installed package:\n{output}"
    assert result.returncode == 2, output
    assert "usage: python -m audio_recap" in output
