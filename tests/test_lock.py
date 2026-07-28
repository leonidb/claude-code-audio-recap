"""Playback-lock + presence-label integration tests for TTSRunner (v2).

Covers Part A (serialise playback: acquire outcomes, fail-open, dry_run skip,
release-on-failure, pre-render-before-lock ordering) and Part B (presence-gated
label: solo stays clean, concurrent gets labeled, naming ladder). Uses the
in-memory fakes so nothing blocks or touches the real ``~/.claude`` tree.
"""

from __future__ import annotations

from dataclasses import replace

from audio_recap.config import Config
from audio_recap.lock import AcquireOutcome
from audio_recap.pipeline import PipelineResult, TTSRunner
from audio_recap.tts import RenderResult, TTSFailed
from tests.fakes import (
    FakeEventLog,
    FakePlaybackLock,
    FakePresenceRegistry,
    FakeTTS,
    build_services,
)


def _result(
    recap_text: str | None = "Edited a file.",
    message_text: str = "Here is the reply.",
) -> PipelineResult:
    return PipelineResult(
        recap_text=recap_text,
        message_text=message_text,
        recap_path="claude_p",
        summary_path="under_threshold_verbatim",
    )


# ---------------------------------------------------------------------------
# Part A: serialise playback
# ---------------------------------------------------------------------------


def test_solo_acquires_lock_and_releases() -> None:
    """Acquire + release happen once each around playback."""
    lock = FakePlaybackLock()
    services = build_services(playback_lock=lock)

    rc = TTSRunner(services).speak(_result(), session_id="sid", cwd="/proj/myapp")

    assert rc == 0
    assert lock.acquire_calls == 1
    assert lock.release_calls == 1


def test_lock_uses_the_120s_timeout_cap() -> None:
    """The generous 120s wait cap is passed to acquire (kept, not lowered)."""
    lock = FakePlaybackLock()
    services = build_services(playback_lock=lock)

    TTSRunner(services).speak(_result(), session_id="sid", cwd="/proj/myapp")

    assert lock.last_timeout_s == 120.0


def test_timeout_fallback_plays_anyway_and_logs_outcome() -> None:
    """A lock timeout plays anyway (fail-open) and logs lock_outcome=timeout."""
    lock = FakePlaybackLock(outcome="timeout")
    tts = FakeTTS()
    log = FakeEventLog()
    services = build_services(playback_lock=lock, tts=tts, eventlog=log)

    rc = TTSRunner(services).speak(_result(), session_id="sid", cwd="/proj/myapp", event="stop")

    assert rc == 0
    assert tts.calls  # audio still played despite the timeout
    assert any(e.get("lock_outcome") == "timeout" for _, e in log.events)


def test_error_outcome_plays_anyway() -> None:
    """A flock error (unsupported FS) plays anyway (fail-open)."""
    lock = FakePlaybackLock(outcome="error")
    tts = FakeTTS()
    services = build_services(playback_lock=lock, tts=tts)

    rc = TTSRunner(services).speak(_result(), session_id="sid", cwd="/proj/myapp")

    assert rc == 0
    assert tts.calls


def test_unavailable_lock_plays_anyway() -> None:
    """NullPlaybackLock (unwritable path) reports 'unavailable' and still plays."""
    lock = FakePlaybackLock(outcome="unavailable")
    tts = FakeTTS()
    log = FakeEventLog()
    services = build_services(playback_lock=lock, tts=tts, eventlog=log)

    rc = TTSRunner(services).speak(_result(), session_id="sid", cwd="/proj/myapp", event="stop")

    assert rc == 0
    assert tts.calls
    assert any(e.get("lock_outcome") == "unavailable" for _, e in log.events)


def test_dry_run_skips_lock_and_playback_entirely() -> None:
    """dry_run: no lock acquire/release, no render, no play."""
    config = replace(Config.default(), dry_run=True)
    lock = FakePlaybackLock()
    tts = FakeTTS()
    services = build_services(config=config, playback_lock=lock, tts=tts)

    rc = TTSRunner(services).speak(_result(), session_id="sid", cwd="/proj/myapp")

    assert rc == 0
    assert lock.acquire_calls == 0
    assert lock.release_calls == 0
    assert tts.calls == []
    assert tts.render_calls == []


def test_lock_released_even_when_tts_fails() -> None:
    """TTSFailed → rc=1, but the lock is still released."""
    lock = FakePlaybackLock()
    tts = FakeTTS(raises=TTSFailed("say not found"))
    services = build_services(playback_lock=lock, tts=tts)

    rc = TTSRunner(services).speak(_result(), session_id="sid", cwd="/proj/myapp")

    assert rc == 1
    assert lock.release_calls == 1


