from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, TypeAlias


JSONScalar: TypeAlias = str | int | float | bool | None
CanonicalValue: TypeAlias = JSONScalar | tuple[JSONScalar, ...]


class SemanticSchemaVariant(StrEnum):
    FLAT = "flat"
    NESTED = "nested"
    TUPLE = "tuple"


def _freeze_value(value: Any) -> CanonicalValue:
    if isinstance(value, list):
        return tuple(value)
    return value


def _canonical_argument(action: str, key: str, value: Any) -> CanonicalValue:
    del action, key
    return _freeze_value(value)


def _thaw_value(value: CanonicalValue) -> Any:
    if isinstance(value, tuple):
        return list(value)
    return value


@dataclass(frozen=True)
class SemanticAction:
    action: str
    args: tuple[tuple[str, CanonicalValue], ...]

    @classmethod
    def from_mapping(cls, action: str, args: Mapping[str, Any]) -> SemanticAction:
        return cls(
            action=action,
            args=tuple(sorted(
                (key, _canonical_argument(action, key, value))
                for key, value in args.items()
            )),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "args": {key: _thaw_value(value) for key, value in self.args},
        }


@dataclass(frozen=True)
class SemanticChain:
    name: str
    actions: tuple[SemanticAction, ...]


@dataclass(frozen=True)
class SemanticPayload:
    chains: tuple[SemanticChain, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chains": [
                {
                    "name": chain.name,
                    "actions": [action.to_dict() for action in chain.actions],
                }
                for chain in self.chains
            ]
        }
