from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping

from .closest import (
    ClosestSearchResult,
    ProjectedProgram,
    _freeze_json,
    find_closest_programs,
    find_closest_routes,
)
from .compiler import (
    AuthorityCompilation,
    CompilationStatus,
    compile_semantic_chain,
    matched_semantic_prefix_length,
    reorder_legal_semantic_actions,
    semantic_intent_facts,
    semantic_prefix_fact_count,
)
from .descriptor import (
    SemanticDescriptor,
    project_authority_program,
    project_frontier_semantic_actions,
    semantic_chain_to_authority_query,
)
from .model import SemanticAction, SemanticChain
from .route_automaton import RouteAutomaton


_COVERAGE_PRIORITY = {
    "not_explored": 0,
    "bounded": 1,
    "unknown": 2,
    "complete": 3,
}


@dataclass(frozen=True)
class ProjectedAuthoritySet:
    programs: tuple[ProjectedProgram, ...]
    coverage_status: str
    enumeration_complete: bool
    pages: int
    unmapped_programs: int = 0


@dataclass(frozen=True)
class AuthorityClosestEnvelope:
    search: ClosestSearchResult
    authority: ProjectedAuthoritySet
    requested_facts: int
    used_facts: int
    relaxed_facts: tuple[Mapping[str, Any], ...]
    search_mode: str = "exact"
    relaxed_action_indexes: tuple[int, ...] = ()
    compilation: AuthorityCompilation | None = None


def _weaker_coverage(current: str, candidate: str) -> str:
    normalized = candidate if candidate in _COVERAGE_PRIORITY else "unknown"
    return min((current, normalized), key=lambda item: _COVERAGE_PRIORITY[item])


def _with_additional_programs(
    authority: ProjectedAuthoritySet,
    programs: tuple[ProjectedProgram, ...],
) -> ProjectedAuthoritySet:
    """Add independently validated routes without weakening enumeration coverage."""

    if not programs:
        return authority
    merged: dict[str, ProjectedProgram] = {
        program.program_id: program for program in programs
    }
    for program in authority.programs:
        merged.setdefault(program.program_id, program)
    return ProjectedAuthoritySet(
        programs=tuple(merged.values()),
        coverage_status=authority.coverage_status,
        enumeration_complete=authority.enumeration_complete,
        pages=authority.pages,
        unmapped_programs=authority.unmapped_programs,
    )


def _combine_authority_sets(
    *sets: ProjectedAuthoritySet,
) -> ProjectedAuthoritySet:
    if not sets:
        return ProjectedAuthoritySet(
            programs=(),
            coverage_status="not_explored",
            enumeration_complete=False,
            pages=0,
        )
    programs: dict[str, ProjectedProgram] = {}
    coverage = "complete"
    for authority in sets:
        for program in authority.programs:
            programs.setdefault(program.program_id, program)
        coverage = _weaker_coverage(coverage, authority.coverage_status)
    return ProjectedAuthoritySet(
        programs=tuple(programs.values()),
        coverage_status=coverage,
        enumeration_complete=all(
            authority.enumeration_complete for authority in sets
        ),
        pages=sum(authority.pages for authority in sets),
        unmapped_programs=sum(authority.unmapped_programs for authority in sets),
    )


def _evidence_scoped_authority(
    worker: Any,
    descriptor: SemanticDescriptor,
    submitted: SemanticChain,
    *,
    suspect_action_index: int | None,
    page_size: int,
    max_pages: int,
) -> ProjectedAuthoritySet:
    """Retrieve legal routes by submitted facts without using them as rank weights."""

    queries: list[list[dict[str, Any]]] = []

    def add_query(chain: SemanticChain, *, relaxed: frozenset[int] = frozenset()) -> None:
        facts = [
            copy.deepcopy(dict(fact))
            for fact in semantic_intent_facts(
                descriptor,
                chain,
                relaxed_action_indexes=relaxed,
            )
        ]
        if not facts:
            facts = semantic_chain_to_authority_query(
                descriptor,
                chain,
                relaxed_action_indexes=relaxed,
            )["stepsContain"]
        if facts and facts not in queries:
            queries.append(facts)

    add_query(submitted)
    if suspect_action_index is not None:
        add_query(
            submitted,
            relaxed=frozenset({suspect_action_index}),
        )
        if len(submitted.actions) > 1:
            add_query(SemanticChain(
                name=submitted.name,
                actions=(
                    *submitted.actions[:suspect_action_index],
                    *submitted.actions[suspect_action_index + 1:],
                ),
            ))
    unique_actions = tuple(dict.fromkeys(submitted.actions))
    if len(unique_actions) != len(submitted.actions):
        add_query(SemanticChain(name=submitted.name, actions=unique_actions))

    found: list[ProjectedAuthoritySet] = []
    for facts in queries:
        authority = collect_projected_programs(
            worker,
            descriptor,
            page_size=page_size,
            max_pages=max_pages,
            enumeration_request={
                "stepsContain": copy.deepcopy(facts),
                "coverageTarget": True,
            },
            allow_truncated=True,
        )
        if authority.programs:
            found.append(authority)
        if sum(len(item.programs) for item in found) >= 5:
            break
    return _combine_authority_sets(*found)


