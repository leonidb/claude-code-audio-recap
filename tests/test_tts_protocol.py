from __future__ import annotations

from audio_recap.config import TTS as TTSConfig
from audio_recap.tts import TTS, TTSFailed
from audio_recap.tts.macos_say import MacOSSay
from tests.fakes import FakeProcessRunner


def test_tts_failed_is_an_exception() -> None:
    assert issubclass(TTSFailed, Exception)


def test_tts_protocol_exposes_speak() -> None:
    assert hasattr(TTS, "speak")


def test_macos_say_satisfies_protocol() -> None:
    # Structural: MacOSSay must be usable anywhere a TTS is expected.
    impl: TTS = MacOSSay(TTSConfig(), FakeProcessRunner())
    assert hasattr(impl, "speak")
