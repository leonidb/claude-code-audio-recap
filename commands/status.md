---
description: "Print Audio Recap's current on/off state for this Claude Code session (no change)."
---

The user invoked the Audio Recap plugin's `/audio-recap:status` command.

Run this bash command **verbatim**, then report its stdout to the user exactly as printed:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/run.sh ${CLAUDE_PLUGIN_ROOT} command --session-id "${CLAUDE_SESSION_ID}" --cwd "$PWD" status
```

The command reads the per-session state file at `~/.claude/audio-recap/projects/<encoded-cwd>/<session-id>.json` (or applies `default_enabled` from the per-cwd `.audio-recap/config.json` if no state file exists) and prints a one-line "Audio Recap is enabled/disabled." confirmation without touching state. Do not interpret, rephrase, or add commentary to its output — quote it to the user directly.