def _field_neighbor_values(
    schema: Mapping[str, Any],
    current: Any,
) -> tuple[Any, ...]:
    values: list[Any] = []
    enum = schema.get("enum")
    if isinstance(enum, list):
        values.extend(enum)
    types = schema.get("type")
    accepted = {types} if isinstance(types, str) else set(types or ())
    if "integer" in accepted:
        minimum = schema.get("minimum", 0)
        maximum = schema.get("maximum", max(3, int(current) if isinstance(current, int) else 3))
        if isinstance(minimum, int) and isinstance(maximum, int):
            values.extend(range(minimum, min(maximum, minimum + 4) + 1))
    if "boolean" in accepted:
        values.extend((False, True))
    if "null" in accepted:
        values.append(None)
    return tuple(
        value for value in dict.fromkeys(values)
        if type(value) is not type(current) or value != current
    )


def _compile_nearby_routes(
    worker: Any,
    descriptor: SemanticDescriptor,
    submitted: SemanticChain,
    compilation: AuthorityCompilation,
    *,
    decision_id: str,
    state_hash: str,
    max_validation_calls: int,
    max_attempts: int = 64,
) -> tuple[ProjectedProgram, ...]:
    """Validate a bounded one-edit JSON neighborhood before graph enumeration."""

    programs: dict[tuple[str, str], ProjectedProgram] = {}
    attempts = 0

    def try_chain(chain: SemanticChain) -> AuthorityCompilation | None:
        nonlocal attempts
        if attempts >= max_attempts:
            return None
        attempts += 1
        result = compile_semantic_chain(
            worker,
            descriptor,
            chain,
            decision_id=decision_id,
            state_hash=state_hash,
            max_validation_calls=min(32, max_validation_calls),
        )
        for program in (
            (result.program, *result.alternative_programs)
            if result.program is not None
            else ()
        ):
            programs.setdefault(
                (program.semantic_signature, program.outcome_key),
                program,
            )
        return result

    suspect = (
        min(
            compilation.reorder_action_index
            if compilation.reorder_action_index is not None
            else matched_semantic_prefix_length(
                descriptor,
                submitted,
                compilation.matched_facts,
            ),
            len(submitted.actions) - 1,
        )
        if submitted.actions
        else None
    )

    seen_orders = {submitted.actions}
    reorder_frontier = [(submitted, compilation)]
    reorder_attempt_limit = min(16, max_attempts)
    for _depth in range(len(submitted.actions)):
        scored: list[tuple[tuple[int, int, int], SemanticChain, AuthorityCompilation]] = []
        for working, current in reorder_frontier:
            for alternative in reorder_legal_semantic_actions(
                descriptor,
                working,
                current,
            ):
                if alternative.actions in seen_orders:
                    continue
                seen_orders.add(alternative.actions)
                result = try_chain(alternative)
                if result is None:
                    break
                score = (
                    result.matched_facts,
                    result.reorder_action_index or 0,
                    -result.validation_calls,
                )
                scored.append((score, alternative, result))
                if attempts >= reorder_attempt_limit:
                    break
            if attempts >= reorder_attempt_limit:
                break
        if programs or not scored or attempts >= reorder_attempt_limit:
            break
        scored.sort(key=lambda item: item[0], reverse=True)
        reorder_frontier = [
            (chain, result) for _score, chain, result in scored[:6]
        ]

    # Repair consecutive model-owned JSON mistakes one authority frontier at a
    # time. Each step recompiles the whole route and only advances when the
    # validated semantic prefix grows; this is bounded correction, not route
    # generation or strategy search.
    repair_seen = {submitted.actions}
    repair_frontier = [(submitted, compilation)]
    for _depth in range(min(6, len(submitted.actions) + 1)):
        repaired: list[
            tuple[tuple[int, int, int], SemanticChain, AuthorityCompilation]
        ] = []
        for working, current in repair_frontier:
            prefix = matched_semantic_prefix_length(
                descriptor,
                working,
                current.matched_facts,
            )
            current_suspect = (
                min(
                    current.reorder_action_index
                    if current.reorder_action_index is not None
                    else prefix,
                    len(working.actions) - 1,
                )
                if working.actions
                else None
            )
            variants: list[SemanticChain] = []
            dead_end_suffix = bool(
                current.program is None
                and current.matched_facts == current.requested_facts
                and not current.legal_frontier
            )
            for frontier_action in project_frontier_semantic_actions(
                descriptor,
                current.legal_frontier,
            ):
                for insertion_index in range(
                    min(prefix, len(working.actions)),
                    -1,
                    -1,
                ):
                    variants.append(SemanticChain(
                        name=working.name,
                        actions=(
                            *working.actions[:insertion_index],
                            frontier_action,
                        ),
                    ))
                inserted = list(working.actions)
                inserted.insert(prefix, frontier_action)
                variants.append(SemanticChain(
                    name=working.name,
                    actions=tuple(inserted),
                ))
                if current_suspect is not None:
                    replaced = list(working.actions)
                    replaced[current_suspect] = frontier_action
                    variants.append(SemanticChain(
                        name=working.name,
                        actions=tuple(replaced),
                    ))
            if dead_end_suffix and current_suspect is not None and current_suspect > 0:
                variants.append(SemanticChain(
                    name=working.name,
                    actions=working.actions[:current_suspect],
                ))
            elif current_suspect is not None:
                suspect_action = working.actions[current_suspect]
                if current_suspect > 0:
                    variants.append(SemanticChain(
                        name=working.name,
                        actions=working.actions[:current_suspect],
                    ))
                if len(working.actions) > 1:
                    variants.append(SemanticChain(
                        name=working.name,
                        actions=(
                            *working.actions[:current_suspect],
                            *working.actions[current_suspect + 1:],
                        ),
                    ))
                definition = descriptor.model_contract.action(
                    suspect_action.action,
                )
                arguments = dict(suspect_action.args)
                for field, field_schema in definition.fields.items():
                    if field not in arguments:
                        continue
                    for value in _field_neighbor_values(
                        field_schema,
                        arguments[field],
                    ):
                        changed = dict(arguments)
                        changed[field] = value
                        actions = list(working.actions)
                        actions[current_suspect] = SemanticAction.from_mapping(
                            suspect_action.action,
                            changed,
                        )
                        variants.append(SemanticChain(
                            name=working.name,
                            actions=tuple(actions),
                        ))
            for variant in variants:
                if variant.actions in repair_seen:
                    continue
                repair_seen.add(variant.actions)
                result = try_chain(variant)
                if result is None:
                    break
                repaired.append((
                    (
                        matched_semantic_prefix_length(
                            descriptor,
                            variant,
                            result.matched_facts,
                        ),
                        result.matched_facts,
                        -result.validation_calls,
                    ),
                    variant,
                    result,
                ))
                if attempts >= max_attempts:
                    break
            if attempts >= max_attempts:
                break
        if programs:
            return tuple(programs.values())
        if not repaired or attempts >= max_attempts:
            break
        repaired.sort(key=lambda item: item[0], reverse=True)
        repair_frontier = [
            (chain, result) for _score, chain, result in repaired[:6]
        ]

    # If Authority accepted every submitted semantic fact but is waiting for
    # one explicit zero-argument player decision (most notably
    # ``finish_action``), test that bounded insertion before falling back to a
    # broad graph search.  This preserves the model's selected root and full
    # action set instead of returning a different route that merely avoids the
    # omitted closing choice.
    if compilation.matched_facts == compilation.requested_facts:
        present = set(submitted.actions)
        for action_name in descriptor.model_action_names:
            definition = descriptor.model_contract.action(action_name)
            if definition.argument_order:
                continue
            candidate_action = SemanticAction.from_mapping(action_name, {})
            if candidate_action in present:
                continue
            try_chain(SemanticChain(
                name=submitted.name,
                actions=(*submitted.actions, candidate_action),
            ))
            if len(programs) >= 5 or attempts >= max_attempts:
                return tuple(programs.values())

    semantic_prefix = matched_semantic_prefix_length(
        descriptor,
        submitted,
        compilation.matched_facts,
    )
    for frontier_action in project_frontier_semantic_actions(
        descriptor,
        compilation.legal_frontier,
    ):
        inserted = list(submitted.actions)
        inserted.insert(semantic_prefix, frontier_action)
        try_chain(SemanticChain(name=submitted.name, actions=tuple(inserted)))
        if suspect is not None and suspect < len(submitted.actions):
            replaced = list(submitted.actions)
            replaced[suspect] = frontier_action
            try_chain(SemanticChain(name=submitted.name, actions=tuple(replaced)))
        if len(programs) >= 5 or attempts >= max_attempts:
            return tuple(programs.values())

    if suspect is not None and len(submitted.actions) > 1:
        try_chain(SemanticChain(
            name=submitted.name,
            actions=(
                *submitted.actions[:suspect],
                *submitted.actions[suspect + 1:],
            ),
        ))

    action_indexes = list(range(len(submitted.actions)))
    if suspect is not None and suspect in action_indexes:
        action_indexes.remove(suspect)
        action_indexes.insert(0, suspect)
    for action_index in action_indexes:
        action = submitted.actions[action_index]
        definition = descriptor.model_contract.action(action.action)
        arguments = dict(action.args)
        for field, field_schema in definition.fields.items():
            if field not in arguments:
                continue
            for value in _field_neighbor_values(field_schema, arguments[field]):
                changed = dict(arguments)
                changed[field] = value
                actions = list(submitted.actions)
                actions[action_index] = SemanticAction.from_mapping(
                    action.action,
                    changed,
                )
                try_chain(SemanticChain(
                    name=submitted.name,
                    actions=tuple(actions),
                ))
                if len(programs) >= 5 or attempts >= max_attempts:
                    return tuple(programs.values())

    return tuple(programs.values())


