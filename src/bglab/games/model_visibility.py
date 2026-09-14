"""Structural audit for the complete model-visible Game request surface.

The audit classifies responsibility.  It intentionally does not ban generic
words such as ``id`` because many package-owned object identifiers are real
player choices.  Exact runtime protocol markers are checked only where the
current text surface has no typed representation yet.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
import copy


@dataclass(frozen=True)
class VisibilityViolation:
    path: str
    owner: str
    reason: str


def _normalized(value: object) -> str:
    return "".join(
        character
        for character in str(value).lower()
        if character.isalnum()
    )


_STRUCTURED_OWNERS = {
    # Runtime request identity/control.  Package object ``id`` remains allowed.
    "authority": "runtime",
    "decisionid": "runtime",
    "turngroupid": "runtime",
    "rulesversion": "runtime",
    "snapshotversion": "runtime",
    # Persistence/replay bookkeeping.
    "gameid": "persistence",
    "pid": "persistence",
    "turnid": "persistence",
    "lastcommittedturnid": "persistence",
    "committedactionids": "persistence",
    "deliveryfingerprint": "persistence",
    # Engine/compiler implementation.
    "op": "engine",
    "effectid": "engine",
    "programid": "engine",
    "enginesteps": "engine",
    "transaction": "engine",
    "bindingfingerprint": "engine",
    "canonicalstepsfingerprint": "engine",
    "routequery": "engine",
    "candidateid": "engine",
    "candidatetoken": "engine",
    "semanticsignature": "engine",
    "token": "engine",
    "steps": "engine",
    "binding": "engine",
    "fingerprint": "engine",
    # Evaluator/search/contract telemetry.
    "schemaversion": "evaluator",
    "version": "evaluator",
    "coverage": "evaluator",
    "contractversion": "evaluator",
    "statehash": "evaluator",
    "outcomecoverage": "evaluator",
    "coveragestatus": "evaluator",
    "enumerationcomplete": "evaluator",
    "searchmode": "evaluator",
    "matchedfacts": "evaluator",
    "requestedfacts": "evaluator",
    "frontiercount": "evaluator",
    "workedexamples": "evaluator",
    "ruleid": "evaluator",
    "inputid": "evaluator",
    "formula": "evaluator",
    "dependencies": "evaluator",
    "scoringstructurechanges": "evaluator",
    "scoringenginechanges": "evaluator",
    "objecttype": "runtime",
    "owner": "runtime",
}

_NON_PLAYER_KEY_PARTS = (
    ("deliveryfingerprint", "persistence"),
    ("canonicalstepsfingerprint", "engine"),
    ("bindingfingerprint", "engine"),
    ("enginesteps", "engine"),
    ("candidatetoken", "engine"),
    ("candidateid", "engine"),
    ("semanticsignature", "engine"),
    ("effectid", "engine"),
    ("programid", "engine"),
    ("routequery", "engine"),
    ("decisionid", "runtime"),
    ("statehash", "evaluator"),
)


def classify_non_player_field(key: object) -> str | None:
    """Return the non-player owner for one structured field, if any."""

    normalized = _normalized(key)
    exact = _STRUCTURED_OWNERS.get(normalized)
    if exact is not None:
        return exact
    return next(
        (
            owner
            for marker, owner in _NON_PLAYER_KEY_PARTS
            if marker in normalized
        ),
        None,
    )


_TEXT_MARKERS = (
    ("rules_version=", "runtime"),
    ("ACTION_EXECUTED:", "runtime"),
    ("typed relink", "runtime"),
    ("semantic-v2 identity/surface", "engine"),
    ("commit fence", "persistence"),
    ("authority worker", "engine"),
    ("committed_action_ids", "persistence"),
    ("last_committed_turn_id", "persistence"),
    ("Authoritative game state after compaction", "runtime"),
)


def _audit_node(value: Any, *, path: str) -> list[VisibilityViolation]:
    violations: list[VisibilityViolation] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            owner = classify_non_player_field(key)
            if owner is not None:
                violations.append(VisibilityViolation(
                    path=child_path,
                    owner=owner,
                    reason=f"{key} is owned by {owner}, not the Player request",
                ))
                # The container itself is the responsibility violation.  Do
                # not multiply one root cause into every descendant field.
                continue
            violations.extend(_audit_node(child, path=child_path))
        return violations
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            violations.extend(_audit_node(child, path=f"{path}[{index}]"))
        return violations
    if isinstance(value, str):
        for marker, owner in _TEXT_MARKERS:
            if marker in value:
                violations.append(VisibilityViolation(
                    path=path,
                    owner=owner,
                    reason=f"model-visible text contains internal marker {marker!r}",
                ))
    return violations


def audit_model_visible_surface(
    surface: Mapping[str, Any],
) -> tuple[VisibilityViolation, ...]:
    """Return responsibility violations from one assembled Provider surface."""

    if not isinstance(surface, Mapping):
        raise TypeError("model-visible surface must be a mapping")
    return tuple(_audit_node(surface, path="$"))


def tool_property_names(surface: Mapping[str, Any]) -> set[str]:
    """Collect actual Provider Tool parameter names from an assembled surface."""

    tools = surface.get("tools", surface.get("toolDefinitions", ()))
    if not isinstance(tools, Sequence) or isinstance(tools, (str, bytes, bytearray)):
        return set()
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        parameters = tool.get("parameters")
        if not isinstance(parameters, Mapping):
            continue
        properties = parameters.get("properties")
        if isinstance(properties, Mapping):
            names.update(str(name) for name in properties)
    return names


def strip_non_player_owned_fields(value: Any) -> Any:
    """Copy a structured value while omitting fields owned by other layers."""

    if isinstance(value, Mapping):
        return {
            str(key): strip_non_player_owned_fields(child)
            for key, child in value.items()
            if classify_non_player_field(key) is None
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [strip_non_player_owned_fields(child) for child in value]
    return copy.deepcopy(value)


__all__ = [
    "VisibilityViolation",
    "audit_model_visible_surface",
    "classify_non_player_field",
    "strip_non_player_owned_fields",
    "tool_property_names",
]
