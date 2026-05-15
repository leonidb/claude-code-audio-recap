"""Test setup shared across the suite.

The suite uses pure dependency injection — no ``monkeypatch``, no
autouse redirect fixture. Tests that touch the filesystem construct
their file-backed services with tmp paths (see
:func:`tests.fakes.real_services` and the ``*_root`` helpers) so the
developer's real ``~/.claude`` tree is never touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes import event_log_path


@pytest.fixture
def log_path(tmp_path: Path) -> Path:
    """The tmp log path tests construct their event logs at.

    Matches the path :func:`tests.fakes.real_services` wires into the
    production graph, so a test can inject ``real_services`` and then
    read this file to inspect the emitted log.
    """

    return event_log_path(tmp_path)
