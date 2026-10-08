"""Configuration schema.

Frozen dataclasses matching the Config surface described in
``docs/architecture.md``. Defaults live here and are authoritative.

Nothing is read from disk: there is no config file. :meth:`Config.default`
returns the shipped defaults, and the composition root
(:meth:`audio_recap.services.Services.from_config`) derives the only two
values that ever differ from them: ``log_level`` from the
``AUDIO_RECAP_LOG_LEVEL`` environment variable, and ``dry_run`` from a
keyword the eval harness passes in code.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Recap:
    model: str = "claude-haiku-4-5"
    # 60s: a 15s ceiling was too tight — transient claude -p cold-start
    # / load made routine turns overrun it and fall back to the
    # rule-based recap, degrading quality. Recap and summarizer share
    # the claude -p binary, the cold-start, and run in parallel in the
    # hook, so giving recap the same 60s ceiling as the summarizer
    # doesn't compound total wall time.
    timeout_s: float = 60.0
    skip_if_no_tool_use: bool = True
    skip_if_message_words_lt: int = 30


@dataclass(frozen=True)
class Summarizer:
    model: str = "claude-haiku-4-5"
    # 60s: a 25s ceiling was too tight — transient claude -p cold-start
    # / load (not input size) made some runs overrun it. Most
    # successful runs land well under, so 60s buys headroom without
    # changing typical-case behavior; the trade is dead-air time when
    # the summarizer does fail. Recap shares this 60s ceiling for the
    # same cold-start reason — see :class:`Recap`.
    timeout_s: float = 60.0


@dataclass(frozen=True)
class Narration:
    # Messages over this many words are summarized via ``claude -p`` instead
    # of spoken verbatim. 50 words ≈ 20s at 150 wpm — the upper end of
    # the "verbatim if under ~10-20s spoken" target.
    summarize_message_over_words: int = 50


@dataclass(frozen=True)
class TTS:
    voice: str | None = None
    rate_wpm: int | None = None
    # Reference speech rate (words per minute) used to compute
    # ``expected_say_s = words * 60 / baseline_wpm``. 142 wpm is the
    # cluster median observed across clean fires in the eventlog;
    # using a fixed reference rather than a running median
    # keeps the comparison stable across sessions and across users
    # running different voices. The number drives the fallback gate
    # too (``aiff_duration_s < expected_s * 0.7`` triggers the
    # streaming fallback in
    # :class:`audio_recap.tts.macos_say.MacOSSay`).
    baseline_wpm: int = 142


def _default_transforms() -> list[str]:
    return [
        "code_blocks",
        "urls",
        "time_units",
        "ratios",
        "paths",
        "currency",
        "percent",
        "symbols",
    ]


@dataclass(frozen=True)
class Config:
    recap: Recap = field(default_factory=Recap)
    summarizer: Summarizer = field(default_factory=Summarizer)
    narration: Narration = field(default_factory=Narration)
    tts: TTS = field(default_factory=TTS)
    speakable_transforms: list[str] = field(default_factory=_default_transforms)
    language: str = "en"
    # When True, the Stop hook and ``/audio-recap:repeat`` skip the ``say``
    # subprocess but keep recap generation, summarization, and event
    # logging on, and log the recap and message text at INFO. No user
    # setting reaches it: the eval harness sets it in code
    # (``Services.from_config(..., dry_run=True)``) to answer "what would
    # it have said?" without making a sound.
    #
    # Note the cost is not zero — ``claude -p`` still runs for real. This
    # suppresses playback, not work.
    dry_run: bool = False
    # Event-log verbosity: ``"info"`` (default) or ``"trace"``. TRACE
    # adds the full hook payload, the verbatim claude -p prompts and
    # responses, per-segment transform text, and tracebacks — high
    # volume, off by default, opt in while debugging by setting
    # ``AUDIO_RECAP_LOG_LEVEL=trace`` (see
    # :func:`audio_recap.services.resolve_log_level`). The composition
    # root maps this to the event log's ``trace_enabled`` flag; any
    # value other than ``"trace"`` is treated as ``"info"``.
    log_level: str = "info"
    # Crash-safety net (seconds) for the cross-session presence registry — NOT
    # an idleness timeout. A heartbeat is retired when the session switches
    # narration off or closes (see :mod:`audio_recap.presence`), so the only way
    # one outlives its session is a hard kill that runs no SessionEnd. This is
    # the horizon past which such an orphan stops counting.
    #
    # It is deliberately long: two sessions open side by side must name
    # themselves however long ago either last spoke, so a session that has gone
    # quiet for an hour is still a neighbour worth disambiguating from. Ageing
    # it out on idleness is the one thing this must not do. 24h means an orphan
    # from a crash can cost an unneeded label for the rest of the day — the
    # harmless direction, and the same one a stale heartbeat always erred in.
    presence_window_s: int = 86_400
    # Word cap on the spoken session label (see :mod:`audio_recap.label`). The
    # label is a cue, not a sentence — it rides in front of the narration, so a
    # long one delays what the listener is actually waiting for. 5 fits a real
    # rename ("nbrown-clean sensei") and a two-segment project path.
    label_max_words: int = 5

    @classmethod
    def default(cls) -> Config:
        """Return a fully default-configured :class:`Config`.

        Equivalent to ``Config()``; present as a named constructor so
        call sites can read as ``Config.default()`` when the intent is
        "give me the shipped defaults" rather than "construct with no
        overrides."
        """

        return cls()
