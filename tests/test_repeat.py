"""``/audio-recap:repeat`` slash command — cache-hit, on-demand-generation, cue."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from audio_recap import repeat
from audio_recap.cache import FileNarrationCache
from audio_recap.services import Services
from audio_recap.state import FileStateStore, State, _encode_cwd
from tests.fakes import (
    FakeProcessRunner,
    audio_handlers,
    cache_root,
    real_services,
    state_root,
    transcript_root,
)


@pytest.fixture
def _runner_and_captured() -> tuple[FakeProcessRunner, list[list[str]]]:
    """A FakeProcessRunner wired for /repeat plus the captured say argv list."""

    captured: list[list[str]] = []
    handlers = audio_handlers()

    def say_capture(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.append(list(argv))
        return handlers["say"](argv, **kwargs)

    def claude(argv: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout="Generated recap from transcript.\n",
            stderr="",
        )

    runner = FakeProcessRunner(
        {
            "say": say_capture,
            "afinfo": handlers["afinfo"],
            "afplay": handlers["afplay"],
            "claude": claude,
        }
    )
    return runner, captured


@pytest.fixture
def say_calls(_runner_and_captured: tuple[FakeProcessRunner, list[list[str]]]) -> list[list[str]]:
    """Capture spoken-text-bearing TTS argv via the FakeProcessRunner."""

    return _runner_and_captured[1]


@pytest.fixture
def make_services(
    _runner_and_captured: tuple[FakeProcessRunner, list[list[str]]], tmp_path: Path
) -> Callable[..., Services]:
    """Build a production Services graph wired to the shared runner + tmp roots."""

    runner = _runner_and_captured[0]

    def _build(cwd: str = "/proj") -> Services:
        return real_services(cwd, tmp_path, runner=runner)

    return _build


@pytest.fixture
def cache(tmp_path: Path) -> FileNarrationCache:
    """The narration cache rooted where the injected Services reads/writes."""

    return FileNarrationCache(cache_root(tmp_path))


def _seed_cc_transcript(
    tmp_path: Path, cwd: str, session_id: str, content_blocks: list[dict[str, Any]]
) -> None:
    """Drop a JSONL file at the path /repeat will read for on-demand generation."""

    path = transcript_root(tmp_path) / _encode_cwd(cwd) / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "user", "message": {"role": "user", "content": "do it"}},
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": content_blocks},
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_cache_hit_replays_recap_then_message(
    say_calls: list[list[str]],
    make_services: Callable[..., Services],
    cache: FileNarrationCache,
) -> None:
    cache.write("sid-1", "Edited a file.", "Done — see the diff for details.")
    rc = repeat.main(
        ["repeat", "--session-id", "sid-1", "--cwd", "/proj"], services=make_services()
    )
    assert rc == 0
    assert len(say_calls) == 2
    assert say_calls[0][-1] == "Edited a file."
    assert say_calls[1][-1] == "Done — see the diff for details."


def test_cache_hit_with_recap_none_speaks_message_only(
    say_calls: list[list[str]],
    make_services: Callable[..., Services],
    cache: FileNarrationCache,
) -> None:
    cache.write("sid-2", None, "Just a message; recap was skipped.")
    rc = repeat.main(
        ["repeat", "--session-id", "sid-2", "--cwd", "/proj"], services=make_services()
    )
    assert rc == 0
    assert len(say_calls) == 1
    assert say_calls[0][-1] == "Just a message; recap was skipped."


def test_cache_miss_with_no_transcript_speaks_empty_cue(
    say_calls: list[list[str]],
    make_services: Callable[..., Services],
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = repeat.main(
        ["repeat", "--session-id", "fresh-sid", "--cwd", "/proj"], services=make_services()
    )
    assert rc == 0
    assert len(say_calls) == 1
    assert say_calls[0][-1] == "Nothing to repeat in this session."
    assert "nothing to repeat" in capsys.readouterr().err


def test_cache_miss_with_transcript_runs_on_demand_generation(
    say_calls: list[list[str]],
    make_services: Callable[..., Services],
    cache: FileNarrationCache,
    tmp_path: Path,
) -> None:
    blocks = [
        {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "/x"}},
        {"type": "text", "text": " ".join(["lorem"] * 80)},
    ]
    _seed_cc_transcript(tmp_path, "/proj", "gen-sid", blocks)
    rc = repeat.main(
        ["repeat", "--session-id", "gen-sid", "--cwd", "/proj"], services=make_services()
    )
    assert rc == 0
    assert len(say_calls) == 2
    assert say_calls[0][-1] == "Generated recap from transcript."
    # Cache populated for the next /repeat to hit instantly.
    assert cache.read("gen-sid") is not None


def test_on_demand_generation_caches_so_next_repeat_is_a_cache_hit(
    say_calls: list[list[str]],
    make_services: Callable[..., Services],
    tmp_path: Path,
) -> None:
    blocks = [
        {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "/x"}},
        {"type": "text", "text": "Short answer."},
    ]
    _seed_cc_transcript(tmp_path, "/proj", "warm-sid", blocks)
    repeat.main(["repeat", "--session-id", "warm-sid", "--cwd", "/proj"], services=make_services())
    say_calls.clear()
    repeat.main(["repeat", "--session-id", "warm-sid", "--cwd", "/proj"], services=make_services())
    assert len(say_calls) >= 1


def test_empty_transcript_path_logs_event_repeat_empty_transcript(
    say_calls: list[list[str]],
    make_services: Callable[..., Services],
    log_path: Path,
) -> None:
    repeat.main(["repeat", "--session-id", "empty-sid", "--cwd", "/proj"], services=make_services())
    text = log_path.read_text(encoding="utf-8")
    assert "event=repeat" in text
    assert "path=empty_transcript" in text


def test_on_demand_generation_logs_event_repeat_path(
    say_calls: list[list[str]],
    make_services: Callable[..., Services],
    log_path: Path,
    tmp_path: Path,
) -> None:
    blocks = [
        {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": "/x"}},
        {"type": "text", "text": "Short answer."},
    ]
    _seed_cc_transcript(tmp_path, "/proj", "log-sid", blocks)
    repeat.main(["repeat", "--session-id", "log-sid", "--cwd", "/proj"], services=make_services())
    text = log_path.read_text(encoding="utf-8")
    assert "event=repeat" in text
    assert "path=on_demand_generation" in text
    assert "recap_path=" in text
    assert "summary_path=" in text


def test_state_independence_cache_hit_speaks_even_when_narration_off(
    say_calls: list[list[str]],
    make_services: Callable[..., Services],
    cache: FileNarrationCache,
    tmp_path: Path,
) -> None:
    # /repeat ignores the on/off toggle — it's an explicit user request
    # for sound, not auto-narration. The state file isn't even read.
    FileStateStore(state_root(tmp_path)).save(State(enabled=False), "sid-3", "/proj")
    cache.write("sid-3", "Recap.", "Message.")
    rc = repeat.main(
        ["repeat", "--session-id", "sid-3", "--cwd", "/proj"], services=make_services()
    )
    assert rc == 0
    assert len(say_calls) == 2


def test_cross_session_cache_isolation(
    say_calls: list[list[str]],
    make_services: Callable[..., Services],
    cache: FileNarrationCache,
) -> None:
    # Cache for session-A doesn't leak into a /repeat invoked in session-B.
    cache.write("session-A", "A recap", "A message")
    rc = repeat.main(
        ["repeat", "--session-id", "session-B", "--cwd", "/proj"], services=make_services()
    )
    assert rc == 0
    assert len(say_calls) == 1
    assert say_calls[0][-1] == "Nothing to repeat in this session."
    spoken_text = " ".join(call[-1] for call in say_calls)
    assert "A recap" not in spoken_text
    assert "A message" not in spoken_text


def test_cache_hit_logs_event_repeat_cache_hit(
    say_calls: list[list[str]],
    make_services: Callable[..., Services],
    cache: FileNarrationCache,
    log_path: Path,
) -> None:
    cache.write("sid-log", "recap", "message")
    repeat.main(["repeat", "--session-id", "sid-log", "--cwd", "/proj"], services=make_services())
    text = log_path.read_text(encoding="utf-8")
    assert "event=repeat" in text
    assert "session_id=sid-log" in text
    assert "path=cache_hit" in text


def test_unexpected_positional_arg_prints_usage_and_exits_2(
    say_calls: list[list[str]],
    make_services: Callable[..., Services],
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = repeat.main(
        ["repeat", "--session-id", "sid", "--cwd", "/proj", "extra"], services=make_services()
    )
    assert rc == 2
    assert "usage: repeat" in capsys.readouterr().err


def test_dry_run_config_skips_say_in_repeat_cache_hit(
    tmp_path: Path,
    say_calls: list[list[str]],
    make_services: Callable[..., Services],
    cache: FileNarrationCache,
) -> None:
    """A testing-agent worktree with dry_run=true keeps /repeat silent too."""

    cfg = tmp_path / ".audio-recap" / "config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"dry_run": True}), encoding="utf-8")

    cache.write("dry-sid", "Edited a file.", "A short reply.")
    rc = repeat.main(
        ["repeat", "--session-id", "dry-sid", "--cwd", str(tmp_path)],
        services=make_services(str(tmp_path)),
    )
    assert rc == 0
    assert say_calls == []
