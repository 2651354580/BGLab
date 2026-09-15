from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator

from bglab.games.semantic_validation import (
    CompilationStatus,
    SemanticDescriptor,
    SemanticPayload,
    SemanticSchemaVariant,
    normalize_semantic_payload,
    render_closest_result,
    search_projected_authority_primary,
)
from bglab.games.semantic_validation.fingerprint import canonical_steps_fingerprint
from bglab.games.semantic_validation.checked_route_fact import (
    CheckedRouteFact,
    build_checked_route_fact,
)
from bglab.games.semantic_validation.render import (
    render_authority_diagnostic,
    render_no_candidate_recovery,
)
from bglab.games.semantic_validation.schema import (
    canonicalize_semantic_payload_input,
)
from bglab.games.semantic_validation.model_contract import ModelActionContract


SINGLE_CHECK_RESULT_LIMIT = 3
BATCH_CHECK_RESULT_LIMIT = 2


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return copy.deepcopy(value)


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return copy.deepcopy(value)


def _contract_hint(descriptor: SemanticDescriptor) -> str:
    actions = []
    for action_name in descriptor.model_action_names:
        definition = descriptor.model_contract.action(action_name)
        fields = ",".join(definition.argument_order) or "无参数"
        actions.append(f"{action_name}({fields})")
    return "可用语义动作与字段：" + "；".join(actions) + "。"


def _field_constraint_detail(path: str, value: Any, error: Any) -> str:
    validator = str(getattr(error, "validator", "schema"))
    limit = getattr(error, "validator_value", None)
    if validator == "maxItems" and isinstance(value, (list, tuple)):
        return f"字段 {path} 最多 {limit} 项，当前为 {len(value)} 项。"
    if validator == "minItems" and isinstance(value, (list, tuple)):
        return f"字段 {path} 至少 {limit} 项，当前为 {len(value)} 项。"
    if validator == "enum" and isinstance(limit, (list, tuple)):
        allowed = json.dumps(list(limit), ensure_ascii=False, separators=(",", ":"))
        return f"字段 {path} 必须取当前 schema 枚举之一：{allowed}。"
    if validator == "type":
        expected = json.dumps(limit, ensure_ascii=False, separators=(",", ":"))
        return f"字段 {path} 类型必须是 {expected}。"
    if validator == "minimum":
        return f"字段 {path} 不得小于 {limit}。"
    if validator == "maximum":
        return f"字段 {path} 不得大于 {limit}。"
    return f"字段 {path} 不符合当前 schema 的 {validator} 约束。"


def _semantic_input_error_detail(
    payload: Any,
    contract: ModelActionContract,
) -> str:
    """Render one safe model-owned field error instead of a generic oneOf dump."""

    if not isinstance(payload, Mapping):
        return "参数根节点必须是对象。"
    chains = payload.get("chains")
    if not isinstance(chains, (list, tuple)):
        return "字段 chains 必须是数组。"
    for chain_index, chain in enumerate(chains):
        if not isinstance(chain, Mapping):
            return f"字段 chains[{chain_index}] 必须是对象。"
        actions = chain.get("actions")
        if not isinstance(actions, (list, tuple)) or not actions:
            return f"字段 chains[{chain_index}].actions 必须是非空数组。"
        for action_index, action in enumerate(actions):
            path = f"chains[{chain_index}].actions[{action_index}]"
            if not isinstance(action, Mapping):
                return f"字段 {path} 必须是对象。"
            action_name = action.get("action")
            if not isinstance(action_name, str) or action_name not in contract.actions:
                allowed = ", ".join(contract.action_names)
                rendered = json.dumps(action_name, ensure_ascii=False)
                return (
                    f"字段 {path}.action 的值 {rendered} 不存在；"
                    f"必须使用当前语义动作之一：{allowed}。"
                )
            definition = contract.action(action_name)
            missing = [field for field in definition.argument_order if field not in action]
            if missing:
                return f"字段 {path} 缺少必填字段 {missing[0]}。"
            allowed_fields = {
                "action",
                *definition.argument_order,
                *definition.implicit_fields,
            }
            unexpected = sorted(str(field) for field in set(action) - allowed_fields)
            if unexpected:
                return f"字段 {path} 不允许包含 {unexpected[0]}。"
            for field, expected in definition.implicit_fields.items():
                if field in action and action[field] != expected:
                    rendered = json.dumps(expected, ensure_ascii=False, separators=(",", ":"))
                    return f"字段 {path}.{field} 是固定值 {rendered}，应省略或原样使用。"
            for field, field_schema in definition.fields.items():
                if field not in action:
                    continue
                error = next(
                    Draft202012Validator(dict(field_schema)).iter_errors(action[field]),
                    None,
                )
                if error is not None:
                    return _field_constraint_detail(
                        f"{path}.{field}",
                        action[field],
                        error,
                    )
    return "参数与当前 action schema 不匹配。"


