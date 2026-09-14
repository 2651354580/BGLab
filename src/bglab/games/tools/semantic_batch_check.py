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
    unique_routes: list[BatchCheckRouteOutcome] = []
    convergence: dict[int, list[int]] = {}
    sources: dict[int, list[tuple[int, CheckedRouteFact]]] = {}
    seen_routes: dict[str, int] = {}
    for route in route_outcomes:
        if not route.outcome.checked_routes:
            unique_routes.append(route)
            continue
        fact = route.outcome.checked_routes[0]
        fingerprint = json.dumps(
            _thaw_json(fact.semantic_chain),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        existing = seen_routes.get(fingerprint)
        if existing is not None:
            sources[existing].append((route.route_index, fact))
            convergence.setdefault(existing, [unique_routes[existing].route_index]).append(
                route.route_index,
            )
            continue
        seen_routes[fingerprint] = len(unique_routes)
        sources[len(unique_routes)] = [(route.route_index, fact)]
        unique_routes.append(route)
    sections.extend(
        _render_route(route, choice_value_labels, outcome_renderer, {
            route.outcome.checked_routes[0].label: sources[index],
        } if route.outcome.checked_routes else None)
        for index, route in enumerate(unique_routes)
    )
    sections.extend(
        "输入路线 " + "、".join(str(index) for index in indexes)
        + " 收敛为上方同一条完整权威路线，只保留一个短 ID。"
        for unique_index, indexes in convergence.items()
        if unique_routes[unique_index].outcome.checked_routes
    )
    valid_routes = [
        route
        for route in unique_routes
        if route.outcome.checked_routes
    ]
    ready_refs = [f"R{route.route_index}.C1" for route in valid_routes]
    failed_count = sum(not route.outcome.ok for route in route_outcomes)
    binding_note = (
        "可提交显示路线：" + "、".join(ready_refs) + "。"
        if ready_refs
        else "当前各组都没有可提交显示路线。"
    )
    partial_note = (
        f"其中 {failed_count} 个路线组参数或语义无效；它们没有标签，也不会使其他"
        "已显示路线失效。"
        if failed_count
        else "所有路线组均已独立校验。"
    )
    sections.extend(
        [
            "候选接近度不代表策略优劣。",
            partial_note,
            binding_note,
            "组号和参考号不能作为 commit.id。",
        ]
    )
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
            f"输入路线 {route.route_index} 无效：{public.code}；"
            f"{public.message}{detail}"
        )
    if lines:
        lines.append(
            "若仍想比较这些路线，只修正上述字段后重新 Check；"
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
                outcome = CheckHandler(self._descriptor).handle(single_request, ports)
                # A batch already represents the model's two or three competing
                # routes.  Returning up to five repairs for every route creates
                # as many as fifteen model-visible choices and exceeds the shared
                # Tool-result budget.  Preserve the closest Authority result for
                # each submitted route; single-route Check retains its C1-C5
                # repair surface.
                if outcome.candidates or outcome.checked_routes:
                    outcome = replace(
                        outcome,
                        candidates=outcome.candidates[:1],
                        checked_routes=outcome.checked_routes[:1],
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
        if len(checked_routes) > 3:
            raise RuntimeError(
                "batch check produced more than one checked route per input route"
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
