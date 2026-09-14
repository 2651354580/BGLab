"""Ordered, Profile-owned system-prompt content Modules."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal


PromptCacheScope = Literal["static", "dynamic"]
PromptSectionBuilder = Callable[[Mapping[str, Any]], str | None | Awaitable[str | None]]


@dataclass(frozen=True)
class PromptSection:
    """One content Module inserted at a fixed PromptShell seam."""

    section_id: str
    build: PromptSectionBuilder
    cache_scope: PromptCacheScope = "static"
    required: bool = False

    def __post_init__(self) -> None:
        if not self.section_id.strip():
            raise ValueError("prompt section id must be non-empty")
        if self.cache_scope not in {"static", "dynamic"}:
            raise ValueError("prompt section cache_scope must be static or dynamic")


@dataclass(frozen=True)
class ResolvedPromptSections:
    section_ids: tuple[str, ...]
    contents: tuple[str, ...]
    cache_boundary_index: int

    @property
    def system_prompt(self) -> str:
        return "\n\n".join(self.contents)


@dataclass(frozen=True)
class PromptShell:
    """Resolve ordered static and dynamic Modules without mode branching."""

    static_sections: tuple[PromptSection, ...]
    dynamic_sections: tuple[PromptSection, ...] = ()

    def __post_init__(self) -> None:
        sections = (*self.static_sections, *self.dynamic_sections)
        ids = [section.section_id for section in sections]
        if len(ids) != len(set(ids)):
            raise ValueError("prompt shell section ids must be unique")
        if any(section.cache_scope != "static" for section in self.static_sections):
            raise ValueError("static prompt shell slots require static sections")
        if any(section.cache_scope != "dynamic" for section in self.dynamic_sections):
            raise ValueError("dynamic prompt shell slots require dynamic sections")

    async def resolve(self, context: Mapping[str, Any]) -> ResolvedPromptSections:
        ids: list[str] = []
        contents: list[str] = []
        for section in (*self.static_sections, *self.dynamic_sections):
            content = section.build(context)
            if inspect.isawaitable(content):
                content = await content
            normalized = str(content or "").strip()
            if not normalized:
                if section.required:
                    raise ValueError(
                        f"required prompt section is empty: {section.section_id}",
                    )
                continue
            ids.append(section.section_id)
            contents.append(normalized)
        static_ids = {
            section.section_id for section in self.static_sections
        }
        boundary = sum(section_id in static_ids for section_id in ids)
        return ResolvedPromptSections(tuple(ids), tuple(contents), boundary)


def resolve_prompt_shell_sync(
    shell: PromptShell,
    context: Mapping[str, Any],
) -> ResolvedPromptSections:
    """Resolve a shell whose selected adapters are intentionally synchronous."""

    ids: list[str] = []
    contents: list[str] = []
    for section in (*shell.static_sections, *shell.dynamic_sections):
        content = section.build(context)
        if inspect.isawaitable(content):
            raise TypeError(
                f"prompt section requires async resolution: {section.section_id}",
            )
        normalized = str(content or "").strip()
        if not normalized:
            if section.required:
                raise ValueError(
                    f"required prompt section is empty: {section.section_id}",
                )
            continue
        ids.append(section.section_id)
        contents.append(normalized)
    static_ids = {section.section_id for section in shell.static_sections}
    boundary = sum(section_id in static_ids for section_id in ids)
    return ResolvedPromptSections(tuple(ids), tuple(contents), boundary)


__all__ = [
    "PromptSection",
    "PromptShell",
    "ResolvedPromptSections",
    "resolve_prompt_shell_sync",
]

