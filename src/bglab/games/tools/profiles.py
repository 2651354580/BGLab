"""Action-tool profile registry shared by all BGLab game sessions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bglab.tools.base import Tool


ToolProfileFactory = Callable[[int], tuple[list[Tool], dict[str, Any]]]
ACTION_PROFILES: dict[str, ToolProfileFactory] = {}


class ActionProfileError(RuntimeError):
    pass


def register_action_profile(name: str, factory: ToolProfileFactory) -> None:
    existing = ACTION_PROFILES.get(name)
    if existing is not None and existing is not factory:
        raise ActionProfileError(f"duplicate action profile: {name}")
    ACTION_PROFILES[name] = factory


def create_profile_tools(
    name: str, seat_index: int,
) -> tuple[list[Tool], dict[str, Any]]:
    try:
        factory = ACTION_PROFILES[name]
    except KeyError as exc:
        raise ActionProfileError(f"unknown action profile: {name}") from exc
    return factory(seat_index)
