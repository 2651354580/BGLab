"""Immutable capability profiles for staged BGLab game validation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GameFeatureProfile:
    """Capabilities exposed to one game player for a match."""

    name: str
    basic_tools: bool = True
    decision_facts: bool = False
    strategy_tools: bool = False
    skills: bool = False
    skill_prefetch: bool = False
    plan: bool = False
    chat: bool = False
    battle_reports: bool = False
    memory_selection: bool = False
    memory_extraction: bool = False
    skill_evolution: bool = False


@dataclass(frozen=True)
class GameAgentProfile:
    """Immutable per-seat experiment identity and optional tool visibility."""

    features: GameFeatureProfile
    enabled_tool_names: frozenset[str] = frozenset()
    skill_bundle_version: str = "none"

    def __post_init__(self) -> None:
        if not isinstance(self.features, GameFeatureProfile):
            raise TypeError("features must be a GameFeatureProfile")
        names = frozenset(str(name).strip() for name in self.enabled_tool_names)
        if "" in names:
            raise ValueError("enabled tool names must be non-empty")
        object.__setattr__(self, "enabled_tool_names", names)
        version = str(self.skill_bundle_version).strip()
        if not version:
            raise ValueError("skill bundle version must be non-empty")
        object.__setattr__(self, "skill_bundle_version", version)


GAME_CORE_PROFILE = GameFeatureProfile(
    name="game-core",
    decision_facts=True,
)
STRATEGY_EVAL_PROFILE = GameFeatureProfile(
    name="strategy-eval",
    decision_facts=True,
    strategy_tools=True,
    skills=True,
    plan=True,
)
FULL_STATIC_EXPERIMENT_PROFILE = GameFeatureProfile(
    name="full-static-experiment",
    decision_facts=True,
    strategy_tools=True,
    skills=True,
    plan=True,
    chat=True,
    battle_reports=True,
)
FULL_CAPABILITY_EXPERIMENT_PROFILE = GameFeatureProfile(
    name="full-capability-experiment",
    decision_facts=True,
    strategy_tools=True,
    skills=True,
    plan=True,
    chat=True,
    battle_reports=True,
    memory_selection=True,
    memory_extraction=True,
    skill_evolution=True,
)
RELEASE_GAME_PROFILE = GameFeatureProfile(
    name="release-semantic-v2",
    decision_facts=True,
)
RELEASE_CHAT_PROFILE = GameFeatureProfile(
    name="release-semantic-v2-chat",
    decision_facts=True,
    chat=True,
)
RELEASE_STATIC_SKILL_PROFILE = GameFeatureProfile(
    name="release-semantic-v2-static",
    decision_facts=True,
    skills=True,
    skill_prefetch=True,
)
RELEASE_ON_DEMAND_SKILL_PROFILE = GameFeatureProfile(
    name="release-semantic-v2-on-demand",
    decision_facts=True,
    skills=True,
)
_PROFILES = {
    profile.name: profile
    for profile in (
        GAME_CORE_PROFILE,
        STRATEGY_EVAL_PROFILE,
        FULL_STATIC_EXPERIMENT_PROFILE,
        FULL_CAPABILITY_EXPERIMENT_PROFILE,
        RELEASE_GAME_PROFILE,
        RELEASE_CHAT_PROFILE,
        RELEASE_STATIC_SKILL_PROFILE,
        RELEASE_ON_DEMAND_SKILL_PROFILE,
    )
}

_LEGACY_PROFILE_ALIASES = {
    "release-semantic-v2-append-only": "release-semantic-v2",
}

GAME_FEATURE_PROFILE_NAMES = tuple((*_PROFILES, *_LEGACY_PROFILE_ALIASES))
DEFAULT_GAME_PROFILE = GAME_CORE_PROFILE


def get_game_feature_profile(name: str) -> GameFeatureProfile:
    """Return a named immutable profile or fail before a match begins."""
    normalized = _LEGACY_PROFILE_ALIASES.get(name, name)
    try:
        return _PROFILES[normalized]
    except KeyError as exc:
        raise ValueError(f"unknown game feature profile: {name}") from exc
