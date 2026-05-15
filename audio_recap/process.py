"""Centralized subprocess execution for Audio Recap.

Single ``subprocess.run`` call site for the whole codebase. Recap, Summarizer,
and TTS implementations accept a :class:`ProcessRunner` via constructor; the
default :class:`SubprocessProcessRunner` calls ``subprocess.run`` directly.
Tests inject a fake instead of patching the global ``subprocess`` module.

Error model:

- ``FileNotFoundError`` (binary missing) maps to :class:`ProcessFailed`
  with ``returncode=-1`` so callers can branch on a single exception type
  for "binary missing or exited nonzero." Callers that need to disambiguate
  inspect ``ProcessFailed.returncode``.
- ``subprocess.TimeoutExpired`` propagates unchanged — Recap and Summarizer
  already catch it and translate to their typed ``*Failed`` exception with
  the configured timeout string.
- A successful call (any returncode, including nonzero) returns the
  :class:`subprocess.CompletedProcess` object; the caller decides how to
  interpret nonzero exit codes.
"""

from __future__ import annotations

import subprocess
from typing import Protocol


class ProcessFailed(Exception):
    """A subprocess could not be launched (e.g. binary missing on PATH).

    Wraps :class:`FileNotFoundError`. ``returncode=-1`` is the sentinel
    "never started"; an actual nonzero exit is reflected in
    :class:`subprocess.CompletedProcess.returncode` instead.
    """

    def __init__(self, argv: list[str], message: str) -> None:
        super().__init__(message)
        self.argv = argv
        self.returncode = -1


class ProcessRunner(Protocol):
    """Minimal seam over :func:`subprocess.run`.

    One method, three params. Implementations may capture stdout/stderr,
    apply timeouts, or stub the call entirely (test fakes). Always returns
    a :class:`subprocess.CompletedProcess` with ``capture_output=True`` and
    ``text=True`` semantics so callers don't repeat the boilerplate.
    """

    def run(
        self,
        argv: list[str],
        *,
        input: str | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``argv`` and return the completed process.

        Raises :class:`ProcessFailed` if the binary cannot be launched
        (e.g. missing on PATH). Raises :class:`subprocess.TimeoutExpired`
        if ``timeout`` elapses before the process exits. A successful
        launch always returns the :class:`CompletedProcess`, even if
        the exit code is nonzero — the caller is responsible for that.
        """
        ...


class SubprocessProcessRunner:
    """Production runner — invokes :func:`subprocess.run` directly."""

    def run(
        self,
        argv: list[str],
        *,
        input: str | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                argv,
                input=input,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as e:
            raise ProcessFailed(argv, f"binary not found: {argv[0]}") from e
