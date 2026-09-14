"""Package-owned model-facing semantic action contracts."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator


_ROOT_FIELDS = frozenset({
    "schemaVersion",
    "engine",
    "actions",
    "canonicalization",
    "toolSchema",
})
_ACTION_FIELDS = frozenset({
    "name",
    "description",
    "fields",
    "implicitFields",
    "inheritedFields",
})
_RESERVED_ACTION_FIELDS = frozenset({"action", "args"})


@dataclass(frozen=True)
class ModelActionDefinition:
    name: str
    description: str
    fields: Mapping[str, Mapping[str, Any]]
    implicit_fields: Mapping[str, Any]
    inherited_fields: tuple[str, ...] = ()

    @property
    def argument_order(self) -> tuple[str, ...]:
        return tuple(self.fields)

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return tuple(
            field for field, schema in self.fields.items() if "default" not in schema
        )


@dataclass(frozen=True)
class ModelActionContract:
    engine: str
    actions: Mapping[str, ModelActionDefinition]
    canonicalization: Mapping[str, tuple[str, ...]]
    source_path: Path
    tool_schema: str = "discriminated"

    @property
    def action_names(self) -> tuple[str, ...]:
        return tuple(self.actions)

    def action(self, name: str) -> ModelActionDefinition:
        try:
            return self.actions[name]
        except KeyError as exc:
            raise ValueError(f"unknown model semantic action: {name}") from exc

    def canonicalize_argument(self, action: str, field: str, value: Any) -> Any:
        order = self.canonicalization.get(f"{action}.{field}")
        if order and isinstance(value, (list, tuple)):
            indexes = {item: index for index, item in enumerate(order)}
            return sorted(
                value,
                key=lambda item: (indexes.get(str(item), len(indexes)), str(item)),
            )
        return value


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(
        json.loads(json.dumps(dict(value), ensure_ascii=False)),
    )


def _freeze_json_value(value: Any, *, path: str) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} JSON object keys must be strings")
            frozen[key] = _freeze_json_value(item, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, list):
        return tuple(
            _freeze_json_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"{path} must contain only finite JSON-compatible values")


def _frozen_json_mapping(
    value: Mapping[str, Any],
    *,
    path: str,
) -> Mapping[str, Any]:
    frozen = _freeze_json_value(value, path=path)
    assert isinstance(frozen, Mapping)
    return frozen


def load_model_action_contract(definition: Any) -> ModelActionContract:
    engine = str(getattr(definition, "id", "")).strip()
    root = Path(getattr(definition, "root", "")).resolve()
    declared = getattr(definition, "model_actions_path", None)
    path = Path(declared).resolve() if declared is not None else (
        root / "semantic" / "model-actions.json"
    ).resolve()
    if not path.is_relative_to(root):
        raise ValueError("model action contract path escapes game package")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as exc:
        raise ValueError(f"model action contract JSON is invalid: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise ValueError("model action contract schemaVersion must be 1")
    unknown_root = set(payload) - _ROOT_FIELDS
    if unknown_root:
        raise ValueError(
            "model action contract has unknown fields: "
            + ", ".join(sorted(unknown_root)),
        )
    if payload.get("engine") != engine:
        raise ValueError("model action contract engine must match game definition")
    tool_schema = payload.get("toolSchema", "discriminated")
    if tool_schema not in {"discriminated", "compact-flat"}:
        raise ValueError(
            "model action contract toolSchema must be discriminated or compact-flat"
        )
    raw_actions = payload.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise ValueError("model action contract actions must be a non-empty array")
    actions: dict[str, ModelActionDefinition] = {}
    for index, raw in enumerate(raw_actions):
        if not isinstance(raw, dict):
            raise ValueError(f"model action contract actions[{index}] must be an object")
        unknown_action = set(raw) - _ACTION_FIELDS
        if unknown_action:
            raise ValueError(
                f"model action contract actions[{index}] has unknown fields: "
                + ", ".join(sorted(unknown_action)),
            )
        name = raw.get("name")
        description = raw.get("description", "")
        fields = raw.get("fields")
        implicit_fields = raw.get("implicitFields", {})
        inherited_fields = raw.get("inheritedFields", [])
        if (
            not isinstance(name, str)
            or not name
            or name in actions
            or not isinstance(description, str)
            or not description
            or not isinstance(fields, dict)
            or not isinstance(implicit_fields, dict)
            or not isinstance(inherited_fields, list)
        ):
            raise ValueError(f"model action contract actions[{index}] is malformed")
        for field, schema in fields.items():
            if (
                not isinstance(field, str)
                or not field
                or not isinstance(schema, dict)
                or "type" not in schema
            ):
                raise ValueError(
                    f"model action contract {name}.{field} is malformed",
                )
            try:
                Draft202012Validator.check_schema(schema)
            except Exception as exc:
                raise ValueError(
                    f"model action contract {name}.{field} field schema is invalid",
                ) from exc
            if "default" in schema and not Draft202012Validator(schema).is_valid(
                schema["default"],
            ):
                raise ValueError(
                    f"model action contract {name}.{field} default violates its field schema",
                )
        if any(
            not isinstance(field, str) or not field
            for field in implicit_fields
        ):
            raise ValueError(
                f"model action contract {name}.implicitFields is malformed",
            )
        reserved = set(implicit_fields) & _RESERVED_ACTION_FIELDS
        if reserved:
            raise ValueError(
                f"model action contract {name}.implicitFields uses reserved fields: "
                + ", ".join(sorted(reserved)),
            )
        overlap = set(fields) & set(implicit_fields)
        if overlap:
            raise ValueError(
                f"model action contract {name} fields overlap implicitFields: "
                + ", ".join(sorted(overlap)),
            )
        if (
            any(not isinstance(field, str) or not field for field in inherited_fields)
            or len(inherited_fields) != len(set(inherited_fields))
        ):
            raise ValueError(
                f"model action contract {name}.inheritedFields is malformed",
            )
        inherited_overlap = (
            set(inherited_fields) & (set(fields) | set(implicit_fields))
        )
        if inherited_overlap:
            raise ValueError(
                f"model action contract {name} inheritedFields overlap declared fields: "
                + ", ".join(sorted(inherited_overlap)),
            )
        frozen_implicit_fields = _frozen_json_mapping(
            implicit_fields,
            path=f"model action contract {name}.implicitFields",
        )
        actions[name] = ModelActionDefinition(
            name=name,
            description=description,
            fields=_frozen_mapping(fields),
            implicit_fields=frozen_implicit_fields,
            inherited_fields=tuple(inherited_fields),
        )
    declared_fields = {
        field
        for definition in actions.values()
        for field in definition.fields
    }
    unknown_inherited = sorted({
        field
        for definition in actions.values()
        for field in definition.inherited_fields
        if field not in declared_fields
    })
    if unknown_inherited:
        raise ValueError(
            "model action contract inheritedFields reference unknown field: "
            + unknown_inherited[0],
        )
    raw_canonicalization = payload.get("canonicalization", {})
    if not isinstance(raw_canonicalization, dict):
        raise ValueError("model action contract canonicalization must be an object")
    canonicalization: dict[str, tuple[str, ...]] = {}
    for key, value in raw_canonicalization.items():
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)
            or len(value) != len(set(value))
        ):
            raise ValueError("model action canonicalization is malformed")
        action_name, separator, field_name = key.partition(".")
        if not separator or action_name not in actions or field_name not in actions[action_name].fields:
            raise ValueError(
                f"model action canonicalization references unknown action field: {key}",
            )
        field_schema = actions[action_name].fields[field_name]
        item_schema = field_schema.get("items")
        allowed = item_schema.get("enum") if isinstance(item_schema, Mapping) else None
        if (
            field_schema.get("type") != "array"
            or not isinstance(allowed, list)
            or set(value) != set(allowed)
        ):
            raise ValueError(
                f"model action canonicalization requires a complete enum array field: {key}",
            )
        canonicalization[key] = tuple(value)
    return ModelActionContract(
        engine=engine,
        actions=MappingProxyType(actions),
        canonicalization=MappingProxyType(canonicalization),
        source_path=path,
        tool_schema=tool_schema,
    )


__all__ = [
    "ModelActionContract",
    "ModelActionDefinition",
    "load_model_action_contract",
]
