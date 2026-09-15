from __future__ import annotations

import copy
import concurrent.futures
import hashlib
import inspect
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from bglab.games.registry import GameDefinition
from bglab.games.submission_state import SubmissionState
from bglab.games.semantic_validation import (
    SemanticSchemaVariant,
    build_compact_flat_semantic_tool_schema,
    build_semantic_tool_schema,
    load_semantic_descriptor,
)
from bglab.games.semantic_validation.schema import (
    canonicalize_semantic_payload_input,
)
from bglab.games.semantic_validation.render import render_compact_checked_route_facts
from bglab.games.tools.semantic_check import (
    CheckHandler,
    CheckPorts,
    CheckRequest,
)
from bglab.games.tools.semantic_batch_check import (
    BatchCheckHandler,
    BatchCheckRequest,
    render_compact_batch_failures,
)
from bglab.games.tools.semantic_commit import (
    CommitCoordinator,
    CommitHandler,
    CommitOutcome,
    CommitPorts,
    CommitRequest,
    CommitResolutionError,
)
from bglab.games.tools.semantic_lifecycle import (
    BoundCandidate,
    SemanticLifecycle,
    retain_checked_candidates,
    restore_turn_semantic_lifecycle,
    semantic_chain_fingerprint,
    semantic_identity_from_ctx,
    semantic_route_number,
    serialize_semantic_lifecycle,
)
from bglab.games.tools.public_error import render_public_bgact_error
from bglab.games.tools.semantic_contract import SemanticInteractionContract
from bglab.tools.base import Tool, ToolCallResult, tool_error


logger = logging.getLogger("bglab.games.semantic_act")


SEMANTIC_V2_RETRIEVAL_SURFACE_HASH = hashlib.sha256(
    b"bglab-semantic-retrieval/v2",
).hexdigest()

SEMANTIC_RESULT_TO_SUBMISSION_BOUNDARY = (
    SemanticInteractionContract.compatibility_submission_boundary()
)

SEMANTIC_CHECK_RESULT_GUIDANCE = (
    SemanticInteractionContract.compatibility_check_result_guidance()
)

SEMANTIC_CHECK_RESPONSE_CONTRACT = (
    "[BgAct Check｜只读，尚未提交]"
)


@dataclass(frozen=True)
class BgActOperation:
    """One independently testable operation plugged into the BgAct shell."""

    name: str
    summary: str
    schema_branch: Mapping[str, Any]
    handler: Callable[[Mapping[str, Any]], Any]
    state_changing: bool

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("BgAct operation name must be non-empty")


def append_semantic_check_result_guidance(
    rendered_result: str,
    interaction_contract: SemanticInteractionContract | None = None,
) -> str:
    text = str(rendered_result).rstrip()
    guidance = (
        interaction_contract.check_result_guidance()
        if interaction_contract is not None
        else SEMANTIC_CHECK_RESULT_GUIDANCE
    )
    if not text.startswith(SEMANTIC_CHECK_RESPONSE_CONTRACT):
        text = f"{SEMANTIC_CHECK_RESPONSE_CONTRACT}\n\n{text}"
    if guidance.strip() not in text:
        text += guidance
    return text


_BATCH_DISPLAY_LABEL_RE = re.compile(r"R([1-3])\.C([1-5])")
_SINGLE_DISPLAY_LABEL_RE = re.compile(r"(?<![A-Za-z0-9.])C([1-5])(?![A-Za-z0-9.])")