def test_unplayed_segment_is_discarded_when_earlier_play_fails() -> None:
    """A pre-rendered segment never reached (earlier TTSFailed) is discarded, not leaked."""
    lock = FakePlaybackLock()
    tts = FakeTTS(raises=TTSFailed("say + afplay both gone"))
    services = build_services(playback_lock=lock, tts=tts)

    rc = TTSRunner(services).speak(
        _result(recap_text="Edited a file.", message_text="Here is the reply."),
        session_id="sid",
        cwd="/proj/myapp",
    )

    assert rc == 1
    # Both segments were pre-rendered before the lock; the message segment was
    # never played (recap's play raised), so its handle must be discarded.
    assert tts.render_calls == ["Edited a file.", "Here is the reply."]
    assert "Here is the reply." in tts.discarded
    assert lock.release_calls == 1


def test_no_segments_skips_the_lock() -> None:
    """Nothing to say → no lock acquired at all."""
    lock = FakePlaybackLock()
    services = build_services(playback_lock=lock)

    rc = TTSRunner(services).speak(
        _result(recap_text=None, message_text=""), session_id="sid", cwd="/proj/myapp"
    )

    assert rc == 0
    assert lock.acquire_calls == 0


def test_render_happens_before_lock_acquire() -> None:
    """Synthesis (render) runs BEFORE the lock is acquired; playback under it.

    Uses a shared event log threaded through both fakes to assert ordering:
    every render must precede the single acquire, and every play must follow it.
    """
    order: list[str] = []

    class _OrderTTS(FakeTTS):
        def render(self, text: str) -> RenderResult | None:
            order.append("render")
            return super().render(text)

        def play(self, rendered: RenderResult) -> dict[str, object] | None:
            order.append("play")
            return super().play(rendered)  # type: ignore[return-value]

    class _OrderLock(FakePlaybackLock):
        def acquire(self, timeout_s: float = 120.0) -> AcquireOutcome:
            order.append("acquire")
            return super().acquire(timeout_s)

    services = build_services(playback_lock=_OrderLock(), tts=_OrderTTS())
    TTSRunner(services).speak(_result(), session_id="sid", cwd="/proj/myapp")

    assert "acquire" in order
    acquire_idx = order.index("acquire")
    # All renders precede the acquire; all plays follow it.
    assert all(order[i] == "render" for i in range(acquire_idx))
    assert all(order[i] == "play" for i in range(acquire_idx + 1, len(order)))


# ---------------------------------------------------------------------------
# Part B: presence-gated session label
# ---------------------------------------------------------------------------


def test_solo_session_no_label() -> None:
    """0 other live sessions → recap spoken verbatim, no label."""
    services = build_services(presence_registry=FakePresenceRegistry(others=0))
    tts = services.tts
    assert isinstance(tts, FakeTTS)

    TTSRunner(services).speak(
        _result(recap_text="Edited a file."), session_id="sid", cwd="/proj/myapp"
    )

    assert tts.calls[0] == "Edited a file."


def test_concurrent_session_labels_recap_with_cwd_path() -> None:
    """≥1 other live session → label = last two cwd segments, prepended to recap."""
    tts = FakeTTS()
    services = build_services(presence_registry=FakePresenceRegistry(others=1), tts=tts)

    TTSRunner(services).speak(
        _result(recap_text="Edited a file."),
        session_id="sid",
        cwd="/home/user/projects/myapp",
    )

    assert tts.calls[0] == "projects myapp. Edited a file."


def test_label_uses_presence_window_from_config() -> None:
    """active_others is queried with the configured presence window."""
    registry = FakePresenceRegistry(others=1)
    config = replace(Config.default(), presence_window_s=600)
    services = build_services(presence_registry=registry, config=config)

    TTSRunner(services).speak(_result(), session_id="sid", cwd="/proj/myapp")

    assert registry.active_others_calls == [("sid", 600)]


def test_label_uses_word_cap_from_config() -> None:
    """The spoken cue honours ``label_max_words`` — no code change to widen it."""
    tts = FakeTTS()
    services = build_services(
        presence_registry=FakePresenceRegistry(others=1),
        config=replace(Config.default(), label_max_words=2),
        tts=tts,
    )

    TTSRunner(services).speak(
        _result(recap_text="Edited a file."),
        session_id="sid",
        cwd="/proj/myapp",
        custom_title="one two three four five",
    )

    assert tts.calls[0] == "one two. Edited a file."


def test_naming_ladder_custom_title_wins() -> None:
    """Tier 1: the CC ``/rename`` beats an aiTitle present in the same transcript."""
    tts = FakeTTS()
    services = build_services(presence_registry=FakePresenceRegistry(others=1), tts=tts)

    TTSRunner(services).speak(
        _result(recap_text="Edited a file."),
        session_id="sid",
        cwd="/home/user/projects/myapp",
        custom_title='"goals sensei"',  # CC stores the rename WITH its quotes
        session_title="Set up local multi-session",
    )

    assert tts.calls[0] == "goals sensei. Edited a file."


