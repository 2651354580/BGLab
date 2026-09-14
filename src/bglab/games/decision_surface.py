"""Game-independent validation and rendering for model decision information."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
from typing import Any


DECISION_SURFACE_SCHEMA_VERSION = 1

_SECTION_SPECS = (
    ("decision", "# Current decision"),
    ("turnAndPhase", "# Turn and phase"),
    ("yourState", "# Your state"),
    ("opponents", "# Public opponents"),
    ("publicBoard", "# Public board"),
    ("visibleOpportunities", "# Visible opportunities"),
    ("currentActions", "# Current action entries"),
    ("pendingEffects", "# Pending effects"),
    ("scoringRules", "# Official scoring"),
    ("informationBoundaries", "# Information boundaries"),
)
_REQUIRED_KEYS = (
    "schemaVersion",
    "decision",
    "turnAndPhase",
    "actor",
    *tuple(key for key, _heading in _SECTION_SPECS[2:]),
)
_MAX_RENDERED_BYTES = 64 * 1024
_MAX_MODEL_FACTS_BYTES = 48 * 1024
_DECISION_FRAME_KEYS = (
    "schemaVersion",
    "frameType",
    "snapshot",
    "decision",
    "actor",
    "yourState",
    "opponents",
    "publicBoard",
    "visibleOpportunities",
    "currentActions",
    "pendingEffects",
    "scoringRules",
    "informationBoundaries",
)
_DECISION_FRAME_SECTIONS = (
    ("snapshot", "# Snapshot before action"),
    ("decision", "# Current decision"),
    ("yourState", "# Your state and score"),
    ("opponents", "# Public opponents"),
    ("publicBoard", "# Current public objects"),
    ("currentActions", "# Current legal action entries"),
    ("visibleOpportunities", "# Visible opportunities"),
    ("pendingEffects", "# Pending effects"),
    ("scoringRules", "# Official scoring references"),
    ("informationBoundaries", "# Unknown until authority execution"),
)
_FORBIDDEN_KEY_FRAGMENTS = (
    "hidden",
    "deckorder",
    "strategyscore",
    "rank",
    "recommendation",
    "bestaction",
    "rawauthoritystate",
)

_RUNTIME_FRAME_SECTIONS = (
    ("publicState", "# Public state"),
    ("seatPrivateState", "# Seat-authorized state"),
    ("currentActions", "# Current semantic actions"),
    ("outcomeCoverage", "# Read-only outcome coverage"),
    ("scoringFacts", "# Current scoring facts"),
    ("informationBoundaries", "# Information boundaries"),
)
_RUNTIME_FRAME_KEYS = frozenset({
    "schemaVersion",
    "authority",
    "decisionId",
    "turnGroupId",
    "actorSeat",
    "modelFacts",
    *(key for key, _heading in _RUNTIME_FRAME_SECTIONS),
})
_RUNTIME_FORBIDDEN_EXACT_KEYS = frozenset({
    "op",
    "effectid",
    "programid",
    "candidateid",
    "candidatetoken",
    "routequery",
    "enginesteps",
    "transaction",
})


def _render_scalar(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _render_item(value: Any, *, indent: int = 0) -> list[str]:
    prefix = "  " * indent
    if isinstance(value, Mapping):
        identity = value.get("id")
        label = value.get("label")
        heading = " — ".join(
            part for part in (
                f"[{identity}]" if identity is not None else "",
                str(label) if label is not None else "",
            )
            if part
        )
        lines = [f"{prefix}- {heading}"] if heading else []
        for key, child in value.items():
            if key in {"id", "label"}:
                continue
            if isinstance(child, (Mapping, list)):
                lines.append(f"{prefix}  {key}:")
                lines.extend(_render_item(child, indent=indent + 2))
            else:
                lines.append(f"{prefix}  {key}: {_render_scalar(child)}")
        return lines or [f"{prefix}- none"]
    if isinstance(value, list):
        if not value:
            return [f"{prefix}- none"]
        lines: list[str] = []
        for child in value:
            if isinstance(child, (Mapping, list)):
                lines.extend(_render_item(child, indent=indent))
            else:
                lines.append(f"{prefix}- {_render_scalar(child)}")
        return lines
    return [f"{prefix}- {_render_scalar(value)}"]


def _reject_forbidden_fields(value: Any, path: str = "observation") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = "".join(character for character in str(key).lower() if character.isalnum())
            if any(fragment in normalized for fragment in _FORBIDDEN_KEY_FRAGMENTS):
                raise ValueError(f"forbidden field at {path}.{key}")
            _reject_forbidden_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        ids = [
            str(child["id"])
            for child in value
            if isinstance(child, Mapping) and child.get("id") is not None
        ]
        duplicates = sorted({identity for identity in ids if ids.count(identity) > 1})
        if duplicates:
            raise ValueError(f"duplicate id: {duplicates[0]}")
        for index, child in enumerate(value):
            _reject_forbidden_fields(child, f"{path}[{index}]")


def validate_authority_observation(observation: Mapping[str, Any]) -> None:
    if not isinstance(observation, Mapping):
        raise ValueError("authority observation must be an object")
    missing = [key for key in _REQUIRED_KEYS if key not in observation]
    if missing:
        raise ValueError(
            "authority observation missing required keys: " + ", ".join(missing)
        )
    unknown = [key for key in observation if key not in _REQUIRED_KEYS]
    if unknown:
        raise ValueError(
            "authority observation has unknown top-level keys: "
            + ", ".join(unknown)
        )
    if observation["schemaVersion"] != DECISION_SURFACE_SCHEMA_VERSION:
        raise ValueError("unsupported authority observation schemaVersion")
    _reject_forbidden_fields(observation)
    try:
        json.dumps(observation, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ValueError("authority observation must be JSON-compatible") from error

    rendered = render_authority_observation(observation, _validated=True)
    if len(rendered.encode("utf-8")) > _MAX_RENDERED_BYTES:
        raise ValueError("rendered authority observation exceeds 64 KiB")


def render_authority_observation(
    observation: Mapping[str, Any],
    *,
    _validated: bool = False,
) -> str:
    if not _validated:
        validate_authority_observation(observation)

    sections: list[str] = []
    for key, heading in _SECTION_SPECS:
        value = observation[key]
        if key == "decision":
            value = {**value, "actor": observation["actor"]}
        sections.append(heading + "\n" + "\n".join(_render_item(value)))
    return "\n\n".join(sections)


def _require_object_metadata(value: Any, path: str) -> None:
    if isinstance(value, Mapping):
        if value.get("id") is not None:
            for field in ("objectType", "owner"):
                if not str(value.get(field, "")).strip():
                    raise ValueError(f"{path}.{field} must be explicit")
        for key, child in value.items():
            _require_object_metadata(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _require_object_metadata(child, f"{path}[{index}]")


def _validate_action_entries(entries: Any) -> None:
    if not isinstance(entries, list) or not entries:
        raise ValueError("currentActions must contain at least one action entry")
    for index, entry in enumerate(entries):
        path = f"currentActions[{index}]"
        if not isinstance(entry, Mapping):
            raise ValueError(f"{path} must be an object")
        if not str(entry.get("statusBeforeAction", "")).strip():
            raise ValueError(f"{path}.statusBeforeAction must be explicit")
        effects = entry.get("effects")
        if not isinstance(effects, list) or not effects:
            raise ValueError(f"{path}.effects must be a non-empty list")
        orders: list[int] = []
        for effect_index, effect in enumerate(effects):
            effect_path = f"{path}.effects[{effect_index}]"
            if not isinstance(effect, Mapping):
                raise ValueError(f"{effect_path} must be an object")
            order = effect.get("order")
            if not isinstance(order, int) or isinstance(order, bool) or order < 1:
                raise ValueError(f"{effect_path}.order must be a positive integer")
            if not isinstance(effect.get("mandatory"), bool):
                raise ValueError(f"{effect_path}.mandatory must be boolean")
            if not str(effect.get("text", "")).strip():
                raise ValueError(f"{effect_path}.text must be non-empty")
            orders.append(order)
        if len(orders) != len(set(orders)):
            raise ValueError(f"{path}.effects orders must be unique")


def _validate_scoring_rules(rules: Any) -> None:
    if not isinstance(rules, list) or not rules:
        raise ValueError("scoringRules must contain at least one rule")
    for index, rule in enumerate(rules):
        path = f"scoringRules[{index}]"
        if not isinstance(rule, Mapping):
            raise ValueError(f"{path} must be an object")
        for field in ("timing", "formula", "currentInputs"):
            if field not in rule:
                raise ValueError(f"{path}.{field} must be explicit")
        if "currentComponentValue" not in rule:
            raise ValueError(
                f"{path}.currentComponentValue must be explicit",
            )


def build_decision_frame(
    observation: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a compact factual frame without adding strategy or inferred facts."""
    validate_authority_observation(observation)
    _require_object_metadata(observation, "observation")
    _validate_action_entries(observation["currentActions"])
    _validate_scoring_rules(observation["scoringRules"])
    snapshot = deepcopy(observation["turnAndPhase"])
    if not str(snapshot.get("snapshotTiming", "")).strip():
        raise ValueError("turnAndPhase.snapshotTiming must be explicit")
    return {
        "schemaVersion": DECISION_SURFACE_SCHEMA_VERSION,
        "frameType": "decision",
        "snapshot": snapshot,
        "decision": deepcopy(observation["decision"]),
        "actor": deepcopy(observation["actor"]),
        "yourState": deepcopy(observation["yourState"]),
        "opponents": deepcopy(observation["opponents"]),
        "publicBoard": deepcopy(observation["publicBoard"]),
        "visibleOpportunities": deepcopy(observation["visibleOpportunities"]),
        "currentActions": deepcopy(observation["currentActions"]),
        "pendingEffects": deepcopy(observation["pendingEffects"]),
        "scoringRules": deepcopy(observation["scoringRules"]),
        "informationBoundaries": deepcopy(
            observation["informationBoundaries"],
        ),
    }


