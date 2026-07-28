"""End-to-end multi-session validation — REAL entrypoints, REAL lock + registry.

Drives the ACTUAL hook entrypoints for several simulated concurrent sessions
against the REAL :class:`audio_recap.lock.FcntlPlaybackLock` and
:class:`audio_recap.presence.FilePresenceRegistry`, all rooted under a shared
tmp dir so the real ``~/.claude`` tree is never touched:

- **Stop** → :func:`audio_recap.hook.main` (the only writer of a heartbeat)
- **SessionEnd** → ``scripts/run.sh <root> session-end`` (a real subprocess, the
  verbatim ``hooks.json`` command, with ``HOME`` pointed at the tmp root)
- **/audio-recap:off** → :func:`audio_recap.command.main`

Asserts the whole flow: playback serialises one-at-a-time under contention, a
heartbeat appears only for an ENABLED session and both SessionEnd and ``off``
retire it, and the recap is labeled only when ≥1 OTHER session holds one
(nobody else → clean).

**Why not literal dry-run?** ``config.dry_run`` skips the lock AND the label by
design — with no playback there is nothing to serialise or announce — so a
dry-run would bypass exactly the coordination under test. Instead every
``say`` / ``afinfo`` / ``afplay`` subprocess is stubbed via a
:class:`FakeProcessRunner`: the real lock / presence / label code runs
end-to-end, but no sound is produced and no human is needed.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from audio_recap import command, hook
from audio_recap.services import Services
from audio_recap.state import FileStateStore, State
from tests.fakes import FakeEventLog, FakeProcessRunner, audio_handlers, completed, raw_payload

RECAP = "Edited a file."
_PLUGIN_ROOT = Path(__file__).parents[1]
_RUN_SH = _PLUGIN_ROOT / "scripts" / "run.sh"


# ---------------------------------------------------------------------------
# helpers — build payloads, runners, and shared-root Services
# ---------------------------------------------------------------------------


def _stop_payload(session_id: str, cwd: str) -> dict[str, Any]:
    """A Stop payload with a tool use and NO assistant text → recap-only narration.

    Tool use present (so ``skip_if_no_tool_use`` doesn't fire) and zero message
    words (so ``skip_if_message_words_lt`` doesn't fire either), giving a clean
    single-segment recap whose spoken text is easy to assert on.
    """

    return {
        "session_id": session_id,
        "cwd": cwd,
        "transcript": [
            {"role": "user", "content": "do the thing"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "/x"}}
                ],
            },
        ],
    }


def _shared_root(home: Path) -> Path:
    """Storage root for a fake ``HOME`` — where the SessionEnd hook deletes.

    The in-process side is told this root explicitly (``audio_recap_root``); the
    SessionEnd subprocess derives it from ``$HOME``. Pointing both at the same
    place is what makes these tests end-to-end rather than two half-systems.
    """

    return home / ".claude" / "audio-recap"


def _session_end(session_id: str, cwd: str, home: Path) -> None:
    """Fire the REAL SessionEnd handler, routed the way CC routes it.

    Runs ``scripts/run.sh <plugin-root> session-end`` as a subprocess with CC's
    event payload on stdin — the exact command string in ``hooks/hooks.json``.
    Going through ``run.sh`` rather than calling
    :func:`audio_recap.session_end.main` keeps the manifest wiring and the
    subcommand name under test, and lets the handler resolve its own storage
    root from ``$HOME`` as it does in production.
    """

    result = subprocess.run(
        [str(_RUN_SH), str(_PLUGIN_ROOT), "session-end"],
        input=json.dumps({"session_id": session_id, "cwd": cwd}),
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
        check=False,
    )
    assert result.returncode == 0, f"session-end failed: {result.stderr}"


class _AfplayTracker:
    """Records the max number of concurrent ``afplay`` calls to detect overlap."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.concurrent = 0
        self.max_concurrent = 0

    def enter(self) -> None:
        with self._lock:
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)

    def leave(self) -> None:
        with self._lock:
            self.concurrent -= 1


def _runner(tracker: _AfplayTracker | None = None) -> FakeProcessRunner:
    """Stub subprocess runner: ``claude`` → RECAP, ``say``/``afinfo`` succeed.

    When ``tracker`` is given, ``afplay`` marks entry/exit and holds briefly so
    a serialisation failure would surface as overlapping playback.
    """

    handlers = audio_handlers()

    def claude(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return completed(argv, stdout=RECAP)

    handlers["claude"] = claude

    if tracker is not None:

        def afplay(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
            tracker.enter()
            try:
                time.sleep(0.05)  # hold the "playback" so overlap would be observable
            finally:
                tracker.leave()
            return completed(argv)

        handlers["afplay"] = afplay

    return FakeProcessRunner(handlers)


def _services(session_id: str, cwd: str, shared_root: Path, runner: FakeProcessRunner) -> Services:
    """A production graph rooted under ``shared_root`` so sessions share lock+registry."""

    return Services.from_config(
        cwd,
        session_id=session_id,
        runner=runner,
        eventlog=FakeEventLog(),  # per-session log avoids concurrent file-append noise
        audio_recap_root=shared_root,
        transcript_root=shared_root / "cc",
    )


def _enable(shared_root: Path, session_id: str, cwd: str) -> None:
    """Enable narration by writing state directly, WITHOUT registering presence.

    Models the ``default_enabled`` route — a session enabled by per-cwd config,
    which never runs a slash command — so tests using this exercise the Stop
    hook's own registration. Use :func:`_on` for the ``/audio-recap:on`` route.
    """

    FileStateStore(shared_root / "state").save(State(enabled=True), session_id, cwd)


def _on(session_id: str, cwd: str, shared_root: Path) -> None:
    """Run the REAL ``/audio-recap:on`` handler against the shared root."""

    argv = ["command.py", "--session-id", session_id, "--cwd", cwd, "on"]
    services = _services(session_id, cwd, shared_root, _runner())
    assert command.main(argv, services=services) == 0


def _off(session_id: str, cwd: str, shared_root: Path) -> None:
    """Run the REAL ``/audio-recap:off`` handler against the shared root."""

    argv = ["command.py", "--session-id", session_id, "--cwd", cwd, "off"]
    services = _services(session_id, cwd, shared_root, _runner())
    assert command.main(argv, services=services) == 0


def _first_say_o_text(runner: FakeProcessRunner) -> str | None:
    """The text handed to the first ``say -o`` render call — the (labeled) recap."""

    for argv, _ in runner.calls:
        if argv[:1] == ["say"] and "-o" in argv:
            return argv[-1]
    return None


def _fire_stop(session_id: str, cwd: str, shared: Path) -> FakeProcessRunner:
    """Run one real Stop fire; return its runner so the caller can read the recap text."""

    runner = _runner()
    payload = raw_payload(_stop_payload(session_id, cwd))
    assert hook.main(payload, services=_services(session_id, cwd, shared, runner)) == 0
    return runner


# ---------------------------------------------------------------------------
# 1. serialise playback under contention
# ---------------------------------------------------------------------------


def test_concurrent_stop_fires_serialize_playback(tmp_path: Path) -> None:
    """Four sessions firing Stop at once never overlap in ``afplay`` (max concurrency 1)."""
    shared = tmp_path
    tracker = _AfplayTracker()
    sessions = [f"sess-{i}" for i in range(4)]
    for sid in sessions:
        _enable(shared, sid, "/proj")

    start = threading.Barrier(len(sessions))

    def fire(sid: str) -> None:
        svc = _services(sid, "/proj", shared, _runner(tracker))
        start.wait()  # all sessions rush the lock together
        assert hook.main(raw_payload(_stop_payload(sid, "/proj")), services=svc) == 0

    threads = [threading.Thread(target=fire, args=(sid,)) for sid in sessions]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not any(t.is_alive() for t in threads), "a Stop fire hung — lock deadlocked"
    assert tracker.max_concurrent == 1, (
        f"playback overlapped across sessions: max_concurrent={tracker.max_concurrent}"
    )


# ---------------------------------------------------------------------------
# 2. heartbeat lifecycle: written only by an enabled narration; two ways out
# ---------------------------------------------------------------------------


def test_heartbeat_lifecycle_across_real_entrypoints(tmp_path: Path) -> None:
    """Only an ENABLED Stop writes it; SessionEnd and ``/audio-recap:off`` retire it.

    Crosses the process boundary on purpose: the heartbeat is written in-process
    by the Stop hook and deleted by a separate SessionEnd subprocess that
    resolves the storage root for itself.
    """
    shared = _shared_root(tmp_path)
    sid, cwd = "sess-A", "/proj"
    hb = shared / "active" / sid

    # Disabled (no state seeded) → the Stop hook returns before registering.
    _fire_stop(sid, cwd, shared)
    assert not hb.exists()

    # Enabled → the narration itself writes the heartbeat.
    _enable(shared, sid, cwd)
    _fire_stop(sid, cwd, shared)
    assert hb.exists()

    # A later narration rewrites it (backdate first to prove the mtime advances).
    stale = hb.stat().st_mtime - 1000
    os.utime(hb, (stale, stale))
    _fire_stop(sid, cwd, shared)
    assert hb.stat().st_mtime > stale

    # SessionEnd (the real shell hook) → delete.
    _session_end(sid, cwd, tmp_path)
    assert not hb.exists()

    # ``/audio-recap:off`` → also retires it, rather than waiting out the window.
    _fire_stop(sid, cwd, shared)
    assert hb.exists()
    _off(sid, cwd, shared)
    assert not hb.exists()


def test_global_sentinel_session_registers_and_retires_like_any_other(tmp_path: Path) -> None:
    """The ``_global`` fallback id is an ordinary heartbeat on every path.

    When Claude Code fails to substitute ``${CLAUDE_SESSION_ID}``, both the Stop
    payload parser and the slash command fall back to the literal ``_global``
    (see :mod:`audio_recap.payload` and :mod:`audio_recap.command`). That id
    reaches the presence layer as a FILENAME, and it is the one id that neither
    looks like a UUID nor is chosen by us at write time — so pin that all three
    operations treat it as they would any other session, across the process
    boundary between the hook that writes it and the one that retires it.
    """

    shared = _shared_root(tmp_path)
    cwd = "/proj"
    hb = shared / "active" / "_global"

    # Drive the REAL sentinel path: a payload with no session_id at all, so the
    # parser (not the test) is what produces ``_global``.
    payload = _stop_payload("", cwd)
    del payload["session_id"]

    def fire_unsubstituted() -> None:
        services = _services("_global", cwd, shared, _runner())
        assert hook.main(raw_payload(payload), services=services) == 0

    # Disabled → no heartbeat, same as any other session.
    fire_unsubstituted()
    assert not hb.exists()

    # Enabled → the narration writes it under the sentinel name.
    _enable(shared, "_global", cwd)
    fire_unsubstituted()
    assert hb.exists()

    # Refresh: a later narration rewrites it rather than duplicating it.
    stale = hb.stat().st_mtime - 1000
    os.utime(hb, (stale, stale))
    fire_unsubstituted()
    assert hb.stat().st_mtime > stale

    # SessionEnd — the shell guard must not reject the leading underscore.
    _session_end("_global", cwd, tmp_path)
    assert not hb.exists()

    # ``/audio-recap:off`` with no --session-id resolves the same sentinel.
    fire_unsubstituted()
    assert hb.exists()
    services = _services("_global", cwd, shared, _runner())
    assert command.main(["command.py", "--cwd", cwd, "off"], services=services) == 0
    assert not hb.exists()


# ---------------------------------------------------------------------------
# 3. label gating: only when another session narrated; two ways to stop counting
# ---------------------------------------------------------------------------


def test_label_fires_only_when_another_session_narrated(tmp_path: Path) -> None:
    """Alone → no label; a second narrating session → labeled; SessionEnd → alone again."""
    shared = _shared_root(tmp_path)
    cwd_a = "/home/u/projects/myapp"
    cwd_b = "/home/u/projects/other"
    _enable(shared, "A", cwd_a)
    _enable(shared, "B", cwd_b)

    # (a) A narrates with nobody else registered → recap spoken verbatim.
    assert _first_say_o_text(_fire_stop("A", cwd_a, shared)) == RECAP

    # (b) B narrates too — the act of speaking is what registers it.
    _fire_stop("B", cwd_b, shared)

    # A fires again → now sees B → recap labeled with A's project path
    # (two segments: a bare basename collides across sibling agent dirs).
    assert _first_say_o_text(_fire_stop("A", cwd_a, shared)) == f"projects myapp. {RECAP}"

    # (c) B ends via SessionEnd → A is the only narrator again → no label.
    _session_end("B", cwd_b, tmp_path)
    assert _first_say_o_text(_fire_stop("A", cwd_a, shared)) == RECAP


def test_a_session_that_never_narrates_never_labels_its_neighbour(tmp_path: Path) -> None:
    """THE LIVE BUG: a recap-disabled session used to count as a live neighbour.

    Presence used to be written by hooks that fired regardless of state, so a
    session with narration switched off — or a headless agent that produces no
    audio and has no listener — made a genuinely alone session announce its
    name. Session B here never runs ``/audio-recap:on``, so its Stop fires must
    leave no trace at all.
    """
    shared = _shared_root(tmp_path)
    cwd_a = "/home/u/projects/myapp"
    cwd_b = "/home/u/projects/other"
    _enable(shared, "A", cwd_a)

    for _ in range(3):  # B works away, silently, throughout
        _fire_stop("B", cwd_b, shared)

    assert not (shared / "active" / "B").exists()
    assert _first_say_o_text(_fire_stop("A", cwd_a, shared)) == RECAP


def test_switching_on_is_what_registers_a_session(tmp_path: Path) -> None:
    """The everyday workflow: open two sessions, ``/audio-recap:on`` in each.

    Registration comes from enabling, so the FIRST narration either session
    makes is already labeled. Under the previous design the heartbeat was
    written by narrating, which meant the first one a user heard was the one
    that went unnamed and only the second onwards was attributed.

    Uses the real ``/audio-recap:on`` entrypoint rather than seeding state, so
    it covers the path an actual user takes.
    """
    shared = _shared_root(tmp_path)
    cwd_a = "/home/u/projects/myapp"
    cwd_b = "/home/u/projects/other"

    _on("A", cwd_a, shared)
    _on("B", cwd_b, shared)

    # Neither has narrated yet, but both can — so A's very first recap is named.
    assert _first_say_o_text(_fire_stop("A", cwd_a, shared)) == f"projects myapp. {RECAP}"


def test_a_long_silence_does_not_stop_a_session_counting(tmp_path: Path) -> None:
    """THE REGRESSION THIS DESIGN EXISTS TO PREVENT: idleness must not un-count.

    Presence briefly meant "narrated within 900s", which broke the case the
    feature is for: leave two sessions open, work in one for half an hour, and
    the other stopped being a neighbour — so the narration you finally hear from
    it arrives unlabeled, at the exact moment you have the least idea which
    session is talking. Being open and enabled is what counts now, so backdating
    A's heartbeat well past that old window must change nothing.
    """
    shared = _shared_root(tmp_path)
    cwd_a = "/home/u/projects/myapp"
    cwd_b = "/home/u/projects/other"
    _enable(shared, "A", cwd_a)
    _enable(shared, "B", cwd_b)

    _fire_stop("A", cwd_a, shared)
    heartbeat = shared / "active" / "A"
    stale = heartbeat.stat().st_mtime - 82_800  # 23h of silence, just inside the horizon
    os.utime(heartbeat, (stale, stale))

    assert _first_say_o_text(_fire_stop("B", cwd_b, shared)) == f"projects other. {RECAP}"
    assert heartbeat.exists()  # and A was not collected as an orphan


def test_switching_narration_off_stops_labeling_the_neighbour(tmp_path: Path) -> None:
    """``/audio-recap:off`` mid-session drops the label at once, not at some horizon."""
    shared = _shared_root(tmp_path)
    cwd_a = "/home/u/projects/myapp"
    cwd_b = "/home/u/projects/other"
    _enable(shared, "A", cwd_a)
    _enable(shared, "B", cwd_b)

    _fire_stop("B", cwd_b, shared)
    assert _first_say_o_text(_fire_stop("A", cwd_a, shared)) == f"projects myapp. {RECAP}"

    _off("B", cwd_b, shared)
    assert _first_say_o_text(_fire_stop("A", cwd_a, shared)) == RECAP
