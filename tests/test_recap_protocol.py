from __future__ import annotations

from audio_recap.recap import Recap, RecapFailed


def test_recap_failed_is_an_exception() -> None:
    assert issubclass(RecapFailed, Exception)


def test_recap_protocol_exposes_generate() -> None:
    # Static-duck-typing check: any object with the right-shaped generate
    # method satisfies the Protocol. This asserts the Protocol object is
    # importable and spelled how the rest of the package expects it.
    assert hasattr(Recap, "generate")
