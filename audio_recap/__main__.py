"""Subcommand dispatcher for ``python -m audio_recap``.

The bash shim (``scripts/run.sh``) sets PYTHONPATH and execs
``python3 -m audio_recap <subcommand> [args...]``. This module owns
the subcommand routing and the runtime ``--session-id`` sentinel
rewrite that previously lived in bash.

Subcommands map to the existing module-level ``main()`` entrypoints:

- ``hook``  → :func:`audio_recap.hook.main`
- ``command`` → :func:`audio_recap.command.main`
- ``repeat`` → :func:`audio_recap.repeat.main`
- ``session-end`` → :func:`audio_recap.session_end.main`

``session-end`` was shell (``scripts/session-end.sh``) while a heartbeat was
refreshed before every turn and an interpreter spawn was a per-turn tax. It
fires once per session now, so it routes through here like everything else —
see :mod:`audio_recap.session_end` for what that bought.

The sentinel rewrite handles the rare-but-real case where Claude Code
fails to substitute ``${CLAUDE_SESSION_ID}`` into the slash-command
markdown body. We detect either the literal ``${CLAUDE_SESSION_ID}``
token or an empty string after ``--session-id``, rewrite to
``"_global"``, and emit a loud stderr warning so the regression is
visible. Behavior matches the original bash logic byte-for-byte;
moving it to Python makes it unit-testable without shell-out.
"""

from __future__ import annotations

import sys

_USAGE = (
    "usage: python -m audio_recap {hook|command|repeat|session-end} [args...]\n"
    "       hook        — Stop-hook entry; reads JSON payload on stdin.\n"
    "       command     — slash-command handler (on/off/status verb in args).\n"
    "       repeat      — /audio-recap:repeat handler.\n"
    "       session-end — SessionEnd hook; reads JSON payload on stdin.\n"
)

_SENTINEL_TOKEN = "${CLAUDE_SESSION_ID}"
_SENTINEL_FALLBACK = "_global"
_SENTINEL_WARNING = (
    "[audio-recap] CC did not substitute ${CLAUDE_SESSION_ID}; "
    "falling back to global state. Please file an issue at "
    "https://github.com/leonidb/claude-code-audio-recap/issues\n"
)


def _rewrite_session_sentinel(argv: list[str]) -> tuple[list[str], bool]:
    """Walk ``argv`` and rewrite an empty / unsubstituted ``--session-id``.

    Returns ``(rewritten_argv, warned)``. Caller writes the
    :data:`_SENTINEL_WARNING` to stderr when ``warned`` is True so the
    regression is visible in the CC console.
    """

    rewritten: list[str] = []
    warned = False
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--session-id" and i + 1 < len(argv):
            value = argv[i + 1]
            if value == "" or value == _SENTINEL_TOKEN:
                rewritten.extend(["--session-id", _SENTINEL_FALLBACK])
                warned = True
            else:
                rewritten.extend(["--session-id", value])
            i += 2
            continue
        rewritten.append(token)
        i += 1
    return rewritten, warned


def main(argv: list[str] | None = None) -> int:
    """Dispatch to the named subcommand. Returns its exit code.

    ``argv`` defaults to :data:`sys.argv`. ``argv[0]`` is the script
    name (typically ``__main__.py``); ``argv[1]`` is the subcommand;
    ``argv[2:]`` are the subcommand's own arguments.
    """

    argv = list(argv if argv is not None else sys.argv)
    if len(argv) < 2:
        sys.stderr.write(_USAGE)
        return 2

    sub = argv[1]
    rest, warned = _rewrite_session_sentinel(argv[2:])
    if warned:
        sys.stderr.write(_SENTINEL_WARNING)

    if sub == "hook":
        from audio_recap.hook import main as hook_main

        # The Stop hook reads its JSON payload from stdin and ignores argv.
        # Reading stdin here keeps ``hook.main`` pure-args / unit-testable.
        return hook_main(sys.stdin.buffer.read())

    if sub == "command":
        from audio_recap.command import main as command_main

        return command_main(["audio_recap.command", *rest])

    if sub == "repeat":
        from audio_recap.repeat import main as repeat_main

        return repeat_main(["audio_recap.repeat", *rest])

    if sub == "session-end":
        from audio_recap.session_end import main as session_end_main

        # Same shape as ``hook``: CC's payload arrives on stdin, argv is unused.
        return session_end_main(sys.stdin.buffer.read())

    sys.stderr.write(f"[audio-recap] unknown subcommand: {sub!r}\n")
    sys.stderr.write(_USAGE)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
