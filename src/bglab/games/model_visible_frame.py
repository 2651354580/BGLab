"""Generic Player projection for package-owned current-decision facts.

The package owns ``modelFacts``.  Shared runtime validates and renders that
typed projection, but never reconstructs game semantics from a rich authority
Frame or from game-specific prose.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Any

from bglab.games.model_visibility import (
    audit_model_visible_surface,
    strip_non_player_owned_fields,
)
from bglab.games.scoring_facts import project_scoring_decision_facts


class ModelVisibleFrameError(ValueError):
    pass


@dataclass(frozen=True)
class ModelFrameProjectionProfile:
    name: str
    current_only: bool


CURRENT_ONLY_EVAL_PROFILE = ModelFrameProjectionProfile(
    name="current-only-eval-v3",
    current_only=True,
)

CURRENT_ACTIONS_HEADING = (
    "Currently available starting actions "
    "(later choices belong to the same complete decision)"
)


_ALLOWED_FACT_KINDS = frozenset({
    "TurnFact",
    "ResourceSnapshot",
    "CurrentTargetFact",
    "DirectOutcomeFact",
    "DynamicScoreFact",
    "AuthorityBoundaryFact",
    "UnknownInformationFact",
})


def _typed_package_facts(package_facts: Mapping[str, Any]) -> list[dict[str, Any]]:
    if package_facts.get("version") != 1:
        raise ModelVisibleFrameError("package modelFacts version must be 1")
    if package_facts.get("coverage") != "complete-current-decision":
        raise ModelVisibleFrameError(
            "package modelFacts coverage must be complete-current-decision"
        )
    raw_facts = package_facts.get("facts")
    if not isinstance(raw_facts, list) or not raw_facts:
        raise ModelVisibleFrameError("package modelFacts.facts must be non-empty")
    facts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_facts):
        if not isinstance(raw, Mapping):
            raise ModelVisibleFrameError(
                f"package modelFacts.facts[{index}] must be an object"
            )
        kind = str(raw.get("kind", ""))
        fact_id = str(raw.get("id", "")).strip()
        title = str(raw.get("title", "")).strip()
        data = raw.get("data")
        if kind not in _ALLOWED_FACT_KINDS:
            raise ModelVisibleFrameError(
                f"unsupported package model fact kind: {kind}"
            )
        if not fact_id or fact_id in seen:
            raise ModelVisibleFrameError(
                f"duplicate or empty package model fact id: {fact_id}"
            )
        if not title:
            raise ModelVisibleFrameError(
                f"package model fact {fact_id} requires title"
            )
        if not isinstance(data, Mapping):
            raise ModelVisibleFrameError(
                f"package model fact {fact_id}.data must be an object"
            )
        seen.add(fact_id)
        fact = {
            "kind": kind,
            "id": fact_id,
            "title": title,
            "data": copy.deepcopy(dict(data)),
        }
        violations = audit_model_visible_surface(fact)
        if violations:
            first = violations[0]
            raise ModelVisibleFrameError(
                f"package model fact {fact_id} exposes {first.owner} field "
                f"at {first.path}"
            )
        facts.append(fact)
    return facts


def _dynamic_scoring(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return project_scoring_decision_facts(value)
    except ValueError as exc:
        raise ModelVisibleFrameError(
            f"invalid model-visible scoringDecisionFacts: {exc}",
        ) from exc


def _resolve_package_facts(
    frame: Mapping[str, Any],
    package_facts: Mapping[str, Any] | None,
) -> Mapping[str, Any]:
    embedded = frame.get("modelFacts")
    if package_facts is None:
        package_facts = embedded if isinstance(embedded, Mapping) else None
    elif isinstance(embedded, Mapping) and dict(embedded) != dict(package_facts):
        raise ModelVisibleFrameError(
            "package modelFacts argument differs from the authoritative Frame"
        )
    if not isinstance(package_facts, Mapping):
        raise ModelVisibleFrameError(
            "package modelFacts are required for the current DecisionFrame"
        )
    return package_facts


def project_model_visible_frame(
    frame: Mapping[str, Any],
    *,
    engine: str,
    profile: ModelFrameProjectionProfile,
    package_facts: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project only package-owned facts; ``engine`` is evidence metadata."""
    del engine
    if not profile.current_only:
        raise ModelVisibleFrameError(
            f"unsupported projection profile: {profile.name}"
        )
    actor_seat = frame.get("actorSeat")
    if isinstance(actor_seat, bool) or not isinstance(actor_seat, int):
        raise ModelVisibleFrameError("runtime frame requires integer actorSeat")
    facts = _typed_package_facts(_resolve_package_facts(frame, package_facts))
    current_actions = strip_non_player_owned_fields(frame.get("currentActions"))
    if not isinstance(current_actions, list) or not current_actions:
        raise ModelVisibleFrameError(
            "runtime frame requires package currentActions"
        )
    visible: dict[str, Any] = {
        "actorSeat": actor_seat,
        "facts": facts,
        "currentActions": current_actions,
    }
    private = strip_non_player_owned_fields(frame.get("seatPrivateState"))
    if private:
        visible["seatPrivateState"] = private
    package_owns_visible_scoring = any(
        fact.get("kind") == "DynamicScoreFact" for fact in facts
    )
    scoring = (
        None
        if package_owns_visible_scoring
        else _dynamic_scoring(frame.get("scoringFacts"))
    )
    if scoring:
        visible["dynamicScoring"] = scoring
    boundaries = frame.get("informationBoundaries")
    if not isinstance(boundaries, list) or any(
        not isinstance(item, str) for item in boundaries
    ):
        raise ModelVisibleFrameError(
            "runtime informationBoundaries must contain text"
        )
    if boundaries:
        visible["facts"].append({
            "kind": "UnknownInformationFact",
            "id": "information-boundaries",
            "title": "Unknown until authority execution",
            "lines": copy.deepcopy(boundaries),
        })
    violations = audit_model_visible_surface(visible)
    if violations:
        first = violations[0]
        raise ModelVisibleFrameError(
            f"non-player field at {first.path}: {first.owner}"
        )
    try:
        json.dumps(visible, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ModelVisibleFrameError(
            "model-visible Frame must be JSON-compatible"
        ) from exc
    return visible


def render_model_visible_frame(frame: Mapping[str, Any]) -> str:
    lines = [
        "# Current decision",
        f"- actorSeat={frame['actorSeat']}",
    ]
    for fact in frame.get("facts", ()):
        lines.extend(["", f"## {fact['title']} [{fact['kind']}]"])
        if "data" in fact:
            data = fact["data"]
            natural_lines = data.get("lines") if isinstance(data, Mapping) else None
            if (
                isinstance(natural_lines, list)
                and set(data) == {"lines"}
                and all(isinstance(item, str) for item in natural_lines)
            ):
                lines.extend(f"- {item}" for item in natural_lines)
            else:
                lines.append(json.dumps(
                    data,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ))
        else:
            lines.extend(f"- {item}" for item in fact.get("lines", ()))
    for key, heading in (
        ("seatPrivateState", "Seat-authorized state"),
        ("currentActions", CURRENT_ACTIONS_HEADING),
        ("dynamicScoring", "Current scoring facts"),
    ):
        value = frame.get(key)
        if value:
            lines.extend([
                "",
                f"## {heading}",
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ])
    return "\n".join(lines)


__all__ = [
    "CURRENT_ACTIONS_HEADING",
    "CURRENT_ONLY_EVAL_PROFILE",
    "ModelFrameProjectionProfile",
    "ModelVisibleFrameError",
    "project_model_visible_frame",
    "render_model_visible_frame",
]
