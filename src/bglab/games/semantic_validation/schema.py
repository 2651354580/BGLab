"""Generic BgAct chain schema assembly from a package model contract."""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator

from .model import SemanticAction, SemanticChain, SemanticPayload, SemanticSchemaVariant
from .model_contract import ModelActionContract


@dataclass(frozen=True, slots=True)
class SemanticInputCanonicalization:
    payload: Any
    events: tuple[Mapping[str, Any], ...] = ()


def strict_json_equal(actual: Any, expected: Any) -> bool:
    """Compare JSON values recursively without Python's numeric coercions."""

    if isinstance(expected, Mapping):
        return (
            type(actual) is dict
            and set(actual) == set(expected)
            and all(
                strict_json_equal(actual[key], expected[key])
                for key in expected
            )
        )
    if isinstance(expected, (list, tuple)):
        return (
            type(actual) is list
            and len(actual) == len(expected)
            and all(
                strict_json_equal(actual_item, expected_item)
                for actual_item, expected_item in zip(actual, expected, strict=True)
            )
        )
    if expected is None:
        return actual is None
    if isinstance(expected, bool):
        return isinstance(actual, bool) and actual is expected
    if isinstance(expected, int):
        return type(actual) is int and actual == expected
    if isinstance(expected, float):
        return (
            type(actual) is float
            and math.isfinite(actual)
            and math.isfinite(expected)
            and actual == expected
        )
    return type(actual) is type(expected) and actual == expected


def _copy_semantic_payload_input(value: Any) -> Any:
    """Copy built-in JSON containers without coercing foreign container types."""

    if type(value) is dict:
        return {
            key: _copy_semantic_payload_input(item)
            for key, item in value.items()
        }
    if type(value) is list:
        return [_copy_semantic_payload_input(item) for item in value]
    if type(value) is tuple:
        return tuple(_copy_semantic_payload_input(item) for item in value)
    return value


def _schema_accepts_integer(schema: Mapping[str, Any]) -> bool:
    declared_type = schema.get("type")
    if declared_type == "integer":
        return True
    if isinstance(declared_type, (list, tuple)) and "integer" in declared_type:
        return True
    return any(
        isinstance(branch, Mapping) and _schema_accepts_integer(branch)
        for keyword in ("oneOf", "anyOf")
        for branch in (
            schema.get(keyword)
            if isinstance(schema.get(keyword), (list, tuple))
            else ()
        )
    )


