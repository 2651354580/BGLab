"""Profile-selected Game Player content assembled by PromptShell."""

from __future__ import annotations

from bglab.engine.prompt_shell import (
    PromptSection,
    PromptShell,
    resolve_prompt_shell_sync,
)
from bglab.games.features import (
    DEFAULT_GAME_PROFILE,
    GameFeatureProfile,
)
from bglab.games.prompt.agent_runtime import agent_runtime_sections
from bglab.games.prompt.core import core_sections
from bglab.games.prompt.persistence import persistence_sections
from bglab.games.prompt.presentation import presentation_sections
from bglab.games.prompt.tools import tool_sections


def _contents_for_profile(
    profile: GameFeatureProfile,
    action_protocol: str = "semantic-v2",
    enabled_tool_names: frozenset[str] | None = None,
) -> tuple[tuple[str, str], ...]:
    contents: list[tuple[str, str]] = []
    for prefix, sections in (
        ("core", core_sections(profile)),
        ("tools", tool_sections(profile, action_protocol, enabled_tool_names)),
        ("runtime", agent_runtime_sections(profile)),
        ("memory", persistence_sections(profile)),
        ("communication", presentation_sections(profile)),
    ):
        contents.extend(
            (f"{prefix}:{index}", content)
            for index, content in enumerate(sections, 1)
        )
    return tuple(contents)


def build_game_prompt_shell(
    profile: GameFeatureProfile,
    *,
    action_protocol: str = "semantic-v2",
    enabled_tool_names: frozenset[str] | None = None,
) -> PromptShell:
    modules = _contents_for_profile(profile, action_protocol, enabled_tool_names)
    return PromptShell(
        static_sections=tuple(
            PromptSection(
                section_id,
                lambda _context, value=content: value,
                required=True,
            )
            for section_id, content in modules
        ),
    )


def build_game_prompt_parts(
    engine_name: str = "",
    *,
    model: str = "",
    profile: GameFeatureProfile = DEFAULT_GAME_PROFILE,
    action_protocol: str = "semantic-v2",
    enabled_tool_names: frozenset[str] | None = None,
):
    del engine_name, model
    shell = build_game_prompt_shell(
        profile,
        action_protocol=action_protocol,
        enabled_tool_names=enabled_tool_names,
    )
    return resolve_prompt_shell_sync(shell, {})


def build_game_system_prompt(
    engine_name: str = "",
    *,
    model: str = "",
    profile: GameFeatureProfile = DEFAULT_GAME_PROFILE,
    action_protocol: str = "semantic-v2",
    enabled_tool_names: frozenset[str] | None = None,
) -> str:
    return build_game_prompt_parts(
        engine_name,
        model=model,
        profile=profile,
        action_protocol=action_protocol,
        enabled_tool_names=enabled_tool_names,
    ).system_prompt


__all__ = [
    "build_game_prompt_parts",
    "build_game_prompt_shell",
    "build_game_system_prompt",
]