def validate_decision_frame(frame: Mapping[str, Any]) -> None:
    if not isinstance(frame, Mapping):
        raise ValueError("decision frame must be an object")
    missing = [key for key in _DECISION_FRAME_KEYS if key not in frame]
    if missing:
        raise ValueError("decision frame missing required keys: " + ", ".join(missing))
    unknown = [key for key in frame if key not in _DECISION_FRAME_KEYS]
    if unknown:
        raise ValueError(
            "decision frame has unknown top-level keys: " + ", ".join(unknown),
        )
    if frame["schemaVersion"] != DECISION_SURFACE_SCHEMA_VERSION:
        raise ValueError("unsupported decision frame schemaVersion")
    if frame["frameType"] != "decision":
        raise ValueError("decision frameType must be decision")
    if not str(frame["snapshot"].get("snapshotTiming", "")).strip():
        raise ValueError("snapshot.snapshotTiming must be explicit")
    _reject_forbidden_fields(frame, "frame")
    _require_object_metadata(frame, "frame")
    _validate_action_entries(frame["currentActions"])
    _validate_scoring_rules(frame["scoringRules"])
    rendered = render_decision_frame(frame, _validated=True)
    if len(rendered.encode("utf-8")) > _MAX_RENDERED_BYTES:
        raise ValueError("rendered decision frame exceeds 64 KiB")


