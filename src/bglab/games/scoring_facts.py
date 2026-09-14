"""Advice-free validation for game-package scoring decision facts."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
import json
from numbers import Real
from typing import Any

from bglab.games.model_visibility import (
    audit_model_visible_surface,
    classify_non_player_field,
    strip_non_player_owned_fields,
)


CONTRACT_VERSION = "scoring-decision-facts-v2"

_STRATEGY_FIELDS = {
    "best",
    "preferred",
    "priority",
    "rank",
    "recommended",
    "recommendation",
    "shouldchoose",
    "utility",
    "utilityscore",
}


def _number(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)


def _require(condition: bool, path: str, message: str) -> None:
    if not condition:
        raise ValueError(f"{path}: {message}")


def _walk_advice_fields(value: Any, path: str = "scoringDecisionFacts") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = "".join(character for character in str(key).lower() if character.isalnum())
            if normalized in _STRATEGY_FIELDS:
                raise ValueError(f"{path}.{key}: strategy field is forbidden")
            _walk_advice_fields(child, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            _walk_advice_fields(child, f"{path}[{index}]")


def _validate_score(score: Any) -> None:
    _require(isinstance(score, Mapping), "score", "must be an object")
    _require(_number(score.get("currentTotal")), "score.currentTotal", "must be a number")

    components = score.get("components")
    _require(isinstance(components, list), "score.components", "must be an array")
    for index, component in enumerate(components):
        path = f"score.components[{index}]"
        _require(isinstance(component, Mapping), path, "must be an object")
        _require(isinstance(component.get("id"), str) and bool(component["id"]), f"{path}.id", "must be a non-empty string")
        _require(isinstance(component.get("label"), str) and bool(component["label"]), f"{path}.label", "must be a non-empty string")
        _require(_number(component.get("value")), f"{path}.value", "must be a number")

    rules = score.get("rules")
    _require(isinstance(rules, list) and bool(rules), "score.rules", "must be a non-empty array")
    for index, rule in enumerate(rules):
        path = f"score.rules[{index}]"
        _require(isinstance(rule, Mapping), path, "must be an object")
        for field in ("ruleId", "timing", "formula"):
            _require(
                isinstance(rule.get(field), str) and bool(rule[field]),
                f"{path}.{field}",
                "must be a non-empty string",
            )
        for field in ("dependencies", "workedExamples"):
            values = rule.get(field)
            _require(
                isinstance(values, list) and all(isinstance(item, str) for item in values),
                f"{path}.{field}",
                "must be an array of strings",
            )


def _validate_resource_map(value: Any, path: str) -> None:
    _require(isinstance(value, Mapping), path, "must be an object")
    for resource, amount in value.items():
        _require(isinstance(resource, str) and bool(resource), path, "resource keys must be non-empty strings")
        _require(_number(amount), f"{path}.{resource}", "must be a number")


def _validate_targets(targets: Any) -> None:
    _require(isinstance(targets, list), "scoringTargets", "must be an array")
    for index, target in enumerate(targets):
        path = f"scoringTargets[{index}]"
        _require(isinstance(target, Mapping), path, "must be an object")
        for field in ("id", "actionFamily"):
            _require(
                isinstance(target.get(field), str) and bool(target[field]),
                f"{path}.{field}",
                "must be a non-empty string",
            )
        for field in ("rawCost", "effectiveCost", "spendable", "remainingGap"):
            if field in target:
                _validate_resource_map(target[field], f"{path}.{field}")
        gap = target.get("remainingGap")
        if isinstance(gap, Mapping):
            _require(
                all(amount >= 0 for amount in gap.values()),
                f"{path}.remainingGap",
                "amounts must be non-negative",
            )
        if "affordableNow" in target:
            _require(
                isinstance(target["affordableNow"], bool),
                f"{path}.affordableNow",
                "must be a boolean",
            )
        for field in ("printedPoints", "immediateScoreDelta", "endNowScoreDelta"):
            if field in target:
                _require(_number(target[field]), f"{path}.{field}", "must be a number")


def validate_scoring_decision_facts(facts: Any) -> None:
    """Raise ``ValueError`` when package facts violate the shared v1 contract."""
    _require(isinstance(facts, Mapping), "scoringDecisionFacts", "must be an object")
    _walk_advice_fields(facts)
    _require(
        facts.get("contractVersion") == CONTRACT_VERSION,
        "contractVersion",
        f"must equal {CONTRACT_VERSION}",
    )
    _validate_score(facts.get("score"))

    end_condition = facts.get("endCondition")
    _require(isinstance(end_condition, Mapping), "endCondition", "must be an object")
    _require(
        isinstance(end_condition.get("description"), str)
        and bool(end_condition["description"]),
        "endCondition.description",
        "must be a non-empty string",
    )

    if "scoringTargets" in facts:
        _validate_targets(facts["scoringTargets"])
    if "opponents" in facts:
        _require(isinstance(facts["opponents"], list), "opponents", "must be an array")
    if "currentState" in facts:
        _require(
            isinstance(facts["currentState"], Mapping),
            "currentState",
            "must be an object",
        )


def project_scoring_decision_facts(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Return the complete advice-free package scoring bundle for the Player."""
    validate_scoring_decision_facts(facts)
    score = facts["score"]
    projected: dict[str, Any] = {
        "score": {
            "currentTotal": copy.deepcopy(score["currentTotal"]),
            "components": copy.deepcopy(score["components"]),
            # ``formula`` and ``ruleId`` are evaluator-owned field names on
            # the generic surface.  Package scoring facts intentionally map
            # the validated formula to player-facing ``rule`` text instead of
            # weakening the global visibility boundary.
            "rules": [
                {
                    "timing": copy.deepcopy(rule["timing"]),
                    "rule": copy.deepcopy(rule["formula"]),
                }
                for rule in score["rules"]
            ],
        },
        "endCondition": strip_non_player_owned_fields(
            copy.deepcopy(facts["endCondition"]),
        ),
    }
    targets = facts.get("scoringTargets")
    if isinstance(targets, list):
        compact_targets: list[dict[str, Any]] = []
        known = {
            "id", "actionFamily", "rawCost", "effectiveCost", "spendable",
            "remainingGap", "affordableNow", "printedPoints",
            "immediateScoreDelta", "endNowScoreDelta", "publicFollowUpEffects",
            "scoringStructureChanges",
        }
        for raw in targets:
            if not isinstance(raw, Mapping):
                continue
            target: dict[str, Any] = {
                "id": copy.deepcopy(raw.get("id")),
                "action": copy.deepcopy(raw.get("actionFamily")),
                "cost": copy.deepcopy(
                    raw.get("effectiveCost", raw.get("rawCost", {})),
                ),
                "gap": copy.deepcopy(raw.get("remainingGap", {})),
            }
            if raw.get("rawCost") != raw.get("effectiveCost"):
                target["rawCost"] = copy.deepcopy(raw.get("rawCost", {}))
            if "affordableNow" in raw:
                target["affordableNow"] = copy.deepcopy(raw["affordableNow"])
            printed = copy.deepcopy(raw.get("printedPoints", 0))
            immediate = copy.deepcopy(raw.get("immediateScoreDelta", 0))
            end_now = copy.deepcopy(raw.get("endNowScoreDelta", 0))
            if immediate == end_now:
                target["score"] = end_now
            else:
                target["score"] = {
                    "now": immediate,
                    "gameEnd": end_now,
                    **({"printed": printed} if printed not in {0, end_now} else {}),
                }
            effects = raw.get("publicFollowUpEffects")
            if effects:
                target["reward"] = strip_non_player_owned_fields(
                    copy.deepcopy(effects),
                )
            for key, value in raw.items():
                if key in known:
                    continue
                if classify_non_player_field(key) is None:
                    target[str(key)] = strip_non_player_owned_fields(
                        copy.deepcopy(value),
                    )
            compact_targets.append(target)
        projected["scoringTargets"] = compact_targets
    for source, target in (
        ("opponents", "opponents"),
        ("endGameProgress", "endGameProgress"),
        ("nobles", "nobles"),
        ("currentState", "currentState"),
    ):
        if source in facts:
            projected[target] = strip_non_player_owned_fields(
                copy.deepcopy(facts[source]),
            )
    violations = audit_model_visible_surface(projected)
    if violations:
        first = violations[0]
        raise ValueError(
            f"scoringDecisionFacts exposes {first.owner} field at {first.path}",
        )
    return projected


