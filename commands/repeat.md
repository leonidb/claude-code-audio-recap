---
description: "Replay the most recent Audio Recap audibly. Hits the per-session cache when available; otherwise generates on demand from the latest assistant turn."
---

The user invoked the Audio Recap plugin's `/audio-recap:repeat` command.

Run this bash command **verbatim**, then report its stdout to the user exactly as printed (it is usually empty — `/audio-recap:repeat` produces audio, not text):

```bash
${CLAUDE_PLUGIN_ROOT}/scripts/run.sh ${CLAUDE_PLUGIN_ROOT} repeat --session-id "${CLAUDE_SESSION_ID}" --cwd "$PWD"
```

`/audio-recap:repeat` is state-independent: it works whether Audio Recap is on or off. On a cache hit (any prior turn this session was narrated) it replays instantly. On a cache miss it generates on demand from the latest assistant turn in CC's session transcript, then caches and speaks the result. Empty transcript → audible "nothing to repeat" cue.

Do not interpret, rephrase, or add commentary to its output — quote it to the user directly.
