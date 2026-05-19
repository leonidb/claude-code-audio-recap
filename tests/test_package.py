from __future__ import annotations

from importlib.metadata import version

import audio_recap


def test_version_matches_packaging_metadata() -> None:
    assert audio_recap.__version__ == version("claude-code-audio-recap")
