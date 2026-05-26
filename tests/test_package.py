from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

import audio_recap


def test_version_matches_packaging_metadata() -> None:
    assert audio_recap.__version__ == version("claude-code-audio-recap")


def test_plugin_manifest_version_matches() -> None:
    """``plugin.json`` is the live install surface and nothing else guards it.

    A drift here ships a same-number/different-bytes build to users, so the
    manifest version must track ``__version__`` (which the test above ties to
    the packaging metadata). The bump tooling keeps all three in lockstep.
    """

    manifest = json.loads(
        (Path(__file__).parents[1] / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["version"] == audio_recap.__version__
