""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class BundledSkill:
    name: str
    description: str
    get_prompt: Callable[[str], str]  # (args) -> prompt text
    argument_hint: str = ""
    allowed_tools: list[str] = field(default_factory=list)
    user_invocable: bool = True


_registry: dict[str, BundledSkill] = {}

# File-based skills cache — populated by _collect_skill_prefetch each turn.
# Also used by game mode to register game skills without filesystem scanning.
_FILE_SKILLS_CACHE: dict[str, dict] = {}


def register_bundled_skill(name: str, description: str,
                           get_prompt: Callable[[str], str],
                           argument_hint: str = "",
                           allowed_tools: list[str] | None = None,
                           user_invocable: bool = True) -> None:
    _registry[name] = BundledSkill(
        name=name, description=description,
        get_prompt=get_prompt,
        argument_hint=argument_hint,
        allowed_tools=allowed_tools or [],
        user_invocable=user_invocable,
    )


def get_bundled_skills() -> list[BundledSkill]:
    return list(_registry.values())


def get_skill(name: str) -> BundledSkill | None:
    return _registry.get(name)


def get_file_skill(name: str) -> dict | None:
    """Look up a file-based or game skill from the runtime cache."""
    return _FILE_SKILLS_CACHE.get(name)


def get_all_registered_skills() -> list[dict]:
    """All skills registered (bundled + file cache) — for listing in prompt.

    Returns list of {name, description, when_to_use, ...} dicts.
    """
    result = []
    for name, s in _registry.items():
        result.append({
            "name": name, "description": s.description, "when_to_use": "",
            "_source": "bundled",
        })
    for name, s in _FILE_SKILLS_CACHE.items():
        if name not in _registry and not s.get("disable_model_invocation", False):
            result.append(s)
    return result