def render_decision_frame(
    frame: Mapping[str, Any],
    *,
    _validated: bool = False,
) -> str:
    if not _validated:
        validate_decision_frame(frame)
    sections: list[str] = []
    for key, heading in _DECISION_FRAME_SECTIONS:
        value = frame[key]
        if key == "decision":
            value = {**value, "actor": frame["actor"]}
        sections.append(heading + "\n" + "\n".join(_render_item(value)))
    return "\n\n".join(sections)


def render_runtime_decision_frame(frame: Mapping[str, Any]) -> str:
    """Render the sole production, package-owned Player projection."""
    validate_runtime_decision_frame(frame)
    from bglab.games.model_visible_frame import (
        CURRENT_ONLY_EVAL_PROFILE,
        project_model_visible_frame,
        render_model_visible_frame,
    )

    projected = project_model_visible_frame(
        frame,
        engine="",
        profile=CURRENT_ONLY_EVAL_PROFILE,
        package_facts=frame["modelFacts"],
    )
    return render_model_visible_frame(projected)


def render_internal_runtime_frame_for_historical_diagnostics(
    frame: Mapping[str, Any],
) -> str:
    """Render rich authority only for offline historical comparison.

    This function is never a Provider/Tool fallback.  Production callers use
    :func:`render_runtime_decision_frame`, which requires package ``modelFacts``.
    """
    validate_runtime_decision_frame(frame)
    metadata = {
        key: frame.get(key)
        for key in ("authority", "decisionId", "turnGroupId", "actorSeat")
    }
    sections = [
        "# Internal historical decision diagnostic\n"
        + "\n".join(_render_item(metadata)),
    ]
    for key, heading in _RUNTIME_FRAME_SECTIONS:
        sections.append(
            heading + "\n" + "\n".join(_render_item(frame.get(key))),
        )
    return "\n\n".join(sections)