def project_current_turn_route_ids(
    rendered_result: str,
    source_route_ids: Mapping[str, int],
) -> str:
    """Replace internal display ranks with persisted turn-local numbers."""

    text = str(rendered_result)
    placeholders: dict[str, str] = {}
    for source_label, route_id in sorted(
        source_route_ids.items(),
        key=lambda item: (item[1], item[0]),
    ):
        placeholder = f"__BG_READY_ROUTE_{route_id}__"
        placeholders[placeholder] = str(route_id)
        text = text.replace(source_label, placeholder)

    text = _BATCH_DISPLAY_LABEL_RE.sub(
        lambda match: f"路线{match.group(1)}-参考{match.group(2)}",
        text,
    )
    text = _SINGLE_DISPLAY_LABEL_RE.sub(
        lambda match: f"参考{match.group(1)}",
        text,
    )
    for placeholder, route_id in placeholders.items():
        text = text.replace(placeholder, route_id)

    text = text.replace("可提交显示路线：", "可提交路线编号：")
    text = text.replace("可提交短 ID：", "可提交路线编号：")
    text = text.replace(
        "显示标签不进入 Tool 输入",
        "只有明确列出的可提交路线编号才能作为 commit.id；组号和参考号不能提交",
    )
    text = text.replace(
        "显示编号永远不进入 Tool 输入",
        "只有明确列出的可提交路线编号才能作为 commit.id",
    )
    text = re.sub(
        r"可提交路线编号：([0-9、]+)",
        lambda match: "可提交路线编号：" + "、".join(dict.fromkeys(
            match.group(1).split("、"),
        )),
        text,
    )
    return text


def _operation_chain_branch(
    definition: GameDefinition,
    *,
    operation: str,
    max_chains: int,
    interaction_contract: SemanticInteractionContract | None = None,
) -> dict[str, Any]:
    descriptor = load_semantic_descriptor(definition)
    contract = interaction_contract or SemanticInteractionContract.for_descriptor(
        descriptor,
    )
    if descriptor.model_contract.tool_schema == "compact-flat":
        model_schema = build_compact_flat_semantic_tool_schema(
            descriptor.model_contract,
            action_names=contract.action_names,
            max_chains=max_chains,
        )
    else:
        model_schema = build_semantic_tool_schema(
            SemanticSchemaVariant.FLAT,
            descriptor.model_contract,
            action_names=contract.action_names,
            max_chains=max_chains,
        )
    branch = copy.deepcopy(model_schema)
    branch.pop("$schema", None)
    branch.pop("title", None)
    branch["properties"] = {
        "operation": {"type": "string", "const": operation},
        **branch["properties"],
    }
    branch["required"] = ["operation", "chains"]
    branch["additionalProperties"] = False
    return branch


def _operation_schema_branches(
    definition: GameDefinition,
    interaction_contract: SemanticInteractionContract | None = None,
) -> dict[str, dict[str, Any]]:
    contract = interaction_contract or SemanticInteractionContract.for_descriptor(
        load_semantic_descriptor(definition),
    )
    chain_branch = _operation_chain_branch(
        definition,
        operation="check",
        max_chains=3,
        interaction_contract=contract,
    )
    chains = copy.deepcopy(chain_branch["properties"]["chains"])
    chains["description"] = (
        "Check 输入有实质差异的备选路线；直接 Commit 输入一条完整路线。输入条数遵循各操作的 Schema 约束。"
        "完全相同的 Check 路线会被确定性合并。"
    )
    check_chains = copy.deepcopy(chains)
    check_chains["minItems"] = 1
    check_chains["maxItems"] = 3
    commit_chains = copy.deepcopy(chains)
    commit_chains["minItems"] = 1
    commit_chains["maxItems"] = 1
    return {
        "check": {
            "description": contract.check_operation_summary(),
            "properties": {"chains": check_chains},
            "required": ["chains"],
        },
        "commit": {
            "description": contract.commit_operation_summary(),
            "properties": {
                "id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "填写 Check 返回的可提交路线编号，如 1。同一 turn 的多次 Check"
                        "共用连续编号，旧编号不会改指其他路线；提交完成后的新 turn 从1重新编号。"
                        "编号只绑定校验时的局面；局面改变后须重新 Check。"
                        "尚未 Check 时可在 chains 中直接提交完整行动链，不要猜编号。"
                    ),
                },
                "chains": commit_chains,
            },
            "oneOf": [
                {"required": ["chains"]},
                {"required": ["id"]},
            ],
        },
    }


