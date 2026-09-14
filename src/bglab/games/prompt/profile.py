"""Game Prompt adapter for the shared pre-query prompt position."""

from __future__ import annotations

from typing import Any

from bglab.engine.prompt_profiles import PromptBundle, PromptProfile
from bglab.games.prompt.builder import build_game_prompt_parts


def build_game_prompt_bundle(
    *,
    engine: str,
    model: str,
    feature_profile: Any,
    action_protocol: str,
    enabled_tool_names: frozenset[str],
    **_unused: Any,
) -> PromptBundle:
    parts = build_game_prompt_parts(
        engine,
        model=model,
        profile=feature_profile,
        action_protocol=action_protocol,
        enabled_tool_names=enabled_tool_names,
    )
    return PromptBundle(
        system_prompt=parts.system_prompt,
        sections=parts.contents,
        user_context={},
        system_context={},
        section_ids=parts.section_ids,
        cache_boundary_index=parts.cache_boundary_index,
    )


GAME_PROMPT_PROFILE = PromptProfile(
    name="game",
    build=build_game_prompt_bundle,
)


__all__ = ["GAME_PROMPT_PROFILE", "build_game_prompt_bundle"]