def _bounded_repair_envelope(
    descriptor: SemanticDescriptor,
    submitted: SemanticChain,
    programs: tuple[ProjectedProgram, ...],
    compilation: AuthorityCompilation,
    *,
    decision_id: str,
    state_hash: str,
    search_mode: str,
) -> AuthorityClosestEnvelope:
    """Return only fully Authority-validated local repairs."""

    authority = ProjectedAuthoritySet(
        programs=programs,
        coverage_status="bounded",
        enumeration_complete=False,
        pages=0,
    )
    return AuthorityClosestEnvelope(
        search=find_closest_programs(
            submitted,
            authority.programs,
            decision_id=decision_id,
            state_hash=state_hash,
            action_roles=descriptor.action_roles,
        ),
        authority=authority,
        requested_facts=compilation.requested_facts,
        used_facts=compilation.matched_facts,
        relaxed_facts=(),
        search_mode=search_mode,
        compilation=compilation,
    )


def collect_projected_programs(
    worker: Any,
    descriptor: SemanticDescriptor,
    *,
    page_size: int = 20,
    max_pages: int = 20,
    enumeration_request: dict[str, Any] | None = None,
    allow_truncated: bool = False,
    require_complete: bool = False,
) -> ProjectedAuthoritySet:
    request = copy.deepcopy(enumeration_request) if enumeration_request is not None else {"requirements": []}
    search_projection = (
        isinstance(request, dict)
        and request.get("projection") == "semantic-search"
    )
    max_page_size = 10_000 if search_projection else 100
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= max_page_size
    ):
        raise ValueError(
            "semantic authority enumeration page_size must be from 1 to "
            f"{max_page_size}"
        )
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 100:
        raise ValueError("semantic authority enumeration max_pages must be from 1 to 100")
    if not isinstance(request, dict) or "cursor" in request:
        raise ValueError("initial semantic authority enumeration request must be a non-cursor object")
    request["limit"] = page_size
    cursor_context = {
        key: copy.deepcopy(value)
        for key, value in request.items()
        if key not in {"cursor", "limit", "offset"}
    }
    projected: list[ProjectedProgram] = []
    unmapped_programs = 0
    coverage = "complete"
    enumeration_complete = True
    seen_cursors: set[str] = set()

    for page_number in range(1, max_pages + 1):
        response = worker.enumerate_routes(copy.deepcopy(request))
        if not isinstance(response, dict):
            raise RuntimeError("semantic authority enumeration response must be an object")
        page_coverage = response.get("coverageStatus", "unknown")
        coverage = _weaker_coverage(coverage, str(page_coverage))
        page_incomplete = bool(
            response.get("enumerationComplete") is False
            or response.get("code") == "INCOMPLETE_OUTCOME_INDEX"
            or page_coverage in {"bounded", "not_explored"}
        )
        if page_incomplete:
            enumeration_complete = False
        programs = response.get("programs", [])
        if not isinstance(programs, list):
            raise RuntimeError("semantic authority enumeration programs must be an array")
        for program in programs:
            if isinstance(program, dict) and program.get("complete") is True:
                try:
                    projected.append(project_authority_program(
                        descriptor,
                        program,
                        validate_outcome=not search_projection,
                    ))
                except ValueError as exc:
                    if not str(exc).startswith("UNMAPPED_ENGINE_STEP:"):
                        raise
                    unmapped_programs += 1

        if require_complete and page_incomplete:
            return ProjectedAuthoritySet(
                programs=(),
                coverage_status=coverage,
                enumeration_complete=False,
                pages=page_number,
                unmapped_programs=unmapped_programs,
            )

        cursor = response.get("nextCursor")
        if cursor in {None, ""}:
            return ProjectedAuthoritySet(
                programs=tuple(projected),
                coverage_status=coverage,
                enumeration_complete=enumeration_complete,
                pages=page_number,
                unmapped_programs=unmapped_programs,
            )
        if not isinstance(cursor, str):
            raise RuntimeError("semantic authority enumeration cursor must be text")
        if cursor in seen_cursors:
            raise RuntimeError("repeated semantic authority enumeration cursor")
        seen_cursors.add(cursor)
        if page_number == max_pages:
            if allow_truncated:
                return ProjectedAuthoritySet(
                    programs=tuple(projected),
                    coverage_status=_weaker_coverage(coverage, "bounded"),
                    enumeration_complete=False,
                    pages=page_number,
                    unmapped_programs=unmapped_programs,
                )
            raise RuntimeError("semantic authority enumeration page limit reached before completion")
        request = {
            **copy.deepcopy(cursor_context),
            "cursor": cursor,
            "limit": page_size,
        }
    raise RuntimeError("semantic authority enumeration page limit reached before completion")