def _schema_from_operations(
    operations: Mapping[str, BgActOperation],
) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "operation": {
            "type": "string",
            "enum": list(operations),
            "description": (
                "check 只读校验拟议路线，不改变状态；commit 提交当前 Check 的一个"
                "路线编号，或直接提交一条完整行动链。"
            ),
        },
    }
    for operation in operations.values():
        for name, schema in operation.schema_branch.get(
            "properties", {},
        ).items():
            existing = properties.get(name)
            if existing is not None and existing != schema:
                existing_base = copy.deepcopy(existing)
                incoming_base = copy.deepcopy(schema)
                for item in (existing_base, incoming_base):
                    item.pop("minItems", None)
                    item.pop("maxItems", None)
                if existing_base != incoming_base:
                    raise ValueError(
                        f"BgAct operation property schema differs: {name}",
                    )
                continue
            properties[name] = copy.deepcopy(schema)
            if name == "chains":
                properties[name].pop("minItems", None)
                properties[name].pop("maxItems", None)
    variants: list[dict[str, Any]] = []
    for operation in operations.values():
        alternatives = operation.schema_branch.get("oneOf") or ({
            "required": list(operation.schema_branch.get("required", ())),
        },)
        for alternative in alternatives:
            required = [
                "operation",
                *list(operation.schema_branch.get("required", ())),
                *list(alternative.get("required", ())),
            ]
            mutually_exclusive = (
                "chains"
                if "id" in required
                else "id"
            )
            variant_properties: dict[str, Any] = {
                "operation": {"const": operation.name},
            }
            for property_name in required:
                property_schema = operation.schema_branch.get(
                    "properties", {},
                ).get(property_name)
                if not isinstance(property_schema, dict):
                    continue
                bounds = {
                    key: copy.deepcopy(property_schema[key])
                    for key in ("minItems", "maxItems")
                    if key in property_schema
                }
                if bounds:
                    variant_properties[property_name] = bounds
            variants.append({
                "type": "object",
                "required": list(dict.fromkeys(required)),
                "properties": variant_properties,
                "not": {"required": [mutually_exclusive]},
            })
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "BgAct",
        "type": "object",
        "required": ["operation"],
        "properties": properties,
        "anyOf": variants,
        "additionalProperties": False,
    }


def build_semantic_operation_schema(definition: GameDefinition) -> dict[str, Any]:
    interaction_contract = SemanticInteractionContract.for_descriptor(
        load_semantic_descriptor(definition),
    )
    branches = _operation_schema_branches(definition, interaction_contract)
    operations = {
        name: BgActOperation(
            name=name,
            summary=name,
            schema_branch=branch,
            handler=lambda _arguments: None,
            state_changing=name == "commit",
        )
        for name, branch in branches.items()
    }
    return _schema_from_operations(operations)


