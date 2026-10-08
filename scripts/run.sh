#!/usr/bin/env bash
# Audio Recap plugin entry wrapper.
#
# Sets PYTHONPATH so ``audio_recap`` is importable without install,
# then execs ``python3 -m audio_recap <subcommand> [args...]``. All
# subcommand routing and the ``--session-id`` sentinel rewrite live
# in ``audio_recap/__main__.py``.
#
# Argument shape — hybrid: hooks.json and the commands/*.md slash
# bodies pass the plugin's root path as the first positional arg
# (Claude Code substitutes ``${CLAUDE_PLUGIN_ROOT}`` into JSON config
# values; that's the documented contract). When invoked directly for
# local debugging, the leading arg is absent and the wrapper
# self-locates via BASH_SOURCE.
#
# Usage from CC:
#   run.sh "${CLAUDE_PLUGIN_ROOT}" hook
#   run.sh "${CLAUDE_PLUGIN_ROOT}" command --session-id "${CLAUDE_SESSION_ID}" --cwd "$PWD" on
#
# Usage standalone (e.g. piping a fixture into the hook):
#   scripts/run.sh hook
#   scripts/run.sh command --cwd "$PWD" status
# Pass ``--cwd`` when running a command by hand: the wrapper cd's to the
# plugin root before exec (below), so without it the command would treat
# the plugin root as the project directory.
#
# Off macOS the wrapper stops before Python (platform guard below). That
# is the one place it knows subcommand names: ``hook`` and ``session-end``
# exit silently, everything else prints a one-line notice.

set -euo pipefail

# Audio Recap speaks with the macOS ``say`` command; on any other platform
# it stays out of the way. The hooks exit 0 silently, since a hook error
# would show after every turn; anything a person ran (a slash command, a
# by-hand call) gets one plain sentence, so ``/audio-recap:on`` cannot
# switch narration on. Checked before Python, which some platforms cannot
# even start. The first two arguments are checked because the plugin root,
# when given, comes first and need not start with ``/`` (Git Bash passes
# ``C:/...``). A missing or failing ``uname`` counts as "not macOS".
if ! platform="$(uname -s 2>/dev/null)"; then
  platform=""
fi
if [[ "$platform" != "Darwin" ]]; then
  for arg in "${1:-}" "${2:-}"; do
    if [[ "$arg" == "hook" || "$arg" == "session-end" ]]; then
      exit 0
    fi
  done
  echo "Audio Recap works only on macOS."
  exit 0
fi

# A leading absolute path is treated as the plugin root; otherwise we
# self-locate via BASH_SOURCE. The "looks like a Python module" check
# (no leading slash) lets standalone invocations skip the root arg.
if [[ "${1:-}" == /* ]]; then
  PLUGIN_ROOT="$1"
  shift
else
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  PLUGIN_ROOT="$(dirname "${SCRIPT_DIR}")"
fi

export PYTHONPATH="${PLUGIN_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

# ``python3 -m`` puts the process working directory first on sys.path, so a
# stray ``audio_recap/`` directory in the user's CWD (a checkout of this repo
# or a git worktree) would shadow the installed plugin and run stale code.
# cd into the plugin root so the installed package always wins. Production
# never derives its working directory from the process anyway: the Stop hook
# reads ``cwd`` from its stdin payload and the slash commands pass ``--cwd "$PWD"``.
cd "$PLUGIN_ROOT"

# ``python3`` resolves via the user's PATH — on macOS that is the
# system interpreter (/usr/bin/python3) unless they have installed
# another. Audio Recap is stdlib-only and floors at 3.9, so whatever
# python3 the machine has works; the CI matrix (3.9–3.13) guards that.
exec python3 -m audio_recap "$@"
