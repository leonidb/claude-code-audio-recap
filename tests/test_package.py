from __future__ import annotations

import audio_recap


def test_version_is_set() -> None:
    assert audio_recap.__version__ == "0.0.1"
