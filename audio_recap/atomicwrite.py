"""Atomic JSON file write — temp file + fsync + rename.

Used by the per-session file store (:mod:`audio_recap.state`): write to a
sibling temp file, fsync it, then ``os.replace`` into place so a concurrent
reader never observes a half-written file. On any failure the temp file is
cleaned up and the exception propagates.

Single source of truth for the durable-write recipe so a future refinement
(e.g. also fsyncing the parent directory) lands in one place.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, data: Any) -> None:
    """Write ``data`` as JSON to ``path`` atomically. Creates parent dirs.

    Raises on I/O failure (after cleaning up the temp file); the caller
    decides what to do.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


__all__ = ["write_json_atomic"]
