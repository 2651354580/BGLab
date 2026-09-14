from __future__ import annotations

import json
import re
import string
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .closest import ProjectedProgram
from .model import SemanticAction, SemanticChain, SemanticSchemaVariant
from .model_contract import ModelActionContract, load_model_action_contract
from .outcome_contract import (
    OutcomeRequirement,
    parse_outcome_requirements,
    validate_checked_route_outcome,
)
from .schema import normalize_semantic_payload
from .outcome import validate_public_summary


_PATH_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
_ROOT_FIELDS = frozenset({
    "schemaVersion",
    "engine",
    "mechanicalOps",
    "actionRoles",
    "candidateDiversity",
    "choiceValueLabels",
    "rules",
    "outcomeProjection",
    "outcomeRequirements",
    "outcomePresentation",
})
_PROJECTION_RULE_FIELDS = frozenset({"semanticAction", "steps", "defaults"})


@dataclass(frozen=True)
class CollectionCapture:
    field: str
    repeat_by: str | None
    repeat_by_default: int | None


@dataclass(frozen=True)
class FormattedCapture:
    template: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class StepPattern:
    op: str
    effect_type: str | None
    where: Mapping[str, Any]
    where_regex: Mapping[str, re.Pattern[str]]
    capture: Mapping[str, str]
    formats: Mapping[str, FormattedCapture]
    repeat_min: int | None
    repeat_max: int | None
    collect: Mapping[str, CollectionCapture]


@dataclass(frozen=True)
class ProjectionRule:
    semantic_action: str
    steps: tuple[StepPattern, ...]
    defaults: Mapping[str, Any]


@dataclass(frozen=True)
class SemanticDescriptor:
    engine: str
    mechanical_ops: tuple[str, ...]
    rules: tuple[ProjectionRule, ...]
    action_roles: Mapping[str, str]
    collapse_choice_values_for_kinds: tuple[str, ...]
    diversity_ignored_actions: tuple[str, ...]
    choice_value_labels: Mapping[str, Mapping[str, str]]
    outcome_projection: tuple[str, ...]
    outcome_requirements: tuple[OutcomeRequirement, ...]
    source_path: Path
    model_contract: ModelActionContract
    outcome_presentation: str | None = None

    @property
    def model_action_names(self) -> tuple[str, ...]:
        """Semantic actions the model owns; derived engine steps stay internal."""

        mapped = {rule.semantic_action for rule in self.rules}
        return tuple(
            action
            for action in self.model_contract.action_names
            if action in mapped
            and self.action_roles.get(action, "intent") != "derived"
        )


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"semantic descriptor {field} must be non-empty text")
    return value


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"semantic descriptor {field} must be an object")
    return value


def _frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(json.loads(json.dumps(dict(value), ensure_ascii=False)))