def search_projected_authority(
    worker: Any,
    descriptor: SemanticDescriptor,
    submitted: SemanticChain,
    *,
    decision_id: str,
    state_hash: str,
) -> ClosestSearchResult:
    authority = collect_projected_programs(
        worker,
        descriptor,
        enumeration_request={
            **semantic_chain_to_authority_query(descriptor, submitted),
            "coverageTarget": True,
        },
    )
    return find_closest_programs(
        submitted,
        authority.programs,
        decision_id=decision_id,
        state_hash=state_hash,
        action_roles=descriptor.action_roles,
    )


def search_projected_authority_bounded(
    worker: Any,
    descriptor: SemanticDescriptor,
    submitted: SemanticChain,
    *,
    decision_id: str,
    state_hash: str,
    min_facts: int = 1,
    page_size: int = 20,
    max_pages: int = 20,
    locked_prefix_length: int = 0,
) -> AuthorityClosestEnvelope:
    """Compatibility entry point using the same global route-distance contract."""

    query = semantic_chain_to_authority_query(descriptor, submitted)
    facts = query["stepsContain"]
    if (
        isinstance(min_facts, bool)
        or not isinstance(min_facts, int)
        or not 0 <= min_facts <= len(facts)
    ):
        raise ValueError("semantic bounded search min_facts is invalid")
    prefix_fact_count = semantic_prefix_fact_count(
        descriptor,
        submitted,
        locked_prefix_length,
    )
    broad = collect_projected_programs(
        worker,
        descriptor,
        page_size=page_size,
        max_pages=max_pages,
        enumeration_request={"requirements": []},
        allow_truncated=True,
    )
    search = find_closest_programs(
        submitted,
        broad.programs,
        decision_id=decision_id,
        state_hash=state_hash,
        action_roles=descriptor.action_roles,
    )
    if broad.enumeration_complete and search.automaton_state_count <= 4_096:
        return AuthorityClosestEnvelope(
            search=search,
            authority=broad,
            requested_facts=len(facts),
            used_facts=0,
            relaxed_facts=(),
            search_mode="global_route_automaton",
        )

    if prefix_fact_count > 0:
        scoped = collect_projected_programs(
            worker,
            descriptor,
            page_size=page_size,
            max_pages=max_pages,
            enumeration_request={
                "stepsContain": copy.deepcopy(facts[:prefix_fact_count]),
                "coverageTarget": True,
            },
            allow_truncated=True,
        )
        scoped_search = find_closest_programs(
            submitted,
            scoped.programs,
            decision_id=decision_id,
            state_hash=state_hash,
            action_roles=descriptor.action_roles,
        )
        return AuthorityClosestEnvelope(
            search=scoped_search,
            authority=scoped,
            requested_facts=len(facts),
            used_facts=prefix_fact_count,
            relaxed_facts=tuple(
                _freeze_json(fact) for fact in facts[prefix_fact_count:]
            ),
            search_mode="prefix_scoped_route_automaton",
        )
    return AuthorityClosestEnvelope(
        search=search,
        authority=broad,
        requested_facts=len(facts),
        used_facts=0,
        relaxed_facts=(),
        search_mode="bounded_global_route_automaton",
    )