def _resource_text(value: Any) -> str:
    if not isinstance(value, Mapping) or not value:
        return "none"
    return ", ".join(f"{resource}={amount}" for resource, amount in value.items())


def render_scoring_decision_facts(facts: Mapping[str, Any]) -> str:
    """Render validated package facts as an advice-free arithmetic ledger."""
    validate_scoring_decision_facts(facts)
    score = facts["score"]
    lines = [
        "# Official score facts",
        f"- Current official total: {score['currentTotal']}",
        "- Current components: " + ", ".join(
            f"{item['id']}={item['value']}" for item in score["components"]
        ),
        f"- End condition: {facts['endCondition']['description']}",
        "",
        "# Public target arithmetic (facts only; no ranking or recommendation)",
    ]
    targets = facts.get("scoringTargets") or []
    if not targets:
        lines.append("- none")
    for target in targets:
        details = [
            f"id={target['id']}",
            f"action={target['actionFamily']}",
            f"cost={_resource_text(target.get('effectiveCost'))}",
            f"available={_resource_text(target.get('spendable'))}",
            f"gap={_resource_text(target.get('remainingGap'))}",
        ]
        if "affordableNow" in target:
            details.append(
                f"affordableNow={'yes' if target['affordableNow'] else 'no'}",
            )
        for field in (
            "printedPoints",
            "immediateScoreDelta",
            "endNowScoreDelta",
        ):
            if field in target:
                details.append(f"{field}={target[field]}")
        lines.append("- " + "; ".join(details))
        effects = target.get("publicFollowUpEffects")
        if effects:
            lines.append(
                "  publicFollowUpEffects="
                + json.dumps(effects, ensure_ascii=False, separators=(",", ":")),
            )
    lines.extend(["", "# Official scoring formulas"])
    for rule in score["rules"]:
        lines.append(
            f"- {rule['ruleId']} ({rule['timing']}): {rule['formula']}",
        )
    return "\n".join(lines)