@dataclass(frozen=True)
class CheckRequest:
    """One explicit check operation after optional facade dispatch."""

    chains: tuple[Mapping[str, Any], ...]
    decision_id: str
    state_hash: str

    @classmethod
    def from_arguments(
        cls,
        arguments: Mapping[str, Any],
        *,
        decision_id: str,
        state_hash: str,
    ) -> CheckRequest:
        if not isinstance(arguments, Mapping):
            raise TypeError("check arguments must be an object")
        operation = arguments.get("operation")
        if operation is not None and operation != "check":
            raise ValueError("check request operation must be check")
        allowed = {"operation", "chains"}
        if set(arguments) - allowed:
            raise ValueError("check request contains fields other than operation and chains")
        chains = arguments.get("chains")
        if not isinstance(chains, (list, tuple)):
            raise ValueError("check request chains must be an array")
        if not isinstance(decision_id, str) or not decision_id:
            raise ValueError("check request decision_id must be non-empty")
        if not isinstance(state_hash, str) or not state_hash:
            raise ValueError("check request state_hash must be non-empty")
        return cls(
            chains=tuple(_freeze_json(chain) for chain in chains),
            decision_id=decision_id,
            state_hash=state_hash,
        )


@dataclass(frozen=True)
class CheckPorts:
    worker_factory: Callable[[], Any] | None
    outcome_renderer: Callable[[Mapping[str, object]], str] | None = None

    @classmethod
    def from_context(cls, context: Mapping[str, Any]) -> CheckPorts:
        """Select only read-only check dependencies from a runtime context."""

        factory = context.get("_semantic_worker_factory")
        renderer = context.get("_semantic_outcome_renderer")
        return cls(
            worker_factory=factory if callable(factory) else None,
            outcome_renderer=renderer if callable(renderer) else None,
        )


@dataclass(frozen=True)
class CheckCandidate:
    label: str
    program_id: str
    engine_steps: tuple[Mapping[str, Any], ...]
    canonical_steps_fingerprint: str
    binding_fingerprint: str
    intent_exact: bool
    commit_ready: bool
    semantic_chain: Mapping[str, Any]


