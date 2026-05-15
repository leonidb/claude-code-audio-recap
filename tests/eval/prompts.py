"""Stress-shape prompt list for the Audio Recap eval loop.

Each entry exercises a distinct narration shape — short prose, code
blocks, URL-heavy lists, tool-using multi-step turns, etc. The
runner dispatches them in order to a fresh ``claude`` CLI invocation
each (per-prompt-isolated; no carry-over context) and harvests the
hook's per-fire log entries for quality assessment.

The list is the v1 starter set ported from the 2026-05-04T07-13Z
reference run. Cull or extend freely as recap / summary / speakable
behavior evolves; the test runner does not depend on a specific
length or ordering.

Prompts that touch the filesystem assume the run cwd contains the
Audio Recap repo (the runner's default — see ``runner.py``). Prompts
that scaffold scratch files use ``/tmp/`` so the working tree stays
clean.
"""

from __future__ import annotations

from typing import TypedDict


class Prompt(TypedDict):
    n: int
    shape: str
    prompt: str


PROMPTS: list[Prompt] = [
    {"n": 1, "shape": "short-prose", "prompt": "What does TTS stand for? One sentence."},
    {
        "n": 2,
        "shape": "medium-prose-150w",
        "prompt": (
            "Why would an OSS Claude Code plugin pick Apache 2.0 over MIT? "
            "Answer in roughly 150 words."
        ),
    },
    {
        "n": 3,
        "shape": "long-prose-400w",
        "prompt": (
            "Write a roughly 400-word reflection on why developer tools "
            "should support voice interfaces. Do not use lists or code."
        ),
    },
    {
        "n": 4,
        "shape": "very-long-prose-800w",
        "prompt": (
            "Explain the differences between MCP servers, hooks, and slash "
            "commands in Claude Code. Be thorough — aim for roughly 800 "
            "words. Cover what each is for, when to reach for it, and how "
            "they compose. Plain prose preferred over headings."
        ),
    },
    {
        "n": 5,
        "shape": "code-block",
        "prompt": (
            "Write a Python regex that matches semantic version strings "
            "(e.g. 1.2.3, 10.20.30-rc.1). Show it in a code block and "
            "give a short usage example."
        ),
    },
    {
        "n": 6,
        "shape": "url-heavy",
        "prompt": (
            "List 5 useful URLs for learning Rust, with a one-line "
            "description for each. Just the list, no preamble."
        ),
    },
    {
        "n": 7,
        "shape": "slash-path-heavy",
        "prompt": (
            "Explain piece by piece what `git log --oneline "
            "origin/main..HEAD --grep='fix' --since='2 weeks ago'` does. "
            "Cover each flag and the revision range syntax."
        ),
    },
    {
        "n": 8,
        "shape": "bash-readonly",
        "prompt": (
            "Tell me the line count of each .py file directly under audio_recap/ in this repo."
        ),
    },
    {
        "n": 9,
        "shape": "multi-segment-tools-prose",
        "prompt": (
            "Find the most-recently-modified Python file in this repo, "
            "then explain what it does in a few sentences."
        ),
    },
    {
        "n": 10,
        "shape": "multi-segment-commit-explain",
        "prompt": (
            "Look up the latest commit on this branch and explain what "
            "its diff changes. Mention the files touched and a "
            "one-sentence summary of the intent."
        ),
    },
    {
        "n": 11,
        "shape": "numbers-and-units",
        "prompt": (
            "Compute 137 multiplied by 24, divided by 60. Then express "
            "that result as hours and minutes."
        ),
    },
    {
        "n": 12,
        "shape": "regex-special-chars",
        "prompt": (
            "What does the regex `^\\d{3}-\\d{4}$` match? Give two valid "
            "examples and two invalid examples."
        ),
    },
    {
        "n": 13,
        "shape": "punctuation-heavy-prose",
        "prompt": (
            "Compare hooks and slash commands in Claude Code — pros, "
            "cons, and when you'd reach for each. Use semicolons, "
            "em-dashes, and parentheticals freely if helpful."
        ),
    },
    {
        "n": 14,
        "shape": "coding-sandbox-scaffold",
        "prompt": (
            "In /tmp/audio-recap-test-001/, scaffold a small Python package "
            "called `slugify` with one module containing a function that "
            "converts strings to URL-safe slugs (lowercase, alphanumerics "
            "+ hyphens). Add 3 pytest unit tests. Stay strictly in /tmp "
            "— do not touch this repo, do not create branches, do not commit."
        ),
    },
    {
        "n": 15,
        "shape": "coding-refactor-diff",
        "prompt": (
            "Refactor the slugify function in /tmp/audio-recap-test-001/ to "
            "support Unicode normalization (NFKD, then strip combining "
            "marks) before slugifying. Show me the diff. Keep all changes "
            "in /tmp/audio-recap-test-001/."
        ),
    },
    {
        "n": 16,
        "shape": "error-path-pytest",
        "prompt": (
            "Run `pytest /tmp/audio-recap-test-001/` and tell me what "
            "happened. If anything failed, explain why."
        ),
    },
]


def smoke_prompts(n: int = 3) -> list[Prompt]:
    """Return the first ``n`` prompts for the smoke loop. Default 3."""

    return PROMPTS[:n]