def load_semantic_descriptor(definition: Any) -> SemanticDescriptor:
    engine = _text(getattr(definition, "id", None), "engine")
    root = Path(getattr(definition, "root", "")).resolve()
    model_contract = load_model_action_contract(definition)
    declared = getattr(definition, "engine_mapping_path", None)
    path = Path(declared).resolve() if declared is not None else (
        root / "semantic" / "engine-mapping.json"
    ).resolve()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("semantic descriptor path escapes game package")
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"semantic descriptor JSON is invalid: {exc}") from exc
    payload = _mapping(raw, "root")
    unknown_root = set(payload) - _ROOT_FIELDS
    if unknown_root:
        raise ValueError(
            "semantic descriptor root has unknown fields: "
            + ", ".join(sorted(unknown_root)),
        )
    if payload.get("schemaVersion") != 1:
        raise ValueError("semantic descriptor schemaVersion must be 1")
    if payload.get("engine") != engine:
        raise ValueError("semantic descriptor engine must match game definition")

    presentation = payload.get("outcomePresentation")
    if presentation not in (None, "summary-v1"):
        raise ValueError("semantic descriptor outcomePresentation must be summary-v1")

    mechanical = payload.get("mechanicalOps")
    if not isinstance(mechanical, list) or any(not isinstance(item, str) or not item for item in mechanical):
        raise ValueError("semantic descriptor mechanicalOps must be non-empty strings")
    if len(mechanical) != len(set(mechanical)):
        raise ValueError("semantic descriptor mechanicalOps must not contain duplicates")

    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("semantic descriptor rules must be a non-empty array")
    rules: list[ProjectionRule] = []
    declared_roles = payload.get("actionRoles", {})
    if not isinstance(declared_roles, dict):
        raise ValueError("semantic descriptor actionRoles must be an object")
    for rule_index, raw_rule in enumerate(raw_rules):
        rule = _mapping(raw_rule, f"rules[{rule_index}]")
        unknown_rule = set(rule) - _PROJECTION_RULE_FIELDS
        if unknown_rule:
            raise ValueError(
                f"semantic descriptor rules[{rule_index}] has unknown fields: "
                + ", ".join(sorted(unknown_rule)),
            )
        semantic_action = _text(rule.get("semanticAction"), f"rules[{rule_index}].semanticAction")
        if semantic_action in model_contract.actions:
            arguments = model_contract.action(semantic_action).argument_order
        elif declared_roles.get(semantic_action) == "derived":
            raw_defaults = rule.get("defaults", {})
            raw_steps_for_fields = rule.get("steps", [])
            inferred = list(raw_defaults) if isinstance(raw_defaults, dict) else []
            if isinstance(raw_steps_for_fields, list):
                for raw_step_fields in raw_steps_for_fields:
                    if not isinstance(raw_step_fields, dict):
                        continue
                    for field_group in ("capture", "format", "collect"):
                        values = raw_step_fields.get(field_group, {})
                        if isinstance(values, dict):
                            inferred.extend(str(key) for key in values)
            arguments = tuple(dict.fromkeys(inferred))
        else:
            raise ValueError(
                "semantic descriptor semanticAction is absent from the "
                f"model contract: {semantic_action}",
            )
        raw_steps = rule.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError(f"semantic descriptor rules[{rule_index}].steps must be non-empty")
        seen_captures: set[str] = set()
        steps: list[StepPattern] = []
        for step_index, raw_step in enumerate(raw_steps):
            step = _mapping(raw_step, f"rules[{rule_index}].steps[{step_index}]")
            op = _text(step.get("op"), f"rules[{rule_index}].steps[{step_index}].op")
            raw_effect_type = step.get("effectType")
            effect_type = (
                _text(
                    raw_effect_type,
                    f"rules[{rule_index}].steps[{step_index}].effectType",
                )
                if raw_effect_type is not None
                else None
            )
            if effect_type is not None and op != "chooseEffectOption":
                raise ValueError(
                    "semantic descriptor effectType requires chooseEffectOption"
                )
            where = _mapping(step.get("where", {}), f"rules[{rule_index}].steps[{step_index}].where")
            where_regex = _mapping(
                step.get("whereRegex", {}),
                f"rules[{rule_index}].steps[{step_index}].whereRegex",
            )
            compiled_regex: dict[str, re.Pattern[str]] = {}
            for field, pattern in where_regex.items():
                if not isinstance(field, str) or not field or not isinstance(pattern, str):
                    raise ValueError("semantic descriptor whereRegex must map fields to patterns")
                if not pattern.startswith("^") or not pattern.endswith("$"):
                    raise ValueError("semantic descriptor whereRegex patterns must be anchored")
                try:
                    compiled_regex[field] = re.compile(pattern)
                except re.error as exc:
                    raise ValueError(f"semantic descriptor whereRegex is invalid: {exc}") from exc
            capture = _mapping(
                step.get("capture", {}),
                f"rules[{rule_index}].steps[{step_index}].capture",
            )
            raw_formats = _mapping(
                step.get("format", {}),
                f"rules[{rule_index}].steps[{step_index}].format",
            )
            formats: dict[str, FormattedCapture] = {}
            for semantic_name, raw_format in raw_formats.items():
                if semantic_name not in arguments or semantic_name in seen_captures:
                    raise ValueError("semantic descriptor format must target one declared argument")
                format_spec = _mapping(
                    raw_format,
                    f"rules[{rule_index}].steps[{step_index}].format.{semantic_name}",
                )
                if set(format_spec) != {"template", "fields"}:
                    raise ValueError("semantic descriptor format requires template and fields")
                template = _text(format_spec.get("template"), "semantic descriptor format template")
                fields = format_spec.get("fields")
                if not isinstance(fields, list) or any(
                    not isinstance(field, str) or not field for field in fields
                ) or len(fields) != len(set(fields)):
                    raise ValueError("semantic descriptor format fields must be unique text")
                placeholders = []
                try:
                    for _, field_name, format_value, conversion in string.Formatter().parse(template):
                        if field_name is not None:
                            if format_value or conversion or not field_name.isidentifier():
                                raise ValueError("semantic descriptor format placeholders must be simple fields")
                            placeholders.append(field_name)
                except ValueError as exc:
                    raise ValueError(f"semantic descriptor format is invalid: {exc}") from exc
                if set(placeholders) != set(fields) or len(placeholders) != len(fields):
                    raise ValueError("semantic descriptor format placeholders must match fields")
                formats[semantic_name] = FormattedCapture(template=template, fields=tuple(fields))
                seen_captures.add(semantic_name)
            repeat = step.get("repeat")
            collect = _mapping(
                step.get("collect", {}),
                f"rules[{rule_index}].steps[{step_index}].collect",
            )
            repeat_min: int | None = None
            repeat_max: int | None = None
            parsed_collect: dict[str, CollectionCapture] = {}
            if repeat is None:
                if collect:
                    raise ValueError("semantic descriptor collect requires repeat")
            else:
                repeat_mapping = _mapping(
                    repeat,
                    f"rules[{rule_index}].steps[{step_index}].repeat",
                )
                if step_index != len(raw_steps) - 1:
                    raise ValueError("semantic descriptor repeat is allowed only on the final step")
                repeat_min = repeat_mapping.get("min")
                repeat_max = repeat_mapping.get("max")
                if (
                    isinstance(repeat_min, bool)
                    or not isinstance(repeat_min, int)
                    or isinstance(repeat_max, bool)
                    or not isinstance(repeat_max, int)
                    or not 1 <= repeat_min <= repeat_max <= 20
                ):
                    raise ValueError("semantic descriptor repeat bounds must satisfy 1 <= min <= max <= 20")
                if capture:
                    raise ValueError("semantic descriptor repeat cannot combine capture and collect")
                if formats:
                    raise ValueError("semantic descriptor repeat cannot combine format and collect")
                if not collect:
                    raise ValueError("semantic descriptor repeat requires collect")
                for semantic_name, raw_collection in collect.items():
                    if semantic_name not in arguments or semantic_name in seen_captures:
                        raise ValueError("semantic descriptor collect must target one declared argument")
                    collection = _mapping(
                        raw_collection,
                        f"rules[{rule_index}].steps[{step_index}].collect.{semantic_name}",
                    )
                    field = _text(collection.get("field"), "semantic descriptor collect field")
                    repeat_by = collection.get("repeatBy")
                    if repeat_by is not None and (not isinstance(repeat_by, str) or not repeat_by):
                        raise ValueError("semantic descriptor collect repeatBy must be an engine field")
                    repeat_by_default = collection.get("repeatByDefault")
                    if repeat_by_default is not None and (
                        repeat_by is None
                        or isinstance(repeat_by_default, bool)
                        or not isinstance(repeat_by_default, int)
                        or repeat_by_default < 1
                    ):
                        raise ValueError(
                            "semantic descriptor collect repeatByDefault requires "
                            "repeatBy and a positive integer"
                        )
                    if set(collection) - {"field", "repeatBy", "repeatByDefault"}:
                        raise ValueError("semantic descriptor collect contains unknown fields")
                    parsed_collect[semantic_name] = CollectionCapture(
                        field=field,
                        repeat_by=repeat_by,
                        repeat_by_default=repeat_by_default,
                    )
                    seen_captures.add(semantic_name)
            for semantic_name, engine_name in capture.items():
                if semantic_name not in arguments or not isinstance(engine_name, str) or not engine_name:
                    raise ValueError("semantic descriptor capture must map declared arguments to engine fields")
                if semantic_name in seen_captures:
                    raise ValueError(f"semantic descriptor duplicate capture: {semantic_name}")
                seen_captures.add(semantic_name)
            steps.append(
                StepPattern(
                        op=op,
                        effect_type=effect_type,
                        where=_frozen_mapping(where),
                        where_regex=MappingProxyType(compiled_regex),
                        capture=MappingProxyType({str(key): str(value) for key, value in capture.items()}),
                        formats=MappingProxyType(formats),
                        repeat_min=repeat_min,
                        repeat_max=repeat_max,
                        collect=MappingProxyType(parsed_collect),
                    )
            )
        defaults = _mapping(rule.get("defaults", {}), f"rules[{rule_index}].defaults")
        if any(key not in arguments for key in defaults):
            raise ValueError("semantic descriptor defaults contain undeclared arguments")
        if set(arguments) != seen_captures | set(defaults):
            raise ValueError("semantic descriptor captures/defaults must cover every semantic argument")
        rules.append(
            ProjectionRule(
                semantic_action=semantic_action,
                steps=tuple(steps),
                defaults=_frozen_mapping(defaults),
            )
        )

    rule_actions = tuple(dict.fromkeys(rule.semantic_action for rule in rules))
    raw_roles = _mapping(payload.get("actionRoles", {}), "actionRoles")
    unknown_roles = set(raw_roles) - set(rule_actions)
    if unknown_roles:
        raise ValueError(
            "semantic descriptor actionRoles contains unknown action: "
            f"{sorted(unknown_roles)[0]}"
        )
    allowed_roles = {"intent", "derived", "choice"}
    if any(not isinstance(role, str) or role not in allowed_roles for role in raw_roles.values()):
        raise ValueError(
            "semantic descriptor actionRoles values must be intent, derived, or choice"
        )
    action_roles = {
        action: str(raw_roles.get(action, "intent"))
        for action in rule_actions
    }

    raw_diversity = _mapping(
        payload.get("candidateDiversity", {}),
        "candidateDiversity",
    )
    unknown_diversity = set(raw_diversity) - {
        "collapseChoiceValuesForKinds",
        "ignoreActions",
    }
    if unknown_diversity:
        raise ValueError(
            "semantic descriptor candidateDiversity has unknown fields: "
            + ", ".join(sorted(unknown_diversity))
        )
    collapse_choice_values = raw_diversity.get(
        "collapseChoiceValuesForKinds",
        [],
    )
    if (
        not isinstance(collapse_choice_values, list)
        or any(
            not isinstance(item, str) or not item
            for item in collapse_choice_values
        )
        or len(collapse_choice_values) != len(set(collapse_choice_values))
    ):
        raise ValueError(
            "semantic descriptor collapseChoiceValuesForKinds must contain "
            "unique non-empty strings"
        )
    ignored_actions = raw_diversity.get("ignoreActions", [])
    if (
        not isinstance(ignored_actions, list)
        or any(not isinstance(item, str) or not item for item in ignored_actions)
        or len(ignored_actions) != len(set(ignored_actions))
    ):
        raise ValueError(
            "semantic descriptor candidate diversity ignoreActions must contain "
            "unique non-empty strings"
        )
    unknown_ignored_actions = set(ignored_actions) - set(model_contract.actions)
    if unknown_ignored_actions:
        raise ValueError(
            "semantic descriptor candidate diversity names an unknown action: "
            + sorted(unknown_ignored_actions)[0]
        )

    raw_choice_labels = _mapping(
        payload.get("choiceValueLabels", {}),
        "choiceValueLabels",
    )
    choice_value_labels: dict[str, Mapping[str, str]] = {}
    for kind, raw_labels in raw_choice_labels.items():
        if not isinstance(kind, str) or not kind:
            raise ValueError("semantic descriptor choice label kind must be text")
        labels = _mapping(raw_labels, f"choiceValueLabels.{kind}")
        if any(
            not isinstance(choice, str)
            or not choice.isdigit()
            or not isinstance(label, str)
            or not label
            for choice, label in labels.items()
        ):
            raise ValueError(
                "semantic descriptor choice labels must map integer strings "
                "to non-empty text"
            )
        choice_value_labels[kind] = _frozen_mapping(labels)

    projection = payload.get("outcomeProjection")
    if not isinstance(projection, list) or any(
        not isinstance(item, str) or _PATH_RE.fullmatch(item) is None for item in projection
    ):
        raise ValueError("semantic descriptor outcomeProjection must contain dot paths")
    if len(projection) != len(set(projection)):
        raise ValueError("semantic descriptor outcomeProjection must not contain duplicates")
    outcome_requirements = parse_outcome_requirements(
        payload.get("outcomeRequirements", []),
        model_contract=model_contract,
        mapped_actions=frozenset(rule_actions),
        action_roles=action_roles,
        outcome_projection=tuple(projection),
    )
    return SemanticDescriptor(
        engine=engine,
        mechanical_ops=tuple(mechanical),
        rules=tuple(rules),
        action_roles=_frozen_mapping(action_roles),
        collapse_choice_values_for_kinds=tuple(collapse_choice_values),
        diversity_ignored_actions=tuple(ignored_actions),
        choice_value_labels=MappingProxyType(choice_value_labels),
        outcome_projection=tuple(projection),
        outcome_requirements=outcome_requirements,
        outcome_presentation=presentation,
        source_path=resolved,
        model_contract=model_contract,
    )