@dataclass(frozen=True)
class CheckOutcome:
    normalized_payload: SemanticPayload | None = None
    normalization_events: tuple[Mapping[str, Any], ...] = ()
    rendered_result: str = ""
    authority_diagnostic: str = ""
    candidates: tuple[CheckCandidate, ...] = ()
    checked_routes: tuple[CheckedRouteFact, ...] = ()
    compilation_status: str | None = None
    compilation_validation_calls: int = 0
    authority_reason: Mapping[str, Any] | None = None
    legal_frontier: tuple[Mapping[str, Any], ...] = ()
    first_unmatched_action: Mapping[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    search_mode: str | None = None
    coverage_status: str | None = None
    enumeration_complete: bool | None = None
    route_count: int = 0
    automaton_state_count: int = 0

    @property
    def ok(self) -> bool:
        return self.error_code is None


class CheckHandler:
    """Normalize and evaluate one semantic route on a read-only worker."""

    def __init__(
        self,
        descriptor: SemanticDescriptor,
        *,
        candidate_limit: int = 5,
        result_limit: int = SINGLE_CHECK_RESULT_LIMIT,
    ) -> None:
        if isinstance(result_limit, bool) or not isinstance(result_limit, int) or not 1 <= result_limit <= 5:
            raise ValueError("Check result limit must be an integer from 1 to 5")
        self._descriptor = descriptor
        self._candidate_limit = candidate_limit
        self._result_limit = result_limit
        self._cached_key: tuple[str, str, SemanticPayload] | None = None
        self._cached_outcome: CheckOutcome | None = None

    def normalize(self, request: CheckRequest) -> CheckOutcome:
        """Validate and canonicalize one chain without creating an authority worker."""

        if not isinstance(request, CheckRequest):
            raise TypeError("request must be CheckRequest")
        derived_actions = {
            action
            for action, role in self._descriptor.action_roles.items()
            if role == "derived"
        }
        submitted_derived = tuple(
            dict.fromkeys(
                str(action.get("action"))
                for chain in request.chains
                if isinstance(chain, Mapping)
                for action in chain.get("actions", ())
                if (
                    isinstance(action, Mapping)
                    and isinstance(action.get("action"), str)
                    and action.get("action") in derived_actions
                )
            )
        )
        if submitted_derived:
            return CheckOutcome(
                error_code="DERIVED_SEMANTIC_ACTION",
                error_message=(
                    f"{', '.join(submitted_derived)} 由引擎根据玩家选择自动推导；"
                    "请从 chains 省略这些动作，只提交模型拥有的选择。"
                ),
            )
        normalization_events: tuple[Mapping[str, Any], ...] = ()
        canonical_payload: Any = {"chains": _thaw_json(request.chains)}
        try:
            canonicalization = canonicalize_semantic_payload_input(
                canonical_payload,
                SemanticSchemaVariant.FLAT,
                self._descriptor.model_contract,
            )
            canonical_payload = canonicalization.payload
            normalization_events = canonicalization.events
            payload = normalize_semantic_payload(
                canonicalization.payload,
                SemanticSchemaVariant.FLAT,
                self._descriptor.model_contract,
                max_chains=1,
                action_names=self._descriptor.model_action_names,
            )
        except (TypeError, ValueError) as exc:
            detail = _semantic_input_error_detail(
                canonical_payload,
                self._descriptor.model_contract,
            )
            return CheckOutcome(
                normalization_events=normalization_events,
                error_code="INVALID_SEMANTIC_INPUT",
                error_message=(
                    f"{detail}\n{exc}\n{_contract_hint(self._descriptor)}"
                ),
            )
        return CheckOutcome(
            normalized_payload=payload,
            normalization_events=normalization_events,
        )

    def handle(self, request: CheckRequest, ports: CheckPorts) -> CheckOutcome:
        normalized = self.normalize(request)
        if not normalized.ok or normalized.normalized_payload is None:
            return normalized
        payload = normalized.normalized_payload

        cache_key = (request.decision_id, request.state_hash, payload)
        if cache_key == self._cached_key and self._cached_outcome is not None:
            if (
                self._cached_outcome.normalization_events
                == normalized.normalization_events
            ):
                return self._cached_outcome
            return replace(
                self._cached_outcome,
                normalization_events=normalized.normalization_events,
            )

        if not callable(ports.worker_factory):
            return CheckOutcome(
                normalized_payload=payload,
                normalization_events=normalized.normalization_events,
                error_code="SEMANTIC_WORKER_UNAVAILABLE",
                error_message="当前决策未连接只读 authority worker。",
            )
        worker = ports.worker_factory()
        if worker is None:
            return CheckOutcome(
                normalized_payload=payload,
                normalization_events=normalized.normalization_events,
                error_code="SEMANTIC_WORKER_UNAVAILABLE",
                error_message="只读 authority worker 创建失败。",
            )
        try:
            envelope = search_projected_authority_primary(
                worker,
                self._descriptor,
                payload.chains[0],
                decision_id=request.decision_id,
                state_hash=request.state_hash,
                candidate_limit=self._candidate_limit,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            return CheckOutcome(
                normalized_payload=payload,
                normalization_events=normalized.normalization_events,
                error_code="SEMANTIC_VALIDATION_FAILED",
                error_message=str(exc),
            )
        finally:
            close = getattr(worker, "close", None)
            if callable(close):
                close()

        # Output width must not change the authority search's prefix scope or
        # ranking. Slice once before both rendering and Commit bindings exist.
        if len(envelope.search.candidates) > self._result_limit:
            envelope = replace(
                envelope,
                search=replace(
                    envelope.search,
                    candidates=envelope.search.candidates[:self._result_limit],
                ),
            )

        # A validated exact route takes precedence over an incomplete compiler
        # attempt. Otherwise expose why the submitted prefix needed changing.
        diagnostic = (
            render_authority_diagnostic(envelope.compilation, self._descriptor)
            if envelope.compilation is not None
            and envelope.compilation.status is CompilationStatus.ILLEGAL
            and not any(candidate.intent_exact for candidate in envelope.search.candidates)
            else ""
        )
        if not envelope.search.candidates:
            rendered = render_no_candidate_recovery(
                envelope.search,
                envelope.compilation,
                coverage_status=getattr(
                    getattr(envelope, "authority", None),
                    "coverage_status",
                    None,
                ),
                enumeration_complete=getattr(
                    getattr(envelope, "authority", None),
                    "enumeration_complete",
                    None,
                ),
                search_mode=getattr(envelope, "search_mode", None),
                descriptor=self._descriptor,
            )
            return CheckOutcome(
                normalized_payload=payload,
                normalization_events=normalized.normalization_events,
                rendered_result=rendered,
                authority_diagnostic=diagnostic,
                compilation_status=(
                    envelope.compilation.status.value
                    if envelope.compilation is not None
                    else None
                ),
                compilation_validation_calls=(
                    envelope.compilation.validation_calls
                    if envelope.compilation is not None
                    else 0
                ),
                authority_reason=(
                    _freeze_json(envelope.compilation.authority_reason)
                    if envelope.compilation is not None
                    and envelope.compilation.authority_reason is not None
                    else None
                ),
                legal_frontier=(
                    tuple(
                        _freeze_json(step)
                        for step in envelope.compilation.legal_frontier
                    )
                    if envelope.compilation is not None
                    else ()
                ),
                first_unmatched_action=(
                    _freeze_json(envelope.compilation.first_unmatched_action.to_dict())
                    if envelope.compilation is not None
                    and envelope.compilation.first_unmatched_action is not None
                    else None
                ),
                error_code="NO_SEMANTIC_CANDIDATES",
                error_message="当前 authority 没有返回完整合法候选。",
                search_mode=envelope.search_mode,
                coverage_status=envelope.authority.coverage_status,
                enumeration_complete=envelope.authority.enumeration_complete,
                route_count=getattr(envelope.search, "route_count", 0),
                automaton_state_count=getattr(
                    envelope.search,
                    "automaton_state_count",
                    0,
                ),
            )

        candidates: list[CheckCandidate] = []
        checked_routes: list[CheckedRouteFact] = []
        for candidate in envelope.search.candidates:
            engine_steps = tuple(
                _freeze_json(step) for step in candidate.program.engine_steps
            )
            thawed_steps = [_thaw_json(step) for step in engine_steps]
            check_candidate = CheckCandidate(
                label=candidate.label,
                program_id=candidate.program.program_id,
                engine_steps=engine_steps,
                canonical_steps_fingerprint=canonical_steps_fingerprint(
                    thawed_steps
                ),
                binding_fingerprint=candidate.binding_fingerprint,
                intent_exact=candidate.intent_exact,
                commit_ready=candidate.commit_ready,
                semantic_chain=_freeze_json(
                    {
                        "name": candidate.program.chain.name,
                        "actions": [
                            action.to_dict()
                            for action in candidate.program.chain.actions
                        ],
                    }
                ),
            )
            candidates.append(check_candidate)
            fact = build_checked_route_fact(
                submitted=envelope.search.submitted,
                candidate=candidate,
                action_roles=self._descriptor.action_roles,
                authority_reason=(
                    envelope.compilation.authority_reason
                    if envelope.compilation is not None
                    else None
                ),
            )
            if (
                fact.label != check_candidate.label
                or fact.intent_exact is not check_candidate.intent_exact
                or fact.commit_ready is not check_candidate.commit_ready
            ):
                raise RuntimeError("checked-route fact drifted from commit binding")
            checked_routes.append(fact)

        frozen_checked_routes = tuple(checked_routes)
        rendered = render_closest_result(
            envelope.search,
            outcome_renderer=(
                (lambda outcome: ports.outcome_renderer(_thaw_json(outcome)))
                if ports.outcome_renderer is not None
                else None
            ),
            action_roles=self._descriptor.action_roles,
            checked_routes=frozen_checked_routes,
            choice_value_labels=self._descriptor.choice_value_labels,
        )
        outcome = CheckOutcome(
            normalized_payload=payload,
            normalization_events=normalized.normalization_events,
            rendered_result=rendered,
            authority_diagnostic=diagnostic,
            candidates=tuple(candidates),
            checked_routes=frozen_checked_routes,
            compilation_status=(
                envelope.compilation.status.value
                if envelope.compilation is not None
                else None
            ),
            compilation_validation_calls=(
                envelope.compilation.validation_calls
                if envelope.compilation is not None
                else 0
            ),
            authority_reason=(
                _freeze_json(envelope.compilation.authority_reason)
                if envelope.compilation is not None
                and envelope.compilation.authority_reason is not None
                else None
            ),
            legal_frontier=(
                tuple(
                    _freeze_json(step)
                    for step in envelope.compilation.legal_frontier
                )
                if envelope.compilation is not None
                else ()
            ),
            first_unmatched_action=(
                _freeze_json(envelope.compilation.first_unmatched_action.to_dict())
                if envelope.compilation is not None
                and envelope.compilation.first_unmatched_action is not None
                else None
            ),
            search_mode=envelope.search_mode,
            coverage_status=envelope.authority.coverage_status,
            enumeration_complete=envelope.authority.enumeration_complete,
            route_count=getattr(envelope.search, "route_count", 0),
            automaton_state_count=getattr(
                envelope.search,
                "automaton_state_count",
                0,
            ),
        )
        self._cached_key = cache_key
        self._cached_outcome = outcome
        return outcome


__all__ = [
    "CheckCandidate",
    "CheckHandler",
    "CheckOutcome",
    "CheckPorts",
    "CheckRequest",
]
