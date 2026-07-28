"""Configuration schema.

Frozen dataclasses matching the Config surface described in
``docs/architecture.md``. Defaults live here and are authoritative.

Two ways to obtain a :class:`Config` at call sites:

- :meth:`Config.default` returns the shipped defaults, untouched.
- :meth:`Config.load` overlays optional per-cwd overrides read from
  ``<cwd>/.audio-recap/config.json``. Absent file → defaults; malformed
  payload → log a warning to eventlog and fall back to defaults.

The per-cwd loader keeps the file shape minimal: ``{"dry_run": false}``
is the only field today. New primitive fields plug in via a single
``isinstance``-check in :func:`_apply_overrides`; nested dataclass
overrides are deferred until a real need shows up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from audio_recap.eventlog import EventLog


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
    # When True, the Stop hook captures a TRACE-level excerpt from
    # ``log show --predicate '(subsystem CONTAINS "speech") OR
    # coreaudio OR speechsynthesisd'`` per fire. The
    # ``Input data proc returned inconsistent N packets`` line in
    # that excerpt is the FB13188396 fingerprint when chasing
    # synthesizer-side truncation. Off by default — adds ~0.1-0.3s
    # per fire and bloats TRACE volume; flip on only while actively
    # investigating Mechanism #2.
    trace_speech_log: bool = False


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
    # logging on. Set via the per-cwd ``.audio-recap/config.json`` (see
    # :meth:`Config.load`).
    #
    # What it is for: answering "what would it have said?" without making a
    # sound. Paired with ``log_level: "trace"`` the recap and message text
    # land in the event log, which is the way to reproduce a narration
    # complaint on someone else's machine, or to watch the transforms on a
    # turn without listening to it.
    #
    # Note the cost is not zero — ``claude -p`` still runs for real. This
    # suppresses playback, not work.
    dry_run: bool = False
    # When True, a fresh session in this cwd starts with Audio Recap ON
    # without requiring ``/audio-recap:on``. Only kicks in on the
    # ``FileNotFoundError`` branch in :meth:`audio_recap.state.FileStateStore.load`
    # — once a state file exists it stays authoritative, so an explicit
    # ``/audio-recap:off`` persists for the rest of the session even
    # with this flag enabled. Corrupt / wrong-shape state files still
    # default to ``False`` regardless of this flag.
    default_enabled: bool = False
    # Event-log verbosity: ``"info"`` (default) or ``"trace"``. TRACE
    # adds the full hook payload, the verbatim claude -p prompts and
    # responses, per-segment transform text, and tracebacks — high
    # volume, off by default, opt in while debugging. The composition
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
    # Lower it if you hard-kill sessions often and would rather have the
    # opposite trade.
    presence_window_s: int = 86_400
    # Word cap on the spoken session label (see :mod:`audio_recap.label`). The
    # label is a cue, not a sentence — it rides in front of the narration, so a
    # long one delays what the listener is actually waiting for. 5 fits a real
    # rename ("nbrown-clean sensei") and a two-segment project path; raise it if
    # your session names are longer.
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

    @classmethod
    def load(cls, cwd: str, *, eventlog: EventLog) -> Config:
        """Return defaults overlaid with ``<cwd>/.audio-recap/config.json``.

        The file is optional. Resolution order:

        1. Empty / missing ``cwd``, or no ``.audio-recap/config.json``
           at that path → return :meth:`default`.
        2. Read fails (``OSError``), JSON is malformed, or the payload
           is not a JSON object → log ``event=config error=...`` to
           ``eventlog`` and return :meth:`default`. Never raises.
        3. Otherwise apply known fields via :func:`_apply_overrides`.

        Unknown fields are silently ignored so a newer config file on
        an older plugin doesn't break. ``eventlog`` is required — the
        composition root threads in the same instance the rest of the
        graph uses; the bad-config warning goes there.
        """

        base = cls.default()
        if not cwd:
            return base
        path = Path(cwd) / ".audio-recap" / "config.json"
        if not path.is_file():
            return base
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            eventlog.event("config", path=str(path), error=str(e))
            return base
        if not isinstance(data, dict):
            eventlog.event("config", path=str(path), error="non-object payload")
            return base
        return _apply_overrides(base, data)


def _apply_overrides(base: Config, data: dict[str, Any]) -> Config:
    """Apply known v1 fields from ``data`` over ``base``.

    Adding a new top-level primitive field is a one-line schema bump:
    append another ``isinstance`` guard below. The guard exists so a
    typo in the JSON (``"dry_run": "yes"``) silently falls back to the
    default rather than producing a broken-typed Config.
    """

    overrides: dict[str, Any] = {}
    if isinstance(data.get("dry_run"), bool):
        overrides["dry_run"] = data["dry_run"]
    if isinstance(data.get("default_enabled"), bool):
        overrides["default_enabled"] = data["default_enabled"]
    # Only ``"trace"`` turns TRACE on; any other value (incl. a typo or
    # wrong type) leaves the ``"info"`` default.
    if data.get("log_level") == "trace":
        overrides["log_level"] = "trace"
    window = data.get("presence_window_s")
    if isinstance(window, int) and not isinstance(window, bool) and window > 0:
        overrides["presence_window_s"] = window
    max_words = data.get("label_max_words")
    if isinstance(max_words, int) and not isinstance(max_words, bool) and max_words > 0:
        overrides["label_max_words"] = max_words

    # Nested ``tts`` sub-object — only the diagnostic-related fields
    # are exposed today. ``voice`` / ``rate_wpm`` stay Python-level
    # defaults until we have a real reason to expose them in the
    # per-cwd schema. New fields below follow the same one-line
    # ``isinstance`` pattern as the top-level guards above.
    tts_data = data.get("tts")
    if isinstance(tts_data, dict):
        tts_overrides: dict[str, Any] = {}
        if isinstance(tts_data.get("trace_speech_log"), bool):
            tts_overrides["trace_speech_log"] = tts_data["trace_speech_log"]
        baseline = tts_data.get("baseline_wpm")
        if isinstance(baseline, int) and not isinstance(baseline, bool) and baseline > 0:
            tts_overrides["baseline_wpm"] = baseline
        if tts_overrides:
            overrides["tts"] = replace(base.tts, **tts_overrides)

    return replace(base, **overrides) if overrides else base
