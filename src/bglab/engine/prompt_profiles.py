"""Prompt adapters used at the shared pre-query prompt position."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Callable

from bglab.prompt.context import build_system_context, build_user_context
from bglab.prompt.system_prompt import build_system_prompt_sections


@dataclass(frozen=True)
class PromptBundle:
    system_prompt: str
    sections: tuple[str, ...]
    user_context: dict[str, str]
    system_context: dict[str, str]
    section_ids: tuple[str, ...] = ()
    cache_boundary_index: int | None = None


@dataclass(frozen=True)
class PromptProfile:
    """One complete prompt implementation at the fixed prompt position."""

    name: str
    build: Callable[..., PromptBundle | Any]


async def build_code_prompt_bundle(
    *,
    tools: list[Any],
    cwd: str,
    model: str,
    append_prompt: str = "",
    claude_md_content: str | None = None,
    feature_flags: Any = None,
    permission_mode: str = "default",
    language: str | None = None,
    scratchpad_dir: str | None = None,
    token_budget: int | None = None,
    cache_namespace: str = "global",
    cached_user_context: dict[str, str] | None = None,
    cached_system_context: dict[str, str] | None = None,
    **_unused: Any,
) -> PromptBundle:
    prompt_task = build_system_prompt_sections(
        tools,
        cwd=cwd,
        append_prompt=append_prompt,
        model=model,
        feature_flags=feature_flags,
        permission_mode=permission_mode,
        language=language,
        scratchpad_dir=scratchpad_dir,
        token_budget=token_budget,
        cache_namespace=cache_namespace,
    )
    user_task = (
        asyncio.to_thread(
            build_user_context,
            cwd,
            claude_md_content=claude_md_content,
            memory_enabled=(
                feature_flags is None
                or bool(getattr(feature_flags, "memory_section_enabled", True))
            ),
        )
        if cached_user_context is None
        else asyncio.sleep(0, result=dict(cached_user_context))
    )
    system_task = (
        asyncio.to_thread(build_system_context, cwd)
        if cached_system_context is None
        else asyncio.sleep(0, result=dict(cached_system_context))
    )
    resolved_sections, user_context, system_context = await asyncio.gather(
        prompt_task,
        user_task,
        system_task,
    )
    return PromptBundle(
        system_prompt=resolved_sections.system_prompt,
        sections=resolved_sections.contents,
        user_context=user_context,
        system_context=system_context,
        section_ids=resolved_sections.section_ids,
        cache_boundary_index=resolved_sections.cache_boundary_index,
    )


async def resolve_prompt_bundle(
    profile: PromptProfile,
    **kwargs: Any,
) -> PromptBundle:
    result = profile.build(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, PromptBundle):
        raise TypeError(f"prompt profile {profile.name} returned invalid bundle")
    return result


def resolve_prompt_bundle_sync(
    profile: PromptProfile,
    **kwargs: Any,
) -> PromptBundle:
    result = profile.build(**kwargs)
    if inspect.isawaitable(result):
        raise TypeError(
            f"prompt profile {profile.name} requires async resolution",
        )
    if not isinstance(result, PromptBundle):
        raise TypeError(f"prompt profile {profile.name} returned invalid bundle")
    return result


CODE_PROMPT_PROFILE = PromptProfile(
    name="code",
    build=build_code_prompt_bundle,
)


__all__ = [
    "CODE_PROMPT_PROFILE",
    "PromptBundle",
    "PromptProfile",
    "build_code_prompt_bundle",
    "resolve_prompt_bundle",
    "resolve_prompt_bundle_sync",
]
