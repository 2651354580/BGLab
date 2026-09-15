from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from bglab.games.semantic_validation import SemanticDescriptor
from bglab.games.semantic_validation.checked_route_fact import CheckedRouteFact
from bglab.games.semantic_validation.render import render_checked_route_facts
from bglab.games.tools.semantic_check import (
    BATCH_CHECK_RESULT_LIMIT,
    CheckHandler,
    CheckOutcome,
    CheckPorts,
    CheckRequest,
)
from bglab.games.tools.public_error import render_public_bgact_error


_MODEL_HIDDEN_MARKERS = (
    "candidatetoken",
    "parentcandidatetoken",
    "bindingfingerprint",
    "programid",
    "effectid",
    "statehash",
    "decisionid",
    '"candidate"',
)


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


@dataclass(frozen=True)
class BatchCheckRequest:
    """Two or three independent semantic routes for read-only comparison."""

    chains: tuple[Any, ...]
    decision_id: str
    state_hash: str

    @classmethod
    def from_arguments(
        cls,
        arguments: Mapping[str, Any],
        *,
        decision_id: str,
        state_hash: str,
    ) -> BatchCheckRequest:
        if not isinstance(arguments, Mapping):
            raise TypeError("batch check arguments must be an object")
        if arguments.get("operation") != "check":
            raise ValueError("batch check request operation must be check")
        if set(arguments) - {"operation", "chains"}:
            raise ValueError(
                "batch check request contains fields other than operation and chains"
            )
        chains = arguments.get("chains")
        if not isinstance(chains, (list, tuple)):
            raise ValueError("batch check request chains must be an array")
        if not 2 <= len(chains) <= 3:
            raise ValueError("batch check request requires 2 to 3 chains")
        fingerprints = [
            json.dumps(
                chain.get("actions") if isinstance(chain, Mapping) else chain,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for chain in chains
        ]
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("batch check routes must be distinct")
        if not isinstance(decision_id, str) or not decision_id:
            raise ValueError("batch check request decision_id must be non-empty")
        if not isinstance(state_hash, str) or not state_hash:
            raise ValueError("batch check request state_hash must be non-empty")
        return cls(
            chains=tuple(_freeze_json(chain) for chain in chains),
            decision_id=decision_id,
            state_hash=state_hash,
        )


@dataclass(frozen=True)
class BatchCheckRouteOutcome:
    route_index: int
    route_name: str | None
    outcome: CheckOutcome


@dataclass(frozen=True)
class BatchCheckOutcome:
    route_outcomes: tuple[BatchCheckRouteOutcome, ...]
    rendered_result: str
    checked_routes: tuple[CheckedRouteFact, ...] = ()
    normalization_events: tuple[Mapping[str, Any], ...] = ()
    error_code: str | None = None
    error_message: str | None = None

    @property
    def ok(self) -> bool:
        return self.error_code is None and all(
            route.outcome.ok for route in self.route_outcomes
        )


def _model_safe_text(value: str) -> str:
    return "\n".join(
        line
        for line in value.splitlines()
        if not any(marker in line.lower() for marker in _MODEL_HIDDEN_MARKERS)
    )


def _render_route(
    route: BatchCheckRouteOutcome,
    choice_value_labels: Mapping[str, Mapping[str, str]] | None = None,
    outcome_renderer: Callable[[Mapping[str, object]], str] | None = None,
    source_facts: Mapping[str, Sequence[tuple[int, CheckedRouteFact]]] | None = None,
) -> str:
    lines = [f"## 输入路线 {route.route_index}"]
    if route.outcome.checked_routes:
        lines.append(render_checked_route_facts(
            route.outcome.checked_routes,
            outcome_renderer=outcome_renderer,
            choice_value_labels=choice_value_labels,
            source_facts=source_facts,
        ))
    elif route.outcome.rendered_result:
        lines.append(_model_safe_text(route.outcome.rendered_result).strip())
    if not route.outcome.ok:
        public = render_public_bgact_error(
            route.outcome.error_code or "SEMANTIC_CHECK_FAILED",
            route.outcome.error_message or "",
        )
        lines.append(f"组内错误：{public.code}；{public.message}")
    return "\n".join(lines)


def _render_batch(
    route_outcomes: tuple[BatchCheckRouteOutcome, ...],
    choice_value_labels: Mapping[str, Mapping[str, str]] | None = None,
    outcome_renderer: Callable[[Mapping[str, object]], str] | None = None,
) -> str:
    sections = ["多路线只读校验结果："]
    unique_routes = []
    owners = {}
    sources = {}
    convergence = {}
    for route in route_outcomes:
        unique_facts = []
        for fact in route.outcome.checked_routes:
            fingerprint = json.dumps(_thaw_json(fact.semantic_chain), ensure_ascii=False, sort_keys=True)
            owner = owners.get(fingerprint)
            if owner is None:
                owner = fact.label
                owners[fingerprint] = owner
                unique_facts.append(fact)
            else:
                if any(index != route.route_index for index, _ in sources[owner]):
                    convergence.setdefault(owner, set()).add(route.route_index)
            if not any(index == route.route_index for index, _ in sources.get(owner, [])):
                sources.setdefault(owner, []).append((route.route_index, fact))
        if unique_facts or not route.outcome.checked_routes:
            unique_routes.append(replace(route, outcome=replace(route.outcome, checked_routes=tuple(unique_facts))))
    sections.extend(_render_route(route, choice_value_labels, outcome_renderer, sources) for route in unique_routes)
    for label, indexes in convergence.items():
        indexes.update(index for index, _ in sources[label])
        sections.append("输入路线 " + "、".join(str(index) for index in sorted(indexes))
                        + " 收敛为上方同一条完整权威路线，共用一个编号。")
    ready_refs = [fact.label for route in unique_routes for fact in route.outcome.checked_routes]
    failed_count = sum(not route.outcome.ok for route in route_outcomes)
    sections.extend([
        "候选接近度不代表策略优劣。",
        f"其中 {failed_count} 个路线组参数或语义无效；其余已显示路线仍有效。" if failed_count else "所有路线组均已独立校验。",
        "可提交显示路线：" + "、".join(ready_refs) + "。" if ready_refs else "当前各组都没有可提交显示路线。",
        "组号和参考号不能作为 commit.id。",
    ])
    return "\n\n".join(sections)


def render_compact_batch_failures(
    route_outcomes: tuple[BatchCheckRouteOutcome, ...],
) -> str:
    """Keep failed input groups actionable beside a compact success result."""

    lines: list[str] = []
    for route in route_outcomes:
        if route.outcome.ok:
            continue
        public = render_public_bgact_error(
            route.outcome.error_code or "SEMANTIC_CHECK_FAILED",
            route.outcome.error_message or "",
        )
        detail = f"；{public.detail}" if public.detail else ""
        lines.append(
            f"输入草稿 {route.route_index} 无效：{public.code}；"
            f"{public.message}{detail}"
        )
    if lines:
        lines.append(
            "上述输入草稿序号不是可提交编号，其他已返回候选仍有效。"
            "若仍想比较这些草稿，只修正上述字段后重新 Check；"
            "不要重算已经成功返回的路线。"
        )
    return "\n".join(lines)


class BatchCheckHandler:
    """Compose independent single-route checks without creating bindings."""

    def __init__(self, descriptor: SemanticDescriptor) -> None:
        self._descriptor = descriptor

    def handle(
        self,
        request: BatchCheckRequest,
        ports: CheckPorts,
    ) -> BatchCheckOutcome:
        def evaluate_route(
            route_index: int,
            chain: Any,
        ) -> BatchCheckRouteOutcome:
            try:
                single_request = CheckRequest.from_arguments(
                    {"operation": "check", "chains": [_thaw_json(chain)]},
                    decision_id=request.decision_id,
                    state_hash=request.state_hash,
                )
                outcome = CheckHandler(
                    self._descriptor,
                    candidate_limit=3,
                    result_limit=BATCH_CHECK_RESULT_LIMIT,
                ).handle(single_request, ports)
                # Keep the existing search scope; expose two results per input.
                if outcome.candidates or outcome.checked_routes:
                    outcome = replace(
                        outcome,
                        candidates=outcome.candidates[:BATCH_CHECK_RESULT_LIMIT],
                        checked_routes=outcome.checked_routes[:BATCH_CHECK_RESULT_LIMIT],
                    )
                if outcome.normalization_events:
                    outcome = replace(
                        outcome,
                        normalization_events=tuple(
                            MappingProxyType({
                                **dict(event),
                                "chainIndex": route_index - 1,
                            })
                            for event in outcome.normalization_events
                        ),
                    )
                if outcome.checked_routes:
                    outcome = replace(
                        outcome,
                        checked_routes=tuple(
                            fact.with_label(f"R{route_index}.{fact.label}")
                            for fact in outcome.checked_routes
                        ),
                    )
            except Exception as exc:
                outcome = CheckOutcome(
                    error_code="SEMANTIC_BATCH_ROUTE_FAILED",
                    error_message=str(exc),
                )
            return BatchCheckRouteOutcome(
                route_index=route_index,
                route_name=None,
                outcome=outcome,
            )

        with ThreadPoolExecutor(
            # Concurrent Node authority explorers can each hit their bounded
            # graph deadline under CPU contention, causing a route that
            # completes alone to be reported as inconclusive.  Batch size is at
            # most three; evaluate those independent clones serially so a
            # timing race cannot change the legal candidate surface.
            max_workers=1,
            thread_name_prefix="bgact-batch-check",
        ) as executor:
            futures = [
                executor.submit(evaluate_route, route_index, chain)
                for route_index, chain in enumerate(request.chains, start=1)
            ]
            route_outcomes = [future.result() for future in futures]

        frozen_outcomes = tuple(route_outcomes)
        checked_routes = tuple(
            fact
            for route in frozen_outcomes
            for fact in route.outcome.checked_routes
        )
        if len(checked_routes) > BATCH_CHECK_RESULT_LIMIT * len(request.chains):
            raise RuntimeError(
                "batch check exceeded its checked-route result limit per input"
            )
        failed = [route for route in frozen_outcomes if not route.outcome.ok]
        return BatchCheckOutcome(
            route_outcomes=frozen_outcomes,
            rendered_result=_render_batch(
                frozen_outcomes,
                self._descriptor.choice_value_labels,
            ),
            checked_routes=checked_routes,
            normalization_events=tuple(
                event
                for route in frozen_outcomes
                for event in route.outcome.normalization_events
            ),
            error_code="BATCH_CHECK_FAILED" if failed else None,
            error_message=(
                f"{len(failed)} 个路线组校验失败；其余组证据仍保留。"
                if failed
                else None
            ),
        )


__all__ = [
    "BatchCheckHandler",
    "BatchCheckOutcome",
    "BatchCheckRequest",
    "BatchCheckRouteOutcome",
    "render_compact_batch_failures",
]
