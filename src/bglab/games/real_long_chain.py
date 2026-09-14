"""Pure classification helpers for authority-issued White Castle programs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import re
from typing import Any


_IGNORED_SIGNATURE_OPS = {
    "begin",
    "draftDie",
    "placeDie",
    "chooseEffectOption",
    "finishResolution",
}
_DOWNSTREAM_BOUNDARY_OPS = {
    "chooseEffectOption",
    "selectCastleTileAction",
    "beginMajorAction",
    "selectMember",
    "selectMajorTarget",
    "selectGarden",
    "selectTrainingYard",
    "selectCourtierDestination",
    "promoteCourtier",
}
_NESTED_ACTION_OPS = {
    "selectCastleTileAction",
    "beginMajorAction",
}


def _steps(program: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = program.get("steps")
    if not isinstance(raw, list):
        return []
    return [step for step in raw if isinstance(step, Mapping)]


def _workspace_family(value: Any) -> str:
    text = str(value or "")
    return re.sub(r"(?:[-_:]?\d+)+$", "", text)


def _scoring_rule_ids(program: Mapping[str, Any]) -> tuple[str, ...]:
    rules: set[str] = set()
    changes = program.get("scoringEngineChanges")
    if isinstance(changes, list):
        for change in changes:
            if isinstance(change, str):
                rules.add(change)
            elif isinstance(change, Mapping):
                value = (
                    change.get("ruleId")
                    or change.get("component")
                    or change.get("type")
                )
                if value:
                    rules.add(str(value))
    return tuple(sorted(rules))


def semantic_signature(program: Mapping[str, Any]) -> tuple:
    """Return a strategy-level signature, ignoring cosmetic choice variants."""
    steps = _steps(program)
    draft = next((step for step in steps if step.get("op") == "draftDie"), {})
    place = next((step for step in steps if step.get("op") == "placeDie"), {})
    major_modes = {
        str(step.get("mode"))
        for step in steps
        if step.get("op") == "beginMajorAction" and step.get("mode")
    }
    secondary_families = {
        str(step.get("op"))
        for step in steps
        if step.get("op") and step.get("op") not in _IGNORED_SIGNATURE_OPS
    }
    return (
        str(draft.get("bridge") or ""),
        str(draft.get("end") or ""),
        _workspace_family(place.get("workspace")),
        tuple(sorted(major_modes)),
        tuple(sorted(secondary_families)),
        _scoring_rule_ids(program),
    )


def is_long_scoring_chain(program: Mapping[str, Any]) -> bool:
    """Return whether a complete program crosses a nested scoring-linked chain."""
    steps = _steps(program)
    ops = [str(step.get("op") or "") for step in steps]
    if "draftDie" not in ops or "placeDie" not in ops:
        return False
    if not program.get("complete"):
        return False
    if not program.get("boundaryReason") and "finishResolution" not in ops:
        return False

    place_index = ops.index("placeDie")
    downstream = {op for op in ops[place_index + 1 :] if op in _DOWNSTREAM_BOUNDARY_OPS}
    if len(downstream) < 2 or not downstream.intersection(_NESTED_ACTION_OPS):
        return False

    score_delta = program.get("immediateScoreDelta", 0)
    end_score_delta = program.get("endNowScoreDelta", 0)
    positive_score = any(
        isinstance(value, (int, float)) and value > 0
        for value in (score_delta, end_score_delta)
    )
    return positive_score or bool(_scoring_rule_ids(program))


def group_representatives(
    programs: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Keep the first complete long-chain representative per semantic signature."""
    representatives: dict[tuple, dict[str, Any]] = {}
    for program in programs:
        if not is_long_scoring_chain(program):
            continue
        signature = semantic_signature(program)
        representatives.setdefault(signature, dict(program))
    return list(representatives.values())