def _reject_runtime_internal_fields(value: Any, path: str = "frame") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = "".join(
                character for character in str(key).lower() if character.isalnum()
            )
            if normalized in _RUNTIME_FORBIDDEN_EXACT_KEYS:
                raise ValueError(f"internal runtime field at {path}.{key}")
            _reject_runtime_internal_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_runtime_internal_fields(child, f"{path}[{index}]")


def validate_runtime_decision_frame(frame: Mapping[str, Any]) -> None:
    if not isinstance(frame, Mapping):
        raise ValueError("runtime decision frame must be an object")
    missing = sorted(_RUNTIME_FRAME_KEYS - set(frame))
    if missing:
        raise ValueError(
            "runtime decision frame missing required keys: " + ", ".join(missing),
        )
    unknown = sorted(set(frame) - _RUNTIME_FRAME_KEYS)
    if unknown:
        raise ValueError(
            "runtime decision frame has unknown keys: " + ", ".join(unknown),
        )
    if frame.get("schemaVersion") != 2:
        raise ValueError("runtime decision frame schemaVersion must be 2")
    if not isinstance(frame.get("actorSeat"), int):
        raise ValueError("runtime decision frame actorSeat must be an integer")
    actions = frame.get("currentActions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("runtime decision frame requires current semantic actions")
    for index, action in enumerate(actions):
        if not isinstance(action, Mapping) or not str(action.get("action", "")).strip():
            raise ValueError(f"currentActions[{index}] requires an action name")
    boundaries = frame.get("informationBoundaries")
    if not isinstance(boundaries, list):
        raise ValueError("runtime informationBoundaries must be a list")
    _reject_runtime_internal_fields(frame)
    try:
        legacy_frame = {
            key: value for key, value in frame.items()
            if key != "modelFacts"
        }
        encoded = json.dumps(
            legacy_frame,
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("runtime decision frame must be JSON-compatible") from exc
    if len(encoded.encode("utf-8")) > _MAX_RENDERED_BYTES:
        raise ValueError("runtime decision frame exceeds 64 KiB")
    model_facts = frame.get("modelFacts")
    if model_facts is not None:
        if not isinstance(model_facts, Mapping):
            raise ValueError("runtime modelFacts must be an object")
        try:
            encoded_facts = json.dumps(
                model_facts,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("runtime modelFacts must be JSON-compatible") from exc
        if len(encoded_facts.encode("utf-8")) > _MAX_MODEL_FACTS_BYTES:
            raise ValueError("runtime modelFacts exceeds 48 KiB")
