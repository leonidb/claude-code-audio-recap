"""Audio Recap slash-command entrypoint.

Handles three explicit verbs (no toggle):

- ``on`` — enable Audio Recap (persists for this session).
- ``off`` — disable Audio Recap (persists for this session).
- ``status`` — print current state without changing it.

Invalid forms print a short usage to stderr and exit 2 (convention for
argv errors). Successful forms print a single confirmation line to
stdout and exit 0.

State is per-session, keyed on ``session_id`` alone (see
:mod:`audio_recap.state`); ``cwd`` is still passed for logging and API
compatibility but does not affect the state path. The slash-command
markdown bodies (``commands/on.md``, ``commands/off.md``,
``commands/status.md``) pass both as flags before the action verb:

    python -m audio_recap.command --session-id <id> --cwd <path> on|off|status

CC substitutes ``${CLAUDE_SESSION_ID}`` into the markdown body before
Claude reads it; ``$PWD`` is the bash builtin at slash-command runtime.
The runtime sentinel rewrites ``--session-id`` to ``_global`` and warns
to stderr if CC didn't substitute, so this module always sees a
non-empty session_id by the time argv reaches it.
"""

from __future__ import annotations

import argparse
import os
import sys

from audio_recap.services import Services
from audio_recap.state import State

_VERBS = ("on", "off", "status")


def _phrase(enabled: bool) -> str:
    return "enabled" if enabled else "disabled"


def _build_parser() -> argparse.ArgumentParser:
    """argv shape: ``[--session-id <id>] [--cwd <path>] <on|off|status>``.

    ``add_help=False`` keeps the surface strict — there is no ``-h`` for
    a machine-invoked slash command. argparse errors exit 2 with a
    ``usage:`` line on stderr, which ``main`` converts to a return code.
    """

    parser = argparse.ArgumentParser(prog="audio_recap.command", add_help=False)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--cwd", default=None)
    parser.add_argument("verb", choices=_VERBS)
    return parser


def _resolve_session_id(sid: str | None) -> str:
    return sid if sid else "_global"


def _resolve_cwd(cwd: str | None) -> str:
    return cwd if cwd else os.getcwd()


class CommandHandler:
    """Dispatches the three slash-command verbs against a Services graph."""

    def __init__(self, services: Services) -> None:
        self._s = services

    def dispatch(self, verb: str, session_id: str, cwd: str) -> int:
        if verb == "on":
            return self._handle_on(session_id, cwd)
        if verb == "off":
            return self._handle_off(session_id, cwd)
        # ``status`` reads per-cwd config for the default_enabled
        # effective state (so a fresh session in a default-on cwd
        # truthfully reports "enabled" before /audio-recap:on is run).
        return self._handle_status(session_id, cwd, self._s.config.default_enabled)

    def _handle_on(self, session_id: str, cwd: str) -> int:
        self._s.state.save(State(enabled=True), session_id, cwd)
        sys.stdout.write("Audio Recap enabled.\n")
        self._log(session_id, cwd, "on", True)
        return 0

    def _handle_off(self, session_id: str, cwd: str) -> int:
        self._s.state.save(State(enabled=False), session_id, cwd)
        sys.stdout.write("Audio Recap disabled.\n")
        self._log(session_id, cwd, "off", False)
        return 0

    def _handle_status(self, session_id: str, cwd: str, default_enabled: bool) -> int:
        current = self._s.state.load(session_id, cwd, default_enabled=default_enabled)
        sys.stdout.write(f"Audio Recap is {_phrase(current.enabled)}.\n")
        self._log(session_id, cwd, "status", current.enabled)
        return 0

    def _log(self, session_id: str, cwd: str, verb: str, result: bool) -> None:
        self._s.eventlog.event(
            "command",
            session_id=session_id,
            cwd=cwd,
            action="audio_recap",
            verb=verb,
            result=_phrase(result),
        )


def main(argv: list[str] | None = None, *, services: Services | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    try:
        args = _build_parser().parse_args(argv[1:])
    except SystemExit as e:
        # argparse raises SystemExit on a parse error; callers (tests,
        # __main__) expect a return code, not an exception.
        return e.code if isinstance(e.code, int) else 2

    session_id = _resolve_session_id(args.session_id)
    project_cwd = _resolve_cwd(args.cwd)

    if services is None:
        services = Services.from_config(project_cwd, session_id=session_id)
    return CommandHandler(services).dispatch(args.verb, session_id, project_cwd)


if __name__ == "__main__":
    raise SystemExit(main())