_MISSING = object()


def _nested_value(value: Mapping[str, Any], path: str) -> Any:
    current: Any = value
    for key in path.split("."):
        if not isinstance(current, Mapping) or key not in current:
            return _MISSING
        current = current[key]
    return current


def _set_nested(target: dict[str, Any], path: str, value: Any) -> None:
    keys = path.split(".")
    current = target
    for key in keys[:-1]:
        child = current.get(key)
        if not isinstance(child, dict):
            child = {}
            current[key] = child
        current = child
    current[keys[-1]] = json.loads(json.dumps(value, ensure_ascii=False))


def _matches_pattern(
    step: Mapping[str, Any],
    pattern: StepPattern,
    effect_type: Any = _MISSING,
) -> bool:
    return (
        step.get("op") == pattern.op
        and (
            pattern.effect_type is None
            or effect_type == pattern.effect_type
        )
        and not any(step.get(key, _MISSING) != expected for key, expected in pattern.where.items())
        and not any(
            not isinstance(step.get(key), str) or regex.fullmatch(step[key]) is None
            for key, regex in pattern.where_regex.items()
        )
    )


def _project_rule(
    steps: list[dict[str, Any]],
    index: int,
    rule: ProjectionRule,
    effect_types: Mapping[int, str],
) -> tuple[int, dict[str, Any]] | None:
    cursor = index
    arguments: dict[str, Any] = {}
    for pattern in rule.steps:
        if pattern.repeat_min is None:
            if cursor >= len(steps) or not _matches_pattern(
                steps[cursor], pattern, effect_types.get(cursor, _MISSING)
            ):
                return None
            step = steps[cursor]
            for semantic_name, engine_name in pattern.capture.items():
                if engine_name in step:
                    arguments[semantic_name] = step[engine_name]
            for semantic_name, formatted in pattern.formats.items():
                missing = [field for field in formatted.fields if field not in step]
                if missing:
                    raise ValueError(
                        f"semantic descriptor format fields missing: {','.join(missing)}"
                    )
                arguments[semantic_name] = formatted.template.format_map(
                    {field: step[field] for field in formatted.fields}
                )
            cursor += 1
            continue

        repeated: list[dict[str, Any]] = []
        assert pattern.repeat_max is not None
        while (
            cursor < len(steps)
            and len(repeated) < pattern.repeat_max
            and _matches_pattern(
                steps[cursor], pattern, effect_types.get(cursor, _MISSING)
            )
        ):
            repeated.append(steps[cursor])
            cursor += 1
        if len(repeated) < pattern.repeat_min:
            return None
        if cursor < len(steps) and _matches_pattern(
            steps[cursor], pattern, effect_types.get(cursor, _MISSING)
        ):
            return None
        for semantic_name, collection in pattern.collect.items():
            values: list[Any] = []
            for step in repeated:
                if collection.field not in step:
                    raise ValueError(f"semantic descriptor collect field missing: {collection.field}")
                count = 1
                if collection.repeat_by is not None:
                    count = step.get(
                        collection.repeat_by,
                        collection.repeat_by_default,
                    )
                    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                        raise ValueError("semantic descriptor collect repeatBy must be a positive integer")
                values.extend([step[collection.field]] * count)
            arguments[semantic_name] = values
    for name, value in rule.defaults.items():
        arguments.setdefault(name, value)
    return cursor - index, arguments