def test_naming_ladder_ai_title_second() -> None:
    """Tier 2: no rename → CC's topic title, at the 5-word cap."""
    tts = FakeTTS()
    services = build_services(presence_registry=FakePresenceRegistry(others=1), tts=tts)

    TTSRunner(services).speak(
        _result(recap_text="Edited a file."),
        session_id="sid",
        cwd="/home/user/projects/myapp",
        custom_title=None,
        session_title="Set up local multi-session",
    )

    assert tts.calls[0] == "Set up local multi-session. Edited a file."


def test_naming_ladder_cwd_path_third() -> None:
    """Tier 3: no rename, no aiTitle yet → two path segments, so sibling dirs differ."""
    tts = FakeTTS()
    services = build_services(presence_registry=FakePresenceRegistry(others=1), tts=tts)

    TTSRunner(services).speak(
        _result(recap_text="Edited a file."),
        session_id="sid",
        cwd="/Users/leo/dev/nbrown-clean/sensei",
        custom_title=None,
        session_title=None,
    )

    assert tts.calls[0] == "nbrown-clean sensei. Edited a file."


def test_naming_ladder_session_id_last_resort() -> None:
    """Short session-id when rename and cwd are both empty."""
    tts = FakeTTS()
    services = build_services(presence_registry=FakePresenceRegistry(others=1), tts=tts)

    TTSRunner(services).speak(
        _result(recap_text="Edited a file."),
        session_id="abcdef012345",
        cwd="",
        custom_title=None,
    )

    assert tts.calls[0] == "abcdef. Edited a file."


def test_label_rides_the_recap_when_there_is_one() -> None:
    """With a recap, the label leads the recap and the message stays untouched."""
    tts = FakeTTS()
    services = build_services(presence_registry=FakePresenceRegistry(others=1), tts=tts)

    TTSRunner(services).speak(
        _result(recap_text="Edited a file.", message_text="Here is the reply."),
        session_id="sid",
        cwd="/home/user/projects/myapp",
    )

    assert tts.calls[0] == "projects myapp. Edited a file."
    assert tts.calls[1] == "Here is the reply."


def test_label_rides_the_message_when_the_recap_was_skipped() -> None:
    """THE REGRESSION: a no-tool-use turn skips the recap — the name must survive.

    The label used to be glued to the recap segment, so any turn without a
    recap (no tool use, or a message too short to earn one) went out
    unattributed — even while another session was live and the narration had
    just waited its turn in the playback queue. Heard in the live audio test.
    """
    tts = FakeTTS()
    services = build_services(presence_registry=FakePresenceRegistry(others=1), tts=tts)

    rc = TTSRunner(services).speak(
        _result(recap_text=None, message_text="Just the message."),
        session_id="sid",
        cwd="/home/user/projects/myapp",
    )

    assert rc == 0
    assert tts.calls == ["projects myapp. Just the message."]


def test_no_label_on_a_message_only_turn_when_solo() -> None:
    """Solo stays clean on the message-only path too."""
    tts = FakeTTS()
    services = build_services(presence_registry=FakePresenceRegistry(others=0), tts=tts)

    TTSRunner(services).speak(
        _result(recap_text=None, message_text="Just the message."),
        session_id="sid",
        cwd="/home/user/projects/myapp",
    )

    assert tts.calls == ["Just the message."]


def test_nothing_to_speak_does_not_log_a_label() -> None:
    """No recap and no message → nothing spoken, so nothing to announce."""
    log = FakeEventLog()
    services = build_services(presence_registry=FakePresenceRegistry(others=1), eventlog=log)

    rc = TTSRunner(services).speak(
        _result(recap_text=None, message_text=""), session_id="sid", cwd="/proj/myapp"
    )

    assert rc == 0
    assert not [e for _, e in log.events if "session_label" in e]


def test_label_logged_with_active_session_count() -> None:
    """When labeled, session_label + active_sessions are logged at INFO."""
    log = FakeEventLog()
    services = build_services(presence_registry=FakePresenceRegistry(others=2), eventlog=log)

    TTSRunner(services).speak(
        _result(recap_text="Edited a file."),
        session_id="sid",
        cwd="/proj/myapp",
        event="stop",
    )

    label_events = [e for _, e in log.events if "session_label" in e]
    assert label_events
    assert label_events[0]["session_label"] == "proj myapp"
    assert label_events[0]["active_sessions"] == 2


def test_solo_does_not_log_session_label() -> None:
    """Solo session: no session_label event emitted."""
    log = FakeEventLog()
    services = build_services(presence_registry=FakePresenceRegistry(others=0), eventlog=log)

    TTSRunner(services).speak(
        _result(recap_text="Edited a file."), session_id="sid", cwd="/proj/myapp", event="stop"
    )

    assert not [e for _, e in log.events if "session_label" in e]
