"""Package-owned required facts for projected Authority outcomes."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator

from .model import SemanticAction, SemanticChain
from .model_contract import ModelActionContract
from .schema import strict_json_equal

if TYPE_CHECKING:
    from .descriptor import SemanticDescriptor


_RULE_FIELDS = frozenset({"when", "required"})
_WHEN_FIELDS = frozenset({"action", "arguments"})
_PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")


def _freeze_json(value: Any, *, path: str) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} JSON object keys must be strings")
            frozen[key] = _freeze_json(item, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            _freeze_json(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"{path} must contain only finite JSON-compatible values")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class OutcomeRequirement:
    action: str
    arguments: Mapping[str, Any]
    required_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.action, str) or not self.action:
            raise ValueError("outcome requirement action must be non-empty text")
        if not isinstance(self.arguments, Mapping):
            raise TypeError("outcome requirement arguments must be a mapping")
        if any(not isinstance(key, str) or not key for key in self.arguments):
            raise ValueError(
                "outcome requirement argument keys must be non-empty text",
            )
        if (
            not isinstance(self.required_paths, (tuple, list))
            or not self.required_paths
            or any(
                not isinstance(path, str) or _PATH_RE.fullmatch(path) is None
                for path in self.required_paths
            )
        ):
            raise ValueError(
                "outcome requirement required_paths must be non-empty dot paths",
            )
        if len(self.required_paths) != len(set(self.required_paths)):
            raise ValueError(
                "outcome requirement required_paths must not contain duplicates",
            )
        frozen_arguments = _freeze_json(
            dict(self.arguments),
            path="outcome requirement arguments",
        )
        assert isinstance(frozen_arguments, Mapping)
        object.__setattr__(self, "arguments", frozen_arguments)
        object.__setattr__(self, "required_paths", tuple(self.required_paths))


def _selector_equal(
    left: OutcomeRequirement,
    right: OutcomeRequirement,
) -> bool:
    return (
        left.action == right.action
        and strict_json_equal(_thaw_json(left.arguments), right.arguments)
    )


def parse_outcome_requirements(
    raw_requirements: Any,
    *,
    model_contract: ModelActionContract,
    mapped_actions: frozenset[str],
    action_roles: Mapping[str, str],
    outcome_projection: tuple[str, ...],
) -> tuple[OutcomeRequirement, ...]:
    if not isinstance(raw_requirements, list):
        raise ValueError("semantic descriptor outcomeRequirements must be an array")
    parsed: list[OutcomeRequirement] = []
    projection = frozenset(outcome_projection)
    for index, raw_rule in enumerate(raw_requirements):
        field = f"semantic descriptor outcomeRequirements[{index}]"
        if not isinstance(raw_rule, dict):
            raise ValueError(f"{field} must be an object")
        unknown = set(raw_rule) - _RULE_FIELDS
        if unknown:
            raise ValueError(
                f"{field} has unknown fields: " + ", ".join(sorted(unknown)),
            )
        if set(raw_rule) != _RULE_FIELDS:
            raise ValueError(f"{field} requires when and required")
        raw_when = raw_rule.get("when")
        if not isinstance(raw_when, dict):
            raise ValueError(f"{field}.when must be an object")
        unknown_when = set(raw_when) - _WHEN_FIELDS
        if unknown_when:
            raise ValueError(
                f"{field}.when has unknown fields: "
                + ", ".join(sorted(unknown_when)),
            )
        if "action" not in raw_when:
            raise ValueError(f"{field}.when requires action")
        action = raw_when.get("action")
        if not isinstance(action, str) or not action:
            raise ValueError(f"{field}.when.action must be non-empty text")
        if action not in model_contract.actions:
            raise ValueError(f"{field} references unknown semantic action: {action}")
        if action not in mapped_actions:
            raise ValueError(f"{field} references unmapped semantic action: {action}")
        if action_roles.get(action, "intent") == "derived":
            raise ValueError(f"{field} cannot reference derived semantic action: {action}")

        arguments = raw_when.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError(f"{field}.when.arguments must be an object")
        action_definition = model_contract.action(action)
        unknown_arguments = set(arguments) - set(action_definition.fields)
        if unknown_arguments:
            raise ValueError(
                f"{field}.when.arguments contains undeclared fields: "
                + ", ".join(sorted(unknown_arguments)),
            )
        for argument, value in arguments.items():
            errors = tuple(
                Draft202012Validator(
                    dict(action_definition.fields[argument]),
                ).iter_errors(value)
            )
            if errors:
                raise ValueError(
                    f"{field}.when.arguments.{argument} is invalid",
                )

        required = raw_rule.get("required")
        if (
            not isinstance(required, list)
            or not required
            or any(not isinstance(path, str) or not path for path in required)
        ):
            raise ValueError(f"{field}.required must be a non-empty text array")
        if len(required) != len(set(required)):
            raise ValueError(f"{field}.required contains duplicate paths")
        not_owned = sorted(set(required) - projection)
        if not_owned:
            raise ValueError(
                f"{field}.required path is not exactly owned by outcomeProjection: "
                + not_owned[0],
            )
        requirement = OutcomeRequirement(
            action=action,
            arguments=arguments,
            required_paths=tuple(required),
        )
        if any(_selector_equal(requirement, prior) for prior in parsed):
            raise ValueError(f"{field} duplicates or ambiguously repeats a prior rule")
        parsed.append(requirement)
    return tuple(parsed)


def requirement_matches(
    requirement: OutcomeRequirement,
    action: SemanticAction,
) -> bool:
    if not isinstance(requirement, OutcomeRequirement):
        raise TypeError("requirement must be an OutcomeRequirement")
    if not isinstance(action, SemanticAction):
        raise TypeError("action must be a SemanticAction")
    if requirement.action != action.action:
        return False
    arguments = action.to_dict()["args"]
    return all(
        key in arguments
        and strict_json_equal(_thaw_json(arguments[key]), expected)
        for key, expected in requirement.arguments.items()
    )


def requirement_paths(
    descriptor: SemanticDescriptor,
    chain: SemanticChain,
) -> frozenset[str]:
    if not isinstance(chain, SemanticChain):
        raise TypeError("outcome requirements require a SemanticChain")
    required: set[str] = set()
    for action in chain.actions:
        for requirement in descriptor.outcome_requirements:
            if requirement_matches(requirement, action):
                required.update(requirement.required_paths)
    return frozenset(required)


def _path_present(outcome: Mapping[str, Any], path: str) -> bool:
    current: Any = outcome
    for key in path.split("."):
        if not isinstance(current, Mapping) or key not in current:
            return False
        current = current[key]
    return True


def validate_checked_route_outcome(
    descriptor: SemanticDescriptor,
    chain: SemanticChain,
    outcome: Mapping[str, Any],
) -> None:
    if not isinstance(outcome, Mapping):
        raise TypeError("projected checked-route outcome must be a mapping")
    missing = tuple(
        path
        for path in sorted(requirement_paths(descriptor, chain))
        if not _path_present(outcome, path)
    )
    if missing:
        raise ValueError(
            "projected checked-route outcome missing required paths: "
            + ", ".join(missing),
        )


__all__ = [
    "OutcomeRequirement",
    "parse_outcome_requirements",
    "requirement_matches",
    "requirement_paths",
    "validate_checked_route_outcome",
]