def search_projected_authority_primary(
    worker: Any,
    descriptor: SemanticDescriptor,
    submitted: SemanticChain,
    *,
    decision_id: str,
    state_hash: str,
    min_facts: int = 1,
    page_size: int = 20,
    max_pages: int = 20,
    max_validation_calls: int = 128,
) -> AuthorityClosestEnvelope:
    """Rank the current legal route language by full-chain global distance."""
    del min_facts
    compilation = compile_semantic_chain(
        worker,
        descriptor,
        submitted,
        decision_id=decision_id,
        state_hash=state_hash,
        max_validation_calls=max_validation_calls,
    )
    legal_prefix_length = matched_semantic_prefix_length(
        descriptor,
        submitted,
        compilation.matched_facts,
    )
    if compilation.reorder_action_index is not None:
        legal_prefix_length = min(
            legal_prefix_length,
            compilation.reorder_action_index,
        )
    compiled_programs = (
        (compilation.program, *compilation.alternative_programs)
        if compilation.program is not None
        else ()
    )
    if compiled_programs and compilation.status is CompilationStatus.COMPLETE:
        authority = ProjectedAuthoritySet(
            programs=compiled_programs,
            coverage_status="bounded",
            enumeration_complete=False,
            pages=0,
        )
        return AuthorityClosestEnvelope(
            search=find_closest_programs(
                submitted,
                authority.programs,
                decision_id=decision_id,
                state_hash=state_hash,
                action_roles=descriptor.action_roles,
            ),
            authority=authority,
            requested_facts=compilation.requested_facts,
            used_facts=compilation.matched_facts,
            relaxed_facts=(),
            search_mode="validated_route_automaton",
            compilation=compilation,
        )

    validated_prefix = (
        tuple(
            copy.deepcopy(dict(step))
            for step in compilation.validated_engine_prefix
        )
        if bool(getattr(worker, "supports_complete_prefix_subgraph", False))
        and compilation.validated_engine_prefix
        else ()
    )
    last_prefix_authority: ProjectedAuthoritySet | None = None
    for prefix_length in range(len(validated_prefix), 0, -1):
        prefix_steps = [
            copy.deepcopy(step)
            for step in validated_prefix[:prefix_length]
        ]
        response = worker.enumerate_routes({
            "prefixSteps": prefix_steps,
            "projection": "semantic-route-automaton",
        })
        if not isinstance(response, dict):
            raise RuntimeError("semantic route automaton response must be an object")
        if response.get("code") == "SEMANTIC_PREFIX_NOT_AT_BOUNDARY":
            continue
        complete = bool(
            response.get("coverageStatus") == "complete"
            and response.get("enumerationComplete") is True
        )
        last_prefix_authority = ProjectedAuthoritySet(
            programs=(),
            coverage_status=str(response.get("coverageStatus", "unknown")),
            enumeration_complete=complete,
            pages=1,
        )
        if not complete:
            break
        start_state = response.get("startState")
        raw_states = response.get("states")
        raw_route_count = response.get("routeCount")
        try:
            route_count = int(raw_route_count)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError("semantic route automaton routeCount is invalid") from exc
        if isinstance(start_state, bool) or not isinstance(start_state, int):
            raise RuntimeError("semantic route automaton startState is invalid")
        if not isinstance(raw_states, list):
            raise RuntimeError("semantic route automaton states must be an array")
        automaton = RouteAutomaton.from_compact(
            start_state=start_state,
            states=raw_states,
            word_count=route_count,
        )

        hydrated: list[ProjectedProgram] = []

        def hydrate(chain: SemanticChain) -> ProjectedProgram:
            details = worker.enumerate_routes({
                "prefixSteps": copy.deepcopy(prefix_steps),
                "projection": "semantic-route-hydrate",
                "semanticActions": [
                    action.to_dict() for action in chain.actions
                ],
            })
            if not isinstance(details, dict):
                raise RuntimeError("semantic route hydration response must be an object")
            programs = details.get("programs")
            if (
                details.get("coverageStatus") != "complete"
                or details.get("enumerationComplete") is not True
                or not isinstance(programs, list)
                or len(programs) != 1
                or not isinstance(details.get("routeDiagnostics"), dict)
                or details["routeDiagnostics"].get("exactReplay") is not True
            ):
                raise RuntimeError("semantic route hydration did not return one exact route")
            program = project_authority_program(descriptor, programs[0])
            # Compact graph tokens may contain package-private disambiguation
            # (for example an exact castle room) that the public model action
            # deliberately projects to a coarser floor. The adapter has already
            # replayed the exact graph path; this projection must remain the
            # only model-visible representation.
            hydrated.append(program)
            return program

        search = find_closest_routes(
            submitted,
            automaton,
            hydrate=hydrate,
            decision_id=decision_id,
            state_hash=state_hash,
            action_roles=descriptor.action_roles,
            collapse_choice_values_for_kinds=(
                descriptor.collapse_choice_values_for_kinds
            ),
            diversity_ignored_actions=descriptor.diversity_ignored_actions,
        )
        prefix_authority = ProjectedAuthoritySet(
            programs=tuple(hydrated),
            coverage_status="complete",
            enumeration_complete=True,
            pages=1 + len(hydrated),
        )
        return AuthorityClosestEnvelope(
            search=search,
            authority=prefix_authority,
            requested_facts=compilation.requested_facts,
            used_facts=compilation.matched_facts,
            relaxed_facts=(),
            search_mode="complete_prefix_semantic_automaton",
            compilation=compilation,
        )
    if bool(getattr(worker, "supports_complete_prefix_subgraph", False)) and validated_prefix:
        repaired_programs = _compile_nearby_routes(
            worker,
            descriptor,
            submitted,
            compilation,
            decision_id=decision_id,
            state_hash=state_hash,
            max_validation_calls=max_validation_calls,
        )
        if repaired_programs:
            return _bounded_repair_envelope(
                descriptor,
                submitted,
                repaired_programs,
                compilation,
                decision_id=decision_id,
                state_hash=state_hash,
                search_mode="validated_neighborhood_after_prefix_bound",
            )
        incomplete_authority = last_prefix_authority or ProjectedAuthoritySet(
            programs=(),
            coverage_status="not_explored",
            enumeration_complete=False,
            pages=0,
        )
        return AuthorityClosestEnvelope(
            search=find_closest_programs(
                submitted,
                (),
                decision_id=decision_id,
                state_hash=state_hash,
                action_roles=descriptor.action_roles,
            ),
            authority=incomplete_authority,
            requested_facts=compilation.requested_facts,
            used_facts=compilation.matched_facts,
            relaxed_facts=(),
            search_mode="prefix_subgraph_incomplete",
            compilation=compilation,
        )

    repaired_programs = _compile_nearby_routes(
        worker,
        descriptor,
        submitted,
        compilation,
        decision_id=decision_id,
        state_hash=state_hash,
        max_validation_calls=max_validation_calls,
    )
    if repaired_programs:
        return _bounded_repair_envelope(
            descriptor,
            submitted,
            repaired_programs,
            compilation,
            decision_id=decision_id,
            state_hash=state_hash,
            search_mode="validated_neighborhood_automaton",
        )

    suspect_action_index = (
        min(legal_prefix_length, len(submitted.actions) - 1)
        if submitted.actions
        else None
    )
    if suspect_action_index is not None and suspect_action_index > 0:
        for prefix_length in range(suspect_action_index, 0, -1):
            prefix = SemanticChain(
                name=submitted.name,
                actions=submitted.actions[:prefix_length],
            )
            scoped_prefix = _evidence_scoped_authority(
                worker,
                descriptor,
                prefix,
                suspect_action_index=None,
                page_size=page_size,
                max_pages=max_pages,
            )
            if not scoped_prefix.programs:
                continue
            return AuthorityClosestEnvelope(
                search=find_closest_programs(
                    submitted,
                    scoped_prefix.programs,
                    decision_id=decision_id,
                    state_hash=state_hash,
                    action_roles=descriptor.action_roles,
                ),
                authority=scoped_prefix,
                requested_facts=compilation.requested_facts,
                used_facts=semantic_prefix_fact_count(
                    descriptor,
                    submitted,
                    prefix_length,
                ),
                relaxed_facts=(),
                search_mode="prefix_recovery_route_automaton",
                compilation=compilation,
            )

    evidence = _evidence_scoped_authority(
        worker,
        descriptor,
        submitted,
        suspect_action_index=suspect_action_index,
        page_size=page_size,
        max_pages=max_pages,
    )
    if evidence.programs:
        evidence_authority = _with_additional_programs(evidence, compiled_programs)
        evidence_search = find_closest_programs(
            submitted,
            evidence_authority.programs,
            decision_id=decision_id,
            state_hash=state_hash,
            action_roles=descriptor.action_roles,
        )
        return AuthorityClosestEnvelope(
            search=evidence_search,
            authority=evidence_authority,
            requested_facts=compilation.requested_facts,
            used_facts=0,
            relaxed_facts=(),
            search_mode="evidence_scoped_route_automaton",
            compilation=compilation,
        )

    broad = collect_projected_programs(
        worker,
        descriptor,
        page_size=page_size,
        max_pages=max_pages,
        enumeration_request={"requirements": []},
        allow_truncated=True,
    )
    authority = broad
    search = find_closest_programs(
        submitted,
        authority.programs,
        decision_id=decision_id,
        state_hash=state_hash,
        action_roles=descriptor.action_roles,
    )
    if broad.enumeration_complete and search.automaton_state_count <= 4_096:
        return AuthorityClosestEnvelope(
            search=search,
            authority=authority,
            requested_facts=compilation.requested_facts,
            used_facts=0,
            relaxed_facts=(),
            search_mode="global_route_automaton",
            compilation=compilation,
        )

    prefix_fact_count = semantic_prefix_fact_count(
        descriptor,
        submitted,
        legal_prefix_length,
    )
    if prefix_fact_count > 0:
        facts = semantic_chain_to_authority_query(descriptor, submitted)["stepsContain"]
        scoped = collect_projected_programs(
            worker,
            descriptor,
            page_size=page_size,
            max_pages=max_pages,
            enumeration_request={
                "stepsContain": copy.deepcopy(facts[:prefix_fact_count]),
                "coverageTarget": True,
            },
            allow_truncated=True,
        )
        scoped_authority = _with_additional_programs(scoped, compiled_programs)
        scoped_search = find_closest_programs(
            submitted,
            scoped_authority.programs,
            decision_id=decision_id,
            state_hash=state_hash,
            action_roles=descriptor.action_roles,
        )
        return AuthorityClosestEnvelope(
            search=scoped_search,
            authority=scoped_authority,
            requested_facts=compilation.requested_facts,
            used_facts=prefix_fact_count,
            relaxed_facts=tuple(
                _freeze_json(fact) for fact in facts[prefix_fact_count:]
            ),
            search_mode="prefix_scoped_route_automaton",
            compilation=compilation,
        )

    return AuthorityClosestEnvelope(
        search=search,
        authority=authority,
        requested_facts=compilation.requested_facts,
        used_facts=0,
        relaxed_facts=(),
        search_mode="bounded_global_route_automaton",
        compilation=compilation,
    )