def canonicalize_semantic_payload_input(
    payload: Any,
    variant: SemanticSchemaVariant | str,
    contract: ModelActionContract,
) -> SemanticInputCanonicalization:
    """Apply exact package-declared normalization without guessing choices."""

    if not isinstance(contract, ModelActionContract):
        raise TypeError("semantic input canonicalization requires a ModelActionContract")
    selected = SemanticSchemaVariant(variant)
    canonical = _copy_semantic_payload_input(payload)
    if selected is SemanticSchemaVariant.TUPLE or type(canonical) is not dict:
        return SemanticInputCanonicalization(payload=canonical)
    raw_chains = canonical.get("chains")
    if type(raw_chains) is not list:
        return SemanticInputCanonicalization(payload=canonical)

    events: list[Mapping[str, Any]] = []
    for chain_index, raw_chain in enumerate(raw_chains):
        if type(raw_chain) is not dict:
            continue
        raw_actions = raw_chain.get("actions")
        if type(raw_actions) is not list:
            continue
        inherited_context: dict[str, Any] = {}
        for action_index, raw_action in enumerate(raw_actions):
            if type(raw_action) is not dict:
                continue
            action_name = raw_action.get("action")
            if not isinstance(action_name, str):
                continue
            definition = contract.actions.get(action_name)
            arguments = (
                raw_action
                if selected is SemanticSchemaVariant.FLAT
                else raw_action.get("args")
            )
            if type(arguments) is not dict:
                continue
            if definition is None:
                choice_definition = contract.actions.get("choose_reward")
                kind_schema = (
                    choice_definition.fields.get("kind")
                    if choice_definition is not None
                    else None
                )
                allowed_kinds = (
                    kind_schema.get("enum", ())
                    if isinstance(kind_schema, Mapping)
                    else ()
                )
                # Some providers occasionally promote the selected reward
                # ``kind`` into the action discriminator.  This shape still
                # contains exactly the player's choice and is unambiguous when
                # the name equals a current choose_reward kind.  Restore only
                # that wrapper; all fields still pass the normal JSON Schema.
                if (
                    choice_definition is not None
                    and action_name == arguments.get("kind")
                    and action_name in allowed_kinds
                    and "choice" in arguments
                ):
                    raw_action["action"] = "choose_reward"
                    definition = choice_definition
                    events.append(MappingProxyType({
                        "kind": "choice_kind_as_action",
                        "chainIndex": chain_index,
                        "actionIndex": action_index,
                        "from": action_name,
                        "to": "choose_reward",
                    }))
                else:
                    continue
            # JSON Schema defaults are annotations. The host applies the
            # game declaration before validation, only to absent named
            # fields. Explicit values and positional arguments stay intact.
            for field, field_schema in definition.fields.items():
                if field in arguments or "default" not in field_schema:
                    continue
                arguments[field] = _copy_semantic_payload_input(field_schema["default"])
                events.append(MappingProxyType({
                    "kind": "declared_default",
                    "chainIndex": chain_index,
                    "actionIndex": action_index,
                    "action": definition.name,
                    "field": field,
                }))
            for field, value in tuple(arguments.items()):
                field_schema = definition.fields.get(field)
                if not isinstance(field_schema, Mapping) or not isinstance(value, str):
                    continue
                if not _schema_accepts_integer(field_schema):
                    continue
                try:
                    integer = int(value)
                except ValueError:
                    continue
                if str(integer) != value:
                    continue
                arguments[field] = integer
                events.append(MappingProxyType({
                    "kind": "numeric_string_to_integer",
                    "chainIndex": chain_index,
                    "actionIndex": action_index,
                    "action": definition.name,
                    "field": field,
                }))
            for field, expected in definition.implicit_fields.items():
                if field not in arguments:
                    continue
                if not strict_json_equal(arguments[field], expected):
                    continue
                del arguments[field]
                events.append(MappingProxyType({
                    "kind": "fixed_implicit_field",
                    "chainIndex": chain_index,
                    "actionIndex": action_index,
                    "action": definition.name,
                    "field": field,
                }))
            for field in definition.inherited_fields:
                if field not in arguments or field not in inherited_context:
                    continue
                if not strict_json_equal(arguments[field], inherited_context[field]):
                    continue
                del arguments[field]
                events.append(MappingProxyType({
                    "kind": "inherited_context_field",
                    "chainIndex": chain_index,
                    "actionIndex": action_index,
                    "action": definition.name,
                    "field": field,
                }))
            for field in definition.fields:
                if field in arguments:
                    inherited_context[field] = _copy_semantic_payload_input(
                        arguments[field],
                    )
    return SemanticInputCanonicalization(
        payload=canonical,
        events=tuple(events),
    )


def _flat_action_schema(contract: ModelActionContract, action: str) -> dict[str, Any]:
    definition = contract.action(action)
    return {
        "type": "object",
        "required": ["action", *definition.required_arguments],
        "properties": {
            "action": {
                "const": action,
                "description": definition.description,
            },
            **{
                field: copy.deepcopy(dict(schema))
                for field, schema in definition.fields.items()
            },
        },
        "additionalProperties": False,
    }


