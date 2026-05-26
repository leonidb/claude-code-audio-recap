---
description: "Disable Audio Recap for this Claude Code session."
---

The user invoked the Audio Recap plugin's `/audio-recap:off` command.

Run this bash command **verbatim**, then report its stdout to the user exactly as printed:

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/run.sh ${CLAUDE_PLUGIN_ROOT} command --session-id "${CLAUDE_SESSION_ID}" --cwd "$PWD" off
```

The command persists a single boolean per CC session to `~/.claude/audio-recap/state/<session-id>.json` and prints a one-line confirmation. Do not interpret, rephrase, or add commentary to its output — quote it to the user directly.
