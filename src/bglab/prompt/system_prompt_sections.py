""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger("bglab.prompt.sections")

ComputeFn = Callable[[], Awaitable[str | None]]


@dataclass
class SystemPromptSection:
    name: str
    compute: ComputeFn
    cache_break: bool = False   # True = DANGEROUS_uncached (re-compute every turn)
    reason: str = ""            # Required for uncached sections


# ── Cache ──
_cache: dict[tuple[str, str], str | None] = {}


def system_prompt_section(name: str, compute: ComputeFn) -> SystemPromptSection:
    ""
    return SystemPromptSection(name=name, compute=compute, cache_break=False)


def DANGEROUS_uncached_system_prompt_section(
    name: str, compute: ComputeFn, reason: str,
) -> SystemPromptSection:
    ""
    return SystemPromptSection(name=name, compute=compute, cache_break=True, reason=reason)


async def resolve_system_prompt_sections(
    sections: list[SystemPromptSection],
    *,
    cache_namespace: str = "global",
) -> list[str | None]:
    ""
    async def resolve_one(section: SystemPromptSection) -> str | None:
        key = (cache_namespace, section.name)
        if not section.cache_break and key in _cache:
            return _cache[key]
        try:
            val = await section.compute()
            if not section.cache_break:
                _cache[key] = val
            return val
        except Exception:
            mode = "uncached" if section.cache_break else "cached"
            logger.exception(
                "Error computing %s section '%s'",
                mode,
                section.name,
            )
            return None

    return list(await asyncio.gather(*(resolve_one(section) for section in sections)))


def clear_system_prompt_sections(cache_namespace: str | None = None) -> None:
    ""
    global _cache
    if cache_namespace is None:
        _cache.clear()
        logger.debug("Cleared all system prompt section cache")
        return
    namespace = str(cache_namespace)
    _cache = {
        key: value for key, value in _cache.items()
        if key[0] != namespace
    }
    logger.debug("Cleared system prompt cache namespace %s", namespace)