def project_authority_program(
    descriptor: SemanticDescriptor,
    program: Mapping[str, Any],
    *,
    validate_outcome: bool = True,
) -> ProjectedProgram:
    if not isinstance(program, Mapping) or program.get("complete") is not True:
        raise ValueError("authority program must be complete")
    program_id = program.get("programId")
    if not isinstance(program_id, str) or not program_id:
        raise ValueError("authority program must have a programId")
    raw_steps = program.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps or any(not isinstance(step, dict) for step in raw_steps):
        raise ValueError("authority program steps must be non-empty mappings")
    steps = json.loads(json.dumps(raw_steps, ensure_ascii=False))
    raw_automatic_indexes = program.get("automaticStepIndexes", [])
    if (
        not isinstance(raw_automatic_indexes, list)
        or any(
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < len(steps)
            for index in raw_automatic_indexes
        )
        or len(raw_automatic_indexes) != len(set(raw_automatic_indexes))
    ):
        raise ValueError("authority program automaticStepIndexes are invalid")
    automatic_indexes = frozenset(raw_automatic_indexes)
    effect_types: dict[int, str] = {}
    raw_trace = program.get("causalTrace", [])
    if isinstance(raw_trace, list):
        for event in raw_trace:
            if not isinstance(event, Mapping):
                continue
            action_step = event.get("actionStep")
            choice_type = event.get("choiceType")
            if (
                isinstance(action_step, bool)
                or not isinstance(action_step, int)
                or action_step < 0
                or not isinstance(choice_type, str)
                or not choice_type
            ):
                continue
            prior = effect_types.get(action_step)
            if prior is not None and prior != choice_type:
                raise ValueError(
                    f"authority program has conflicting choiceType at step {action_step}"
                )
            effect_types[action_step] = choice_type
    actions: list[SemanticAction] = []
    index = 0
    while index < len(steps):
        op = steps[index].get("op")
        if index in automatic_indexes:
            index += 1
            continue
        match = next(
            (
                (rule, projected)
                for rule in descriptor.rules
                if (
                    projected := _project_rule(
                        steps, index, rule, effect_types
                    )
                ) is not None
            ),
            None,
        )
        if match is None:
            if op in descriptor.mechanical_ops:
                index += 1
                continue
            raise ValueError(f"UNMAPPED_ENGINE_STEP:{index}:{op}")
        matched_rule, (consumed, arguments) = match
        if descriptor.action_roles.get(matched_rule.semantic_action) != "derived":
            actions.append(
                SemanticAction.from_mapping(
                    matched_rule.semantic_action,
                    arguments,
                ),
            )
        index += consumed
    if not actions:
        raise ValueError("authority program has no projected semantic actions")

    chain = SemanticChain(
        name=f"authority-{program_id[:8]}",
        actions=tuple(actions),
    )
    canonical = normalize_semantic_payload(
        {
            "chains": [
                {
                    "name": chain.name,
                    "actions": [action.to_dict() for action in chain.actions],
                }
            ]
        },
        SemanticSchemaVariant.NESTED,
        descriptor.model_contract,
    ).chains[0]
    outcome = program.get("outcome")
    if not isinstance(outcome, Mapping):
        outcome = program.get("netOutcome")
    if not isinstance(outcome, Mapping):
        outcome = {}
    projected_outcome: dict[str, Any] = {}
    for path in descriptor.outcome_projection:
        value = _nested_value(outcome, path)
        if value is not _MISSING:
            _set_nested(projected_outcome, path, value)
    if validate_outcome:
        validate_checked_route_outcome(descriptor, canonical, projected_outcome)
    return ProjectedProgram.create(
        program_id=program_id,
        chain=canonical,
        engine_steps=steps,
        outcome=projected_outcome,
        outcome_key=projected_outcome,
        public_summary=validate_public_summary(
            program.get("publicSummary"),
            required=validate_outcome and descriptor.outcome_presentation == "summary-v1",
        ),
    )