def create_semantic_operation_tool(
    ctx: dict[str, Any],
    definition: GameDefinition,
) -> Tool:
    descriptor = load_semantic_descriptor(definition)
    interaction_contract = SemanticInteractionContract.for_descriptor(descriptor)
    check_handler = CheckHandler(descriptor)
    coordinator = CommitCoordinator()
    base_prompt = interaction_contract.tool_guidance()
    tool: Tool

    def invalid(
        code: str,
        message: str,
        *,
        outcome_kind: str | None = None,
        state_changed: bool | None = False,
        public_evidence: str = "",
    ):
        kind = outcome_kind or (
            "infrastructure_failure"
            if code in {
                "AUTHORITY_WORKER_FAILURE",
                "SEMANTIC_WORKER_UNAVAILABLE",
                "SEMANTIC_VALIDATION_FAILED",
            }
            else "rejected"
        )
        public = render_public_bgact_error(code, message)
        visible = public.render()
        if str(public_evidence).strip():
            visible += "\n\n" + str(public_evidence).strip()
        return tool_error(
            visible,
            metadata={
                "outcome_kind": kind,
                "state_changed": state_changed,
                "public_code": public.code,
                "internal_code": code,
                "internal_detail": message,
            },
        )

    def completed(
        content: str,
        *,
        outcome_kind: str,
        state_changed: bool,
        public_metadata: Mapping[str, Any] | None = None,
    ):
        metadata = {
            "outcome_kind": outcome_kind,
            "state_changed": state_changed,
        }
        if public_metadata:
            metadata.update(copy.deepcopy(dict(public_metadata)))
        return ToolCallResult(
            content,
            metadata=metadata,
        )

    def persist() -> None:
        callback = ctx.get("_persist_state")
        if callable(callback):
            callback()

    def identity():
        return semantic_identity_from_ctx(ctx)

    def load_lifecycle() -> SemanticLifecycle | None:
        current = identity()
        if current is None:
            return None
        return restore_turn_semantic_lifecycle(
            ctx.get("_semantic_lifecycle_v2"),
            current,
        )

    def persist_lifecycle(lifecycle: SemanticLifecycle) -> None:
        ctx["_semantic_lifecycle_v2"] = serialize_semantic_lifecycle(lifecycle)
        persist()

    def persist_checked_candidates(current, candidates) -> SemanticLifecycle | None:
        lifecycle = retain_checked_candidates(load_lifecycle(), current, candidates)
        ctx["_semantic_lifecycle_v2"] = (
            serialize_semantic_lifecycle(lifecycle) if lifecycle is not None else None
        )
        persist()
        return lifecycle

    def record_argument_normalizations(
        events: tuple[Mapping[str, Any], ...],
    ) -> None:
        if not events:
            return
        ctx.setdefault("_semantic_argument_normalizations", []).extend(
            dict(event) for event in events
        )

    def check(arguments: Mapping[str, Any]):
        arguments = dict(arguments)
        duplicate_route_count = 0
        raw_chains = arguments.get("chains")
        if isinstance(raw_chains, list) and len(raw_chains) > 1:
            unique_chains: list[Any] = []
            seen_action_fingerprints: set[str] = set()
            for chain in raw_chains:
                comparable = chain.get("actions") if isinstance(chain, Mapping) else chain
                fingerprint = json.dumps(
                    comparable,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if fingerprint in seen_action_fingerprints:
                    duplicate_route_count += 1
                    continue
                seen_action_fingerprints.add(fingerprint)
                unique_chains.append(chain)
            if duplicate_route_count:
                arguments["chains"] = unique_chains
                record_argument_normalizations(({
                    "field": "chains",
                    "from": "duplicate_routes",
                    "to": "unique_routes",
                    "removed": duplicate_route_count,
                },))

        def add_duplicate_route_notice(content: str) -> str:
            if not duplicate_route_count:
                return content
            return (
                str(content).rstrip()
                + "\n\n输入中完全相同的路线已合并；已保留一条进行校验，无需重发。"
            )

        def compact_result(facts, source_facts=None) -> str:
            rendered = render_compact_checked_route_facts(
                facts,
                choice_value_labels=descriptor.choice_value_labels,
                source_facts=source_facts,
            )
            labels = "、".join(fact.label for fact in facts if fact.commit_ready)
            if labels:
                rendered += f"\n\n可提交路线编号：{labels}。"
            return rendered

        def finish_check_result(content, route_ids, diagnostic="") -> str:
            if diagnostic:
                content += (
                    "\n\n原始输入草稿的校验说明：以下问题针对输入草稿，"
                    "不否定上方已核验的候选；输入草稿序号不是可提交编号。\n"
                    + diagnostic
                )
            # All evidence and notices precede the single next-action handoff.
            return project_current_turn_route_ids(
                append_semantic_check_result_guidance(
                    add_duplicate_route_notice(content), interaction_contract,
                ),
                route_ids,
            )

        current = identity()
        if current is None:
            return invalid(
                "SEMANTIC_IDENTITY_UNAVAILABLE",
                "当前决策缺少 semantic-v2 身份或 surface。",
            )
        if ctx.get("_act_submitted"):
            return invalid(
                "ACTION_ALREADY_COMMITTED",
                "当前决策已经提交，不能再次校验。",
            )
        try:
            active = load_lifecycle()
        except ValueError as exc:
            return invalid("INVALID_SEMANTIC_BINDING", str(exc))
        if active is not None and active.commit_fence is not None:
            return invalid(
                "COMMIT_FENCE_ACTIVE",
                "当前提交 fence 尚未清理，请先由宿主恢复。",
            )
        identity_key = current.to_dict()
        check_intent = dict(arguments)
        chains = arguments.get("chains")
        if isinstance(chains, list) and all(
            isinstance(chain, Mapping)
            and set(chain).issubset({"name", "actions"})
            and isinstance(chain.get("actions"), list)
            for chain in chains
        ):
            # Route labels and batch order do not change player choices. Keep
            # the submitted presentation intact; normalize only its identity.
            # Action order inside each route remains significant.
            check_intent["chains"] = sorted(
                json.dumps(
                    chain["actions"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for chain in chains
            )
        check_fingerprint = hashlib.sha256(
            json.dumps(
                check_intent,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
        ).hexdigest()
        if ctx.get("_semantic_check_fingerprint_identity") != identity_key:
            ctx["_semantic_check_fingerprint_identity"] = copy.deepcopy(identity_key)
            ctx["_semantic_last_check_fingerprint"] = None
            ctx["_semantic_last_check_diagnostic"] = ""
        if ctx.get("_semantic_last_check_fingerprint") == check_fingerprint:
            return invalid(
                "DUPLICATE_CHECK",
                "当前 DecisionFrame 已执行完全相同的 Check。",
                public_evidence=ctx.get("_semantic_last_check_diagnostic", ""),
            )
        ctx["_semantic_last_check_diagnostic"] = ""

        def remember_deterministic_failure(codes: list[str | None]) -> None:
            transient = {
                "SEMANTIC_WORKER_UNAVAILABLE",
                "SEMANTIC_VALIDATION_FAILED",
                "SEMANTIC_BATCH_ROUTE_FAILED",
            }
            if any(code in transient for code in codes):
                return
            ctx["_semantic_last_check_fingerprint"] = check_fingerprint
            persist()
        chains = arguments.get("chains")
        if isinstance(chains, list) and len(chains) > 1:
            try:
                batch_request = BatchCheckRequest.from_arguments(
                    arguments,
                    decision_id=current.decision_id,
                    state_hash=current.state_hash,
                )
            except (TypeError, ValueError) as exc:
                return invalid("INVALID_SEMANTIC_INPUT", str(exc))
            batch_outcome = BatchCheckHandler(descriptor).handle(
                batch_request,
                CheckPorts.from_context(ctx),
            )
            record_argument_normalizations(batch_outcome.normalization_events)
            diagnostics: dict[str, list[int]] = {}
            for route in batch_outcome.route_outcomes:
                if route.outcome.authority_diagnostic:
                    diagnostics.setdefault(route.outcome.authority_diagnostic, []).append(route.route_index)
            diagnostic = "\n\n".join(
                "输入草稿 " + "、".join(map(str, indexes)) + "：\n" + detail
                for detail, indexes in diagnostics.items()
            )
            ctx["_semantic_last_check_diagnostic"] = diagnostic
            ctx["_semantic_lifecycle_pending_confirmation"] = False
            candidates: dict[str, BoundCandidate] = {}
            checked_route_aliases: dict[str, str] = {}
            source_route_labels: dict[str, str] = {}
            route_label_by_fingerprint: dict[str, str] = {}
            for route in batch_outcome.route_outcomes:
                for primary in route.outcome.candidates:
                    if primary.commit_ready is not True:
                        return invalid(
                            "INVALID_SEMANTIC_BINDING",
                            "完整校验路线缺少可提交绑定。",
                        )
                    source_label = f"R{route.route_index}.{primary.label}"
                    fingerprint = semantic_chain_fingerprint(primary.semantic_chain)
                    existing_route_id = route_label_by_fingerprint.get(fingerprint)
                    if existing_route_id is not None:
                        source_route_labels[source_label] = existing_route_id
                        continue
                    label = f"C{len(candidates) + 1}"
                    checked_route_aliases[label] = source_label
                    bound = BoundCandidate.from_dict(
                        {
                            "label": label,
                            "programId": primary.program_id,
                            "engineSteps": primary.engine_steps,
                            "canonicalStepsFingerprint": primary.canonical_steps_fingerprint,
                            "bindingFingerprint": primary.binding_fingerprint,
                            "intentExact": primary.intent_exact,
                            "commitReady": primary.commit_ready,
                            "semanticChain": primary.semantic_chain,
                        },
                    )
                    if bound is not None:
                        candidates[label] = bound
                        source_route_labels[source_label] = label
                        route_label_by_fingerprint[fingerprint] = label
            ctx["_semantic_latest_check_route_count"] = len(
                batch_outcome.checked_routes,
            )
            ctx["_semantic_latest_ready_route_count"] = len(candidates)
            if candidates:
                lifecycle = persist_checked_candidates(current, candidates)
            elif batch_outcome.checked_routes:
                lifecycle = persist_checked_candidates(current, {})
            else:
                remember_deterministic_failure([
                    route.outcome.error_code
                    for route in batch_outcome.route_outcomes
                ])
                return invalid(
                    batch_outcome.error_code or "BATCH_CHECK_FAILED",
                    batch_outcome.error_message or "批量校验失败。",
                    public_evidence=batch_outcome.rendered_result,
                )
            source_route_ids = {
                source: semantic_route_number(lifecycle, candidates[label])
                for source, label in source_route_labels.items()
            }
            ctx["_semantic_last_check_fingerprint"] = check_fingerprint
            if descriptor.model_contract.tool_schema == "compact-flat":
                compact_facts = []
                seen_route_ids: set[int] = set()
                sources_by_id = {}
                for route in batch_outcome.route_outcomes:
                    for source in route.outcome.checked_routes:
                        route_id = source_route_ids.get(source.label)
                        if route_id is not None:
                            sources_by_id.setdefault(route_id, []).append((route.route_index, source))
                for fact in batch_outcome.checked_routes:
                    route_id = source_route_ids.get(fact.label)
                    if route_id is None or route_id in seen_route_ids:
                        continue
                    seen_route_ids.add(route_id)
                    compact_facts.append(fact)
                raw_visible_result = compact_result(compact_facts, {
                    fact.label: sources_by_id[source_route_ids[fact.label]]
                    for fact in compact_facts
                })
                failed_count = sum(
                    not route.outcome.ok for route in batch_outcome.route_outcomes
                )
                if failed_count:
                    raw_visible_result += (
                        "\n\n"
                        + render_compact_batch_failures(
                            batch_outcome.route_outcomes,
                        )
                    )
                if len(compact_facts) < len(batch_outcome.checked_routes):
                    raw_visible_result += "\n\n多条输入收敛为同一条路线，已合并。"
            else:
                raw_visible_result = batch_outcome.rendered_result
            visible_result = finish_check_result(
                raw_visible_result, source_route_ids, diagnostic,
            )
            return completed(
                visible_result,
                outcome_kind="check_complete",
                state_changed=False,
                public_metadata={
                    "checkedRoutes": [
                        fact.to_dict() for fact in batch_outcome.checked_routes
                    ],
                    "checkedRouteAliases": checked_route_aliases,
                    "checkedRouteIds": {
                        source_route_ids[source_label]: source_label
                        for source_label in checked_route_aliases.values()
                    },
                },
            )
        try:
            request = CheckRequest.from_arguments(
                arguments,
                decision_id=current.decision_id,
                state_hash=current.state_hash,
            )
        except (TypeError, ValueError) as exc:
            return invalid("INVALID_SEMANTIC_INPUT", str(exc))
        outcome = check_handler.handle(request, CheckPorts.from_context(ctx))
        record_argument_normalizations(outcome.normalization_events)
        ctx["_semantic_last_check_diagnostic"] = outcome.authority_diagnostic
        if not outcome.ok:
            detail = outcome.error_message or "只读校验失败。"
            remember_deterministic_failure([outcome.error_code])
            return invalid(
                outcome.error_code or "SEMANTIC_CHECK_FAILED",
                detail,
                public_evidence=outcome.rendered_result,
            )
        candidates: dict[str, BoundCandidate] = {}
        for candidate in outcome.candidates:
            if candidate.commit_ready is not True:
                return invalid(
                    "INVALID_SEMANTIC_BINDING",
                    "完整校验路线缺少可提交绑定。",
                )
            bound = BoundCandidate.from_dict(
                {
                    "label": candidate.label,
                    "programId": candidate.program_id,
                    "engineSteps": candidate.engine_steps,
                    "canonicalStepsFingerprint": candidate.canonical_steps_fingerprint,
                    "bindingFingerprint": candidate.binding_fingerprint,
                    "intentExact": candidate.intent_exact,
                    "commitReady": candidate.commit_ready,
                    "semanticChain": candidate.semantic_chain,
                },
            )
            if bound is None:
                return invalid(
                    "INVALID_SEMANTIC_BINDING",
                    f"候选 {candidate.label} 无法形成稳定绑定。",
                )
            candidates[bound.label] = bound
        ctx["_semantic_latest_check_route_count"] = len(outcome.checked_routes)
        ctx["_semantic_latest_ready_route_count"] = len(candidates)
        lifecycle = persist_checked_candidates(current, candidates)
        ctx["_semantic_lifecycle_pending_confirmation"] = False
        checked_route_aliases = {
            fact.label: fact.label
            for fact in outcome.checked_routes
            if fact.commit_ready is True
        }
        source_route_ids = {
            fact.label: semantic_route_number(lifecycle, candidates[fact.label])
            for fact in outcome.checked_routes
            if fact.commit_ready is True
        }
        ctx["_semantic_last_check_fingerprint"] = check_fingerprint
        raw_visible_result = (
            compact_result(outcome.checked_routes)
            if descriptor.model_contract.tool_schema == "compact-flat"
            else outcome.rendered_result
        )
        visible_result = finish_check_result(
            raw_visible_result, source_route_ids, outcome.authority_diagnostic,
        )
        return completed(
            visible_result,
            outcome_kind="check_complete",
            state_changed=False,
            public_metadata={
                "checkedRoutes": [fact.to_dict() for fact in outcome.checked_routes],
                "checkedRouteAliases": checked_route_aliases,
                "checkedRouteIds": {
                    source_route_ids[source_label]: source_label
                    for source_label in checked_route_aliases.values()
                },
            },
        )

    def render_commit(outcome: CommitOutcome):
        if outcome.ok:
            ctx["_semantic_reconciliation_required"] = False
            ctx["_semantic_reconciliation_error"] = ""
            lifecycle = load_lifecycle()
            label = "当前路线"
            if lifecycle is not None and lifecycle.commit_fence is not None:
                candidate = lifecycle.candidates[lifecycle.commit_fence.candidate_label]
                label = f"路线 {semantic_route_number(lifecycle, candidate)}"
            if outcome.idempotent:
                return completed(
                    f"{label} 已在本轮提交；重复请求未再次执行。",
                    outcome_kind="commit_complete",
                    state_changed=False,
                    public_metadata={"idempotent": True},
                )
            return completed(
                f"已提交 {label}；当前真实决策已完成。",
                outcome_kind="commit_complete",
                state_changed=True,
            )
        if outcome.fence_status in {
            "authority_committed", "sink_failed", "indeterminate",
        }:
            ctx["_semantic_reconciliation_required"] = True
            ctx["_semantic_reconciliation_error"] = (
                outcome.error_message
                or "commit delivery requires host reconciliation"
            )
            persist()
        return invalid(
            outcome.error_code or "SEMANTIC_COMMIT_FAILED",
            outcome.error_message or "语义提交失败。",
            outcome_kind=(
                "uncertain"
                if outcome.fence_status in {
                    "authority_committed", "sink_failed", "indeterminate",
                }
                else "rejected"
            ),
            state_changed=(
                True
                if outcome.fence_status in {"authority_committed", "sink_failed"}
                else None
                if outcome.fence_status == "indeterminate"
                else False
            ),
        )

    def commit(arguments: Mapping[str, Any]):
        current = identity()
        if current is None:
            return invalid(
                "SEMANTIC_IDENTITY_UNAVAILABLE",
                "当前决策缺少 semantic-v2 身份或 surface。",
            )
        try:
            request = CommitRequest.from_arguments(arguments)
        except (TypeError, ValueError) as exc:
            return invalid("INVALID_SEMANTIC_INPUT", str(exc))
        try:
            lifecycle = load_lifecycle()
        except ValueError as exc:
            return invalid("INVALID_SEMANTIC_BINDING", str(exc))
        try:
            commit_handler = CommitHandler(
                check_handler=check_handler,
                check_ports=CheckPorts.from_context(ctx),
                action_roles=descriptor.action_roles,
            )
            transaction = commit_handler.resolve(request, current, lifecycle)
        except CommitResolutionError as exc:
            return invalid(exc.code, str(exc))

        def mark_submitted(public: Mapping[str, Any]) -> None:
            SubmissionState(ctx).mark_submitted(public, transaction.to_validator_payload())
            persist()

        result = coordinator.commit(
            current,
            transaction,
            CommitPorts(
                validator=(
                    ctx.get("_transaction_validator")
                    if callable(ctx.get("_transaction_validator"))
                    else None
                ),
                sink=(
                    ctx.get("_action_sink")
                    if callable(ctx.get("_action_sink"))
                    else None
                ),
                load_lifecycle=load_lifecycle,
                persist_lifecycle=persist_lifecycle,
                attempt_is_open=(
                    ctx.get("_attempt_guard")
                    if callable(ctx.get("_attempt_guard"))
                    else None
                ),
                mark_submitted=mark_submitted,
            ),
        )
        if inspect.isawaitable(result):
            async def await_commit():
                return render_commit(await result)

            return await_commit()
        return render_commit(result)

    branches = _operation_schema_branches(definition, interaction_contract)
    operations = {
        "check": BgActOperation(
            name="check",
            summary=interaction_contract.check_operation_summary(),
            schema_branch=branches["check"],
            handler=check,
            state_changing=False,
        ),
        "commit": BgActOperation(
            name="commit",
            summary=interaction_contract.commit_operation_summary(),
            schema_branch=branches["commit"],
            handler=commit,
            state_changing=True,
        ),
    }
    full_schema = _schema_from_operations(operations)

    def call(arguments: dict[str, Any]):
        if not isinstance(arguments, dict):
            return invalid("INVALID_SEMANTIC_INPUT", "BgAct 输入必须是对象。")
        arguments = copy.deepcopy(arguments)
        raw_chains = arguments.get("chains")
        if isinstance(raw_chains, str) and len(raw_chains) <= 128_000:
            try:
                decoded_chains = json.loads(raw_chains)
            except json.JSONDecodeError:
                decoded_chains = None
            if isinstance(decoded_chains, list):
                arguments["chains"] = decoded_chains
                ctx.setdefault("_semantic_argument_normalizations", []).append({
                    "field": "chains",
                    "from": "json_string",
                    "to": "array",
                })
        if (
            arguments.get("operation") == "commit"
            and isinstance(arguments.get("chains"), (list, tuple))
        ):
            canonicalization = canonicalize_semantic_payload_input(
                {"chains": arguments["chains"]},
                SemanticSchemaVariant.FLAT,
                descriptor.model_contract,
            )
            if isinstance(canonicalization.payload, Mapping):
                arguments["chains"] = canonicalization.payload.get("chains")
            record_argument_normalizations(canonicalization.events)
        operation = operations.get(arguments.get("operation"))
        if operation is not None:
            try:
                return operation.handler(arguments)
            except concurrent.futures.CancelledError:
                raise
            except Exception as exc:
                if operation.state_changing:
                    # Commit may already have crossed an authority boundary;
                    # let the shared executor stop for reconciliation.
                    raise
                # Check runs only against disposable Authority clones.  An
                # unexpected local failure is retryable and known not to have
                # changed the real game, so it must never be reported as an
                # indeterminate commit outcome.
                logger.exception("Unexpected BgAct Check failure")
                return invalid(
                    "SEMANTIC_VALIDATION_FAILED",
                    str(exc),
                    outcome_kind="infrastructure_failure",
                    state_changed=False,
                )
        return invalid(
            "INVALID_SEMANTIC_OPERATION",
            "operation 必须是 check 或 commit。",
        )

    tool = Tool(
        name="BgAct",
        description=interaction_contract.tool_description(),
        prompt=base_prompt,
        parameters=copy.deepcopy(full_schema),
        call=call,
        is_read_only=not any(
            operation.state_changing for operation in operations.values()
        ),
        is_destructive=False,
        always_load=True,
        should_defer=False,
        is_concurrency_safe=False,
    )
    return tool


__all__ = [
    "BgActOperation",
    "SEMANTIC_CHECK_RESULT_GUIDANCE",
    "SEMANTIC_RESULT_TO_SUBMISSION_BOUNDARY",
    "SEMANTIC_V2_RETRIEVAL_SURFACE_HASH",
    "append_semantic_check_result_guidance",
    "build_semantic_operation_schema",
    "create_semantic_operation_tool",
    "project_current_turn_route_ids",
]
