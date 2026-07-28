from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path

import audio_recap
from audio_recap.command import _VERBS
from audio_recap.payload import _OWN_COMMAND_TAGS

_REPO_ROOT = Path(__file__).parents[1]

# The slash commands the plugin ships. CC discovers them by scanning
# ``commands/*.md``, so the directory listing IS the published command surface.
_SHIPPED_COMMANDS = {"on", "off", "status", "repeat"}

# The CC events the plugin subscribes to. ``Stop`` narrates (and records the
# session's presence); ``SessionEnd`` retires that presence. Nothing else.
_SUBSCRIBED_HOOKS = {"Stop", "SessionEnd"}


def test_version_matches_packaging_metadata() -> None:
    assert audio_recap.__version__ == version("claude-code-audio-recap")


def test_plugin_manifest_version_matches() -> None:
    """``plugin.json`` is the live install surface and nothing else guards it.

    A drift here ships a same-number/different-bytes build to users, so the
    manifest version must track ``__version__`` (which the test above ties to
    the packaging metadata). The bump tooling keeps all three in lockstep.
    """

    manifest = json.loads(
        (_REPO_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["version"] == audio_recap.__version__


def test_shipped_slash_commands_are_exactly_the_supported_ones() -> None:
    """``commands/*.md`` is the install surface — a stray file ships a real command.

    Guards the removal of ``/audio-recap:label``: session naming comes from
    Claude Code's own ``/rename``, so the plugin must not publish a competing
    naming command.
    """

    shipped = {p.stem for p in (_REPO_ROOT / "commands").glob("*.md")}
    assert shipped == _SHIPPED_COMMANDS


def test_command_handler_verbs_match_the_shipped_commands() -> None:
    """``repeat`` has its own entrypoint; the rest are ``command.py`` verbs."""

    assert set(_VERBS) == _SHIPPED_COMMANDS - {"repeat"}
    assert "label" not in _VERBS


def test_plugin_subscribes_to_exactly_two_hooks() -> None:
    """Every registration is a callback CC runs in the user's session.

    ``SessionStart`` and ``UserPromptSubmit`` used to be registered to keep a
    presence heartbeat warm; they registered sessions that never narrate and
    cost every prompt a subprocess CC blocks on. Presence is now written by the
    narration itself, so re-adding an event here has to be a deliberate choice.
    """

    hooks = json.loads((_REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert set(hooks["hooks"]) == _SUBSCRIBED_HOOKS


def test_registered_hook_commands_exist() -> None:
    """A typo in a hook command is a silent no-op in production."""

    hooks = json.loads((_REPO_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    for matchers in hooks["hooks"].values():
        for matcher in matchers:
            for entry in matcher["hooks"]:
                script = entry["command"].split()[0].replace("${CLAUDE_PLUGIN_ROOT}/", "")
                assert (_REPO_ROOT / script).is_file(), script


def test_own_command_tags_cover_exactly_the_shipped_commands() -> None:
    """The Stop hook's self-recognition tags must not name a removed command."""

    tagged = {
        tag.removeprefix("<command-name>/audio-recap:").removesuffix("</command-name>")
        for tag in _OWN_COMMAND_TAGS
    }
    assert tagged == _SHIPPED_COMMANDS