def project_frontier_semantic_actions(
    descriptor: SemanticDescriptor,
    frontier: Sequence[Mapping[str, Any]],
) -> tuple[SemanticAction, ...]:
    """Project explicit one-step Authority choices without exposing IDs."""

    projected: list[SemanticAction] = []
    for raw in frontier:
        step = json.loads(json.dumps(dict(raw), ensure_ascii=False))
        if "op" not in step and isinstance(step.get("type"), str):
            step["op"] = step.pop("type")
        choice_type = step.pop("choiceType", None)
        effect_types = {0: choice_type} if isinstance(choice_type, str) else {}
        for rule in descriptor.rules:
            match = _project_rule([step], 0, rule, effect_types)
            if match is None or match[0] != 1:
                continue
            if descriptor.action_roles.get(rule.semantic_action) == "derived":
                continue
            action = SemanticAction.from_mapping(rule.semantic_action, match[1])
            if action not in projected:
                projected.append(action)
    return tuple(projected)


def _rule_accepts_semantic_action(rule: ProjectionRule, action: SemanticAction) -> bool:
    if rule.semantic_action != action.action:
        return False
    arguments = action.to_dict()["args"]
    dynamic = {
        name
        for pattern in rule.steps
        for name in (*pattern.capture.keys(), *pattern.formats.keys(), *pattern.collect.keys())
    }
    return all(
        name in dynamic or arguments.get(name, _MISSING) == value
        for name, value in rule.defaults.items()
    )


