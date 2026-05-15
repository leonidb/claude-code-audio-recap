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
#   scripts/run.sh command status

set -euo pipefail

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
# ``python3`` resolves via the user's PATH — on macOS that is the
# system interpreter (/usr/bin/python3) unless they have installed
# another. Audio Recap is stdlib-only and floors at 3.9, so whatever
# python3 the machine has works; the CI matrix (3.9–3.13) guards that.
exec python3 -m audio_recap "$@"