def _nested_action_schema(contract: ModelActionContract, action: str) -> dict[str, Any]:
    definition = contract.action(action)
    return {
        "type": "object",
        "required": ["action", "args"],
        "properties": {
            "action": {
                "const": action,
                "description": definition.description,
            },
            "args": {
                "type": "object",
                "required": list(definition.required_arguments),
                "properties": {
                    field: copy.deepcopy(dict(schema))
                    for field, schema in definition.fields.items()
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    }


def _tuple_action_schema(contract: ModelActionContract, action: str) -> dict[str, Any]:
    definition = contract.action(action)
    items = [
        {"const": action, "description": definition.description},
        *(
            copy.deepcopy(dict(definition.fields[field]))
            for field in definition.argument_order
        ),
    ]
    return {
        "type": "array",
        "prefixItems": items,
        "items": False,
        "minItems": len(items),
        "maxItems": len(items),
    }


def build_compact_flat_semantic_tool_schema(
    contract: ModelActionContract,
    *,
    action_names: tuple[str, ...] | None = None,
    max_chains: int = 3,
) -> dict[str, Any]:
    """Build a compact model-facing schema; exact validation stays server-side."""

    if not isinstance(contract, ModelActionContract):
        raise TypeError("compact semantic Tool schema requires a ModelActionContract")
    if (
        isinstance(max_chains, bool)
        or not isinstance(max_chains, int)
        or not 1 <= max_chains <= 3
    ):
        raise ValueError("semantic max_chains must be from 1 to 3")
    selected_actions = tuple(dict.fromkeys(action_names or contract.action_names))
    if (
        not selected_actions
        or any(action not in contract.actions for action in selected_actions)
    ):
        raise ValueError("semantic Tool action_names must belong to the package contract")

    field_variants: dict[str, list[dict[str, Any]]] = {}
    field_owners: dict[str, list[str]] = {}
    signatures: list[str] = []
    for action in selected_actions:
        definition = contract.action(action)
        fields = ",".join(
            field + (
                "=" + json.dumps(schema["default"], ensure_ascii=False)
                if "default" in schema else ""
            )
            for field, schema in definition.fields.items()
        )
        signatures.append(f"{action}({fields})")
        for field, raw_schema in definition.fields.items():
            schema = copy.deepcopy(dict(raw_schema))
            variants = field_variants.setdefault(field, [])
            if schema not in variants:
                variants.append(schema)
            owners = field_owners.setdefault(field, [])
            if action not in owners:
                owners.append(action)

    special_notes: list[str] = []
    if "finish_action" in selected_actions:
        special_notes.append(
            "finish_action 表示放弃所有尚未执行的已解锁行动。",
        )
    if "place_die_castle" in selected_actions:
        special_notes.append(
            "place_die_castle 不使用 row；后续选城堡卡牌行要用 choose_castle_row。",
        )

    action_properties: dict[str, Any] = {
        "action": {
            "type": "string",
            "enum": list(selected_actions),
            "description": (
                "action 必须逐字使用 enum 中的名称，不要发明近义动作。动作签名为："
                + "; ".join(signatures) + "。括号内是该动作唯一允许的字段；"
                "空括号表示除 action 外不发送任何字段。只发送所选动作签名中的字段。"
                + "".join(special_notes)
                + "固定奖励和由引擎选择的棋子不写入参数。"
            ),
        },
    }
    for field, variants in field_variants.items():
        property_schema = (
            variants[0]
            if len(variants) == 1
            else {"anyOf": variants}
        )
        owner_text = "仅用于动作：" + "、".join(field_owners[field]) + "。"
        existing_description = property_schema.get("description")
        property_schema["description"] = owner_text + (
            str(existing_description) if existing_description else ""
        )
        action_properties[field] = property_schema

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["chains"],
        "properties": {
            "chains": {
                "type": "array",
                "description": (
                    f"一至 {max_chains} 条有实质差异的完整当前行动路线。"
                ),
                "items": {
                    "type": "object",
                    "required": ["actions"],
                    "properties": {
                        "actions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["action"],
                                "properties": action_properties,
                                "additionalProperties": False,
                            },
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    }


def build_semantic_tool_schema(
    variant: SemanticSchemaVariant | str,
    contract: ModelActionContract,
    *,
    action_names: tuple[str, ...] | None = None,
    max_chains: int = 3,
    include_display_name: bool = False,
) -> dict[str, Any]:
    if not isinstance(contract, ModelActionContract):
        raise TypeError("semantic Tool schema requires a ModelActionContract")
    if (
        isinstance(max_chains, bool)
        or not isinstance(max_chains, int)
        or not 1 <= max_chains <= 3
    ):
        raise ValueError("semantic max_chains must be from 1 to 3")
    selected = SemanticSchemaVariant(variant)
    selected_actions = tuple(dict.fromkeys(action_names or contract.action_names))
    if (
        not selected_actions
        or any(action not in contract.actions for action in selected_actions)
    ):
        raise ValueError("semantic Tool action_names must belong to the package contract")
    builders = {
        SemanticSchemaVariant.FLAT: _flat_action_schema,
        SemanticSchemaVariant.NESTED: _nested_action_schema,
        SemanticSchemaVariant.TUPLE: _tuple_action_schema,
    }
    action_schema = {
        "anyOf": [
            builders[selected](contract, action)
            for action in selected_actions
        ]
    }
    chain_properties: dict[str, Any] = {
        "actions": {
            "type": "array",
            "items": action_schema,
        },
    }
    if include_display_name:
        chain_properties["name"] = {
            "type": "string",
            "minLength": 1,
            "maxLength": 160,
            "description": (
                "Optional concise strategic objective for this route. It is "
                "preserved across Check only to compare authority candidates "
                "with your intent; it does not affect legality or outcome."
            ),
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["chains"],
        "properties": {
            "chains": {
                "type": "array",
                "description": (
                    f"One to {max_chains} materially distinct executable routes; "
                    "the runtime deterministically merges completely identical routes."
                ),
                "items": {
                    "type": "object",
                    "required": ["actions"],
                    "properties": chain_properties,
                    "additionalProperties": False,
                },
            }
        },
        "additionalProperties": False,
    }


def _normalize_action(
    raw: Any,
    variant: SemanticSchemaVariant,
    contract: ModelActionContract,
) -> SemanticAction:
    if variant is SemanticSchemaVariant.FLAT:
        assert isinstance(raw, Mapping)
        action = str(raw["action"])
        args = {key: value for key, value in raw.items() if key != "action"}
    elif variant is SemanticSchemaVariant.NESTED:
        assert isinstance(raw, Mapping)
        action = str(raw["action"])
        args = dict(raw["args"])
    else:
        assert isinstance(raw, list)
        action = str(raw[0])
        args = dict(zip(
            contract.action(action).argument_order,
            raw[1:],
            strict=True,
        ))
    normalized = {
        key: contract.canonicalize_argument(action, key, value)
        for key, value in args.items()
    }
    return SemanticAction.from_mapping(action, normalized)


def normalize_semantic_payload(
    payload: Any,
    variant: SemanticSchemaVariant | str,
    contract: ModelActionContract,
    *,
    max_chains: int = 3,
    action_names: tuple[str, ...] | None = None,
) -> SemanticPayload:
    selected = SemanticSchemaVariant(variant)
    canonicalization = canonicalize_semantic_payload_input(
        payload,
        selected,
        contract,
    )
    payload = canonicalization.payload
    schema = build_semantic_tool_schema(
        selected,
        contract,
        action_names=action_names,
        max_chains=max_chains,
        include_display_name=True,
    )
    errors = sorted(
        Draft202012Validator(schema).iter_errors(payload),
        key=lambda error: list(error.path),
    )
    if errors:
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        raise ValueError(
            f"invalid {selected.value} semantic payload at {path}: "
            f"{error.message}",
        )
    raw_chains = payload["chains"]
    if not 1 <= len(raw_chains) <= max_chains:
        raise ValueError(
            f"invalid {selected.value} semantic payload: chains count must be 1 to {max_chains}"
        )
    if any(not raw_chain["actions"] for raw_chain in raw_chains):
        raise ValueError(
            f"invalid {selected.value} semantic payload: every chain requires at least one action"
        )

    explicit_names = [
        raw_chain["name"]
        for raw_chain in payload["chains"]
        if "name" in raw_chain
    ]
    if len(explicit_names) != len(set(explicit_names)):
        duplicate = next(
            name
            for index, name in enumerate(explicit_names)
            if name in explicit_names[:index]
        )
        raise ValueError(f"duplicate semantic chain name: {duplicate}")

    names = set(explicit_names)
    chains: list[SemanticChain] = []
    for chain_index, raw_chain in enumerate(payload["chains"], start=1):
        name = raw_chain.get("name")
        if name is None:
            base = f"route-{chain_index}"
            name = base
            suffix = 2
            while name in names:
                name = f"{base}-{suffix}"
                suffix += 1
            names.add(name)
        chains.append(
            SemanticChain(
                name=name,
                actions=tuple(
                    _normalize_action(raw_action, selected, contract)
                    for raw_action in raw_chain["actions"]
                ),
            )
        )
    return SemanticPayload(chains=tuple(chains))


__all__ = [
    "SemanticInputCanonicalization",
    "build_compact_flat_semantic_tool_schema",
    "build_semantic_tool_schema",
    "canonicalize_semantic_payload_input",
    "normalize_semantic_payload",
    "strict_json_equal",
]