def _expand_formatted_argument(
    formatted: FormattedCapture,
    value: Any,
) -> dict[str, str]:
    if not isinstance(value, str) or not value:
        raise ValueError("semantic formatted argument must be non-empty text")
    pattern = ""
    for literal, field_name, format_value, conversion in string.Formatter().parse(
        formatted.template,
    ):
        if format_value or conversion:
            raise ValueError("semantic formatted argument uses unsupported formatting")
        pattern += re.escape(literal)
        if field_name is not None:
            pattern += f"(?P<{field_name}>.+?)"
    matched = re.fullmatch(pattern, value)
    if matched is None:
        raise ValueError("semantic formatted argument does not match its template")
    return {
        field: matched.group(field)
        for field in formatted.fields
    }


def semantic_chain_to_authority_query(
    descriptor: SemanticDescriptor,
    chain: SemanticChain,
    *,
    relaxed_action_indexes: frozenset[int] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(relaxed_action_indexes, frozenset) or any(
        isinstance(index, bool)
        or not isinstance(index, int)
        or index < 0
        or index >= len(chain.actions)
        for index in relaxed_action_indexes
    ):
        raise ValueError("semantic relaxed action indexes are invalid")
    facts: list[dict[str, Any]] = []

    def append_fact(fact: dict[str, Any]) -> None:
        facts.append(fact)

    for action_index, action in enumerate(chain.actions):
        relax_arguments = action_index in relaxed_action_indexes
        matching_rules = [
            item for item in descriptor.rules
            if _rule_accepts_semantic_action(item, action)
        ]
        if action_index > 0:
            # The same choice can start a resumed decision or continue an
            # existing action. Prefer its continuation mapping when present;
            # do not insert another root begin in the middle of a transaction.
            continuations = [item for item in matching_rules if item.steps[0].op != "begin"]
            matching_rules = continuations or matching_rules
        rule = next(iter(matching_rules), None)
        if rule is None:
            raise ValueError(f"NO_SEMANTIC_AUTHORITY_RULE:{action.action}")
        arguments = action.to_dict()["args"]
        for pattern in rule.steps:
            if pattern.repeat_min is None:
                fact: dict[str, Any] = {"op": pattern.op, **dict(pattern.where)}
                if not relax_arguments:
                    for semantic_name, engine_name in pattern.capture.items():
                        value = arguments.get(semantic_name, _MISSING)
                        if value is not _MISSING and value is not None:
                            fact[engine_name] = value
                    for semantic_name, formatted in pattern.formats.items():
                        value = arguments.get(semantic_name, _MISSING)
                        if value is not _MISSING and value is not None:
                            fact.update(_expand_formatted_argument(formatted, value))
                append_fact(fact)
                continue
            if relax_arguments:
                append_fact({"op": pattern.op, **dict(pattern.where)})
                continue
            for semantic_name, collection in pattern.collect.items():
                values = arguments.get(semantic_name)
                if not isinstance(values, list):
                    raise ValueError("semantic repeat argument must be a list")
                if collection.repeat_by is None:
                    for value in values:
                        append_fact({"op": pattern.op, **dict(pattern.where), collection.field: value})
                    continue
                counts = Counter(values)
                emitted: set[Any] = set()
                for value in values:
                    if value in emitted:
                        continue
                    emitted.add(value)
                    append_fact(
                        {
                            "op": pattern.op,
                            **dict(pattern.where),
                            collection.field: value,
                            collection.repeat_by: counts[value],
                        }
                    )
    return {"stepsContain": facts}
