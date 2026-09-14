from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from .model import SemanticAction, SemanticChain
from .route_automaton import RouteAutomaton, canonical_chain_tokens


INSERT_COST = 1
DELETE_COST = 1
ACTION_CHANGE_COST = 1
ARGUMENT_CHANGE_COST = 1


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in copy.deepcopy(dict(value)).items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in copy.deepcopy(list(value)))
    return copy.deepcopy(value)


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _thaw_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _copy_chain(chain: SemanticChain) -> SemanticChain:
    return SemanticChain(
        name=str(chain.name),
        actions=tuple(
            SemanticAction.from_mapping(action.action, dict(action.args))
            for action in chain.actions
        ),
    )


@dataclass(frozen=True)
class ProjectedProgram:
    program_id: str
    chain: SemanticChain
    engine_steps: tuple[Mapping[str, Any], ...]
    outcome: Mapping[str, Any]
    outcome_key: str
    semantic_signature: str
    public_summary: str | None = field(default=None, compare=False)

    @classmethod
    def create(
        cls,
        *,
        program_id: str,
        chain: SemanticChain,
        engine_steps: Sequence[Mapping[str, Any]],
        outcome: Mapping[str, Any],
        outcome_key: Any,
        public_summary: str | None = None,
    ) -> ProjectedProgram:
        if not isinstance(program_id, str) or not program_id:
            raise ValueError("projected program id must be non-empty")
        if any(not isinstance(step, Mapping) for step in engine_steps):
            raise ValueError("projected engine steps must be mappings")
        if not isinstance(outcome, Mapping):
            raise ValueError("projected outcome must be a mapping")
        from .outcome import validate_public_summary
        public_summary = validate_public_summary(public_summary)
        frozen_chain = _copy_chain(chain)
        frozen_steps = tuple(_freeze_json(step) for step in engine_steps)
        frozen_outcome = _freeze_json(outcome)
        semantic_signature = _canonical_json(
            [action.to_dict() for action in frozen_chain.actions]
        )
        return cls(
            program_id=program_id,
            chain=frozen_chain,
            engine_steps=frozen_steps,
            outcome=frozen_outcome,
            outcome_key=_canonical_json(outcome_key),
            semantic_signature=semantic_signature,
            public_summary=public_summary,
        )


class SemanticEditKind(StrEnum):
    INSERT = "INSERT"
    DELETE = "DELETE"
    ACTION_CHANGE = "ACTION_CHANGE"
    ARGUMENT_CHANGE = "ARGUMENT_CHANGE"


@dataclass(frozen=True)
class SemanticEdit:
    kind: SemanticEditKind
    submitted_index: int | None
    candidate_index: int | None
    submitted: SemanticAction | None
    candidate: SemanticAction | None
    cost: int


@dataclass(frozen=True)
class SemanticAlignment:
    total_cost: int
    root_changed: bool
    edits: tuple[SemanticEdit, ...]


@dataclass(frozen=True)
class ClosestCandidate:
    label: str
    program: ProjectedProgram
    alignment: SemanticAlignment
    binding_fingerprint: str
    intent_exact: bool
    commit_ready: bool


@dataclass(frozen=True)
class ClosestSearchResult:
    decision_id: str
    state_hash: str
    submitted: SemanticChain
    candidates: tuple[ClosestCandidate, ...]
    route_count: int
    automaton_state_count: int
    automaton_transition_count: int


def _edit_key(edit: SemanticEdit) -> str:
    return _canonical_json(
        {
            "kind": edit.kind.value,
            "submittedIndex": edit.submitted_index,
            "candidateIndex": edit.candidate_index,
            "submitted": edit.submitted.to_dict() if edit.submitted else None,
            "candidate": edit.candidate.to_dict() if edit.candidate else None,
            "cost": edit.cost,
        }
    )


def _path_key(path: tuple[int, tuple[SemanticEdit, ...]]) -> tuple[Any, ...]:
    return path[0], tuple(_edit_key(edit) for edit in path[1])


def align_semantic_chains(
    submitted: SemanticChain,
    candidate: SemanticChain,
) -> SemanticAlignment:
    left = submitted.actions
    right = candidate.actions
    matrix: list[list[tuple[int, tuple[SemanticEdit, ...]]]] = [
        [(0, ()) for _ in range(len(right) + 1)]
        for _ in range(len(left) + 1)
    ]
    for index in range(1, len(left) + 1):
        edit = SemanticEdit(
            kind=SemanticEditKind.DELETE,
            submitted_index=index - 1,
            candidate_index=None,
            submitted=left[index - 1],
            candidate=None,
            cost=DELETE_COST,
        )
        prior = matrix[index - 1][0]
        matrix[index][0] = prior[0] + edit.cost, (*prior[1], edit)
    for index in range(1, len(right) + 1):
        edit = SemanticEdit(
            kind=SemanticEditKind.INSERT,
            submitted_index=None,
            candidate_index=index - 1,
            submitted=None,
            candidate=right[index - 1],
            cost=INSERT_COST,
        )
        prior = matrix[0][index - 1]
        matrix[0][index] = prior[0] + edit.cost, (*prior[1], edit)

    for submitted_index in range(1, len(left) + 1):
        for candidate_index in range(1, len(right) + 1):
            submitted_action = left[submitted_index - 1]
            candidate_action = right[candidate_index - 1]
            diagonal = matrix[submitted_index - 1][candidate_index - 1]
            options: list[tuple[int, tuple[SemanticEdit, ...]]] = []
            if submitted_action == candidate_action:
                options.append(diagonal)
            else:
                same_action = submitted_action.action == candidate_action.action
                cost = (
                    ARGUMENT_CHANGE_COST
                    if same_action
                    else ACTION_CHANGE_COST
                )
                edit = SemanticEdit(
                    kind=(
                        SemanticEditKind.ARGUMENT_CHANGE
                        if same_action
                        else SemanticEditKind.ACTION_CHANGE
                    ),
                    submitted_index=submitted_index - 1,
                    candidate_index=candidate_index - 1,
                    submitted=submitted_action,
                    candidate=candidate_action,
                    cost=cost,
                )
                options.append((diagonal[0] + cost, (*diagonal[1], edit)))

            deleted = matrix[submitted_index - 1][candidate_index]
            delete_edit = SemanticEdit(
                kind=SemanticEditKind.DELETE,
                submitted_index=submitted_index - 1,
                candidate_index=None,
                submitted=submitted_action,
                candidate=None,
                cost=DELETE_COST,
            )
            options.append((deleted[0] + DELETE_COST, (*deleted[1], delete_edit)))

            inserted = matrix[submitted_index][candidate_index - 1]
            insert_edit = SemanticEdit(
                kind=SemanticEditKind.INSERT,
                submitted_index=None,
                candidate_index=candidate_index - 1,
                submitted=None,
                candidate=candidate_action,
                cost=INSERT_COST,
            )
            options.append((inserted[0] + INSERT_COST, (*inserted[1], insert_edit)))
            matrix[submitted_index][candidate_index] = min(options, key=_path_key)

    base_cost, edits = matrix[len(left)][len(right)]
    root_changed = bool(
        (left or right)
        and (not left or not right or left[0] != right[0])
    )
    return SemanticAlignment(
        total_cost=base_cost,
        root_changed=root_changed,
        edits=edits,
    )


def intent_equivalent(
    submitted: SemanticChain,
    candidate: SemanticChain,
    action_roles: Mapping[str, str] | None = None,
) -> bool:
    roles = action_roles or {}
    alignment = align_semantic_chains(submitted, candidate)
    return all(
        edit.kind is SemanticEditKind.INSERT
        and edit.candidate is not None
        and roles.get(edit.candidate.action, "intent") in {"derived", "choice"}
        for edit in alignment.edits
    )


def commit_equivalent(
    submitted: SemanticChain,
    candidate: SemanticChain,
    action_roles: Mapping[str, str] | None = None,
) -> bool:
    """True only when no unchosen Player decision is added or changed."""

    roles = action_roles or {}
    alignment = align_semantic_chains(submitted, candidate)
    return all(
        edit.kind is SemanticEditKind.INSERT
        and edit.candidate is not None
        and roles.get(edit.candidate.action, "intent") == "derived"
        for edit in alignment.edits
    )


def _binding_fingerprint(
    *,
    decision_id: str,
    state_hash: str,
    program: ProjectedProgram,
) -> str:
    payload = _canonical_json(
        {
            "decisionId": decision_id,
            "stateHash": state_hash,
            "programId": program.program_id,
            "semanticSignature": program.semantic_signature,
            "outcomeKey": program.outcome_key,
            "engineSteps": [_thaw_json(step) for step in program.engine_steps],
        }
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def find_closest_programs(
    submitted: SemanticChain,
    programs: Sequence[ProjectedProgram],
    *,
    decision_id: str,
    state_hash: str,
    limit: int = 5,
    action_roles: Mapping[str, str] | None = None,
    primary_program_id: str | None = None,
    locked_prefix_length: int = 0,
    collapse_choice_values_for_kinds: Sequence[str] = (),
    diversity_ignored_actions: Sequence[str] = (),
) -> ClosestSearchResult:
    if not isinstance(decision_id, str) or not decision_id:
        raise ValueError("closest search decision id must be non-empty")
    if not isinstance(state_hash, str) or not state_hash:
        raise ValueError("closest search state hash must be non-empty")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
        raise ValueError("closest search limit must be an integer from 1 to 5")
    if any(not isinstance(program, ProjectedProgram) for program in programs):
        raise ValueError("closest search programs must be projected programs")
    del primary_program_id
    if (
        isinstance(locked_prefix_length, bool)
        or not isinstance(locked_prefix_length, int)
        or not 0 <= locked_prefix_length <= len(submitted.actions)
    ):
        raise ValueError("locked_prefix_length must select a submitted action prefix")
    locked_prefix = submitted.actions[:locked_prefix_length]
    eligible_programs = (
        tuple(programs)
        if locked_prefix_length == 0
        else tuple(
            program
            for program in programs
            if program.chain.actions[:locked_prefix_length] == locked_prefix
        )
    )

    representatives: dict[tuple[str, str], ProjectedProgram] = {}
    for program in eligible_programs:
        key = program.semantic_signature, program.outcome_key
        current = representatives.get(key)
        if current is None:
            representatives[key] = program
        elif (
            len(program.engine_steps), program.program_id
        ) < (len(current.engine_steps), current.program_id):
            representatives[key] = program

    programs_by_tokens: dict[tuple[tuple[str, str], ...], list[ProjectedProgram]] = {}
    for program in representatives.values():
        tokens = canonical_chain_tokens(program.chain)
        programs_by_tokens.setdefault(tokens, []).append(program)
    automaton = RouteAutomaton.build(programs_by_tokens)
    nearest = automaton.nearest(
        canonical_chain_tokens(submitted),
        limit=max(1, automaton.word_count),
    )
    ranked: list[ProjectedProgram] = []
    for match in nearest:
        ranked.extend(sorted(
            programs_by_tokens[match.tokens],
            key=lambda program: (
                len(program.engine_steps),
                program.semantic_signature,
                program.program_id,
                program.outcome_key,
            ),
        ))
    collapsed_kinds = frozenset(collapse_choice_values_for_kinds)
    ignored_actions = frozenset(diversity_ignored_actions)

    def diversity_key(program: ProjectedProgram) -> str:
        actions: list[dict[str, Any]] = []
        for action in program.chain.actions:
            if action.action in ignored_actions:
                continue
            payload = action.to_dict()
            arguments = payload["args"]
            if (
                isinstance(arguments, dict)
                and arguments.get("kind") in collapsed_kinds
                and "choice" in arguments
            ):
                arguments = {**arguments, "choice": "*"}
            normalized = {"action": payload["action"], "args": arguments}
            actions.append(normalized)
        return _canonical_json(actions)

    selected: list[ProjectedProgram] = []
    deferred: list[ProjectedProgram] = []
    seen_diversity: set[str] = set()
    for program in ranked:
        key = diversity_key(program)
        if key in seen_diversity:
            deferred.append(program)
            continue
        seen_diversity.add(key)
        selected.append(program)
        if len(selected) == limit:
            break
    if len(selected) < limit:
        selected.extend(deferred[: limit - len(selected)])

    candidates = tuple(
        ClosestCandidate(
            label=f"C{index}",
            program=program,
            alignment=align_semantic_chains(submitted, program.chain),
            binding_fingerprint=_binding_fingerprint(
                decision_id=decision_id,
                state_hash=state_hash,
                program=program,
            ),
            intent_exact=intent_equivalent(
                submitted,
                program.chain,
                action_roles,
            ),
            commit_ready=True,
        )
        for index, program in enumerate(selected, 1)
    )
    return ClosestSearchResult(
        decision_id=decision_id,
        state_hash=state_hash,
        submitted=_copy_chain(submitted),
        candidates=candidates,
        route_count=automaton.word_count,
        automaton_state_count=automaton.state_count,
        automaton_transition_count=automaton.transition_count,
    )


def _semantic_chain_from_route_tokens(
    tokens: tuple[tuple[str, str], ...],
    *,
    name: str,
) -> SemanticChain:
    actions: list[SemanticAction] = []
    for kind, encoded_arguments in tokens:
        if not kind.startswith("action:"):
            raise ValueError("compact route token is not a semantic action")
        arguments = json.loads(encoded_arguments)
        if not isinstance(arguments, dict):
            raise ValueError("compact route action arguments must be an object")
        actions.append(SemanticAction.from_mapping(kind.removeprefix("action:"), arguments))
    return SemanticChain(name=name, actions=tuple(actions))


def find_closest_routes(
    submitted: SemanticChain,
    automaton: RouteAutomaton,
    *,
    hydrate: Callable[[SemanticChain], ProjectedProgram],
    decision_id: str,
    state_hash: str,
    limit: int = 5,
    action_roles: Mapping[str, str] | None = None,
    collapse_choice_values_for_kinds: Sequence[str] = (),
    diversity_ignored_actions: Sequence[str] = (),
) -> ClosestSearchResult:
    """Rank a complete compact route language and hydrate only selected routes."""

    if not isinstance(decision_id, str) or not decision_id:
        raise ValueError("closest search decision id must be non-empty")
    if not isinstance(state_hash, str) or not state_hash:
        raise ValueError("closest search state hash must be non-empty")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
        raise ValueError("closest search limit must be an integer from 1 to 5")
    collapsed_kinds = frozenset(collapse_choice_values_for_kinds)
    ignored_actions = frozenset(diversity_ignored_actions)

    def diversity_key(tokens: tuple[tuple[str, str], ...]) -> str:
        actions: list[dict[str, Any]] = []
        for encoded_action, encoded_arguments in tokens:
            action = encoded_action.removeprefix("action:")
            if action in ignored_actions:
                continue
            arguments = json.loads(encoded_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("compact route action arguments must be an object")
            arguments = {
                key: value
                for key, value in arguments.items()
                if not key.startswith("$")
            }
            if (
                arguments.get("kind") in collapsed_kinds
                and "choice" in arguments
            ):
                arguments = {**arguments, "choice": "*"}
            actions.append({"action": action, "args": arguments})
        return _canonical_json(actions)

    nearest = automaton.nearest(
        canonical_chain_tokens(submitted),
        limit=min(automaton.word_count, limit),
        distinct_key=(
            diversity_key
            if collapsed_kinds or ignored_actions
            else None
        ),
    )
    ranked = tuple(
        _semantic_chain_from_route_tokens(match.tokens, name=submitted.name)
        for match in nearest
    )
    programs = tuple(hydrate(chain) for chain in ranked)
    candidates = tuple(
        ClosestCandidate(
            label=f"C{index}",
            program=program,
            alignment=align_semantic_chains(submitted, program.chain),
            binding_fingerprint=_binding_fingerprint(
                decision_id=decision_id,
                state_hash=state_hash,
                program=program,
            ),
            intent_exact=intent_equivalent(
                submitted,
                program.chain,
                action_roles,
            ),
            commit_ready=True,
        )
        for index, program in enumerate(programs, 1)
    )
    return ClosestSearchResult(
        decision_id=decision_id,
        state_hash=state_hash,
        submitted=_copy_chain(submitted),
        candidates=candidates,
        route_count=automaton.word_count,
        automaton_state_count=automaton.state_count,
        automaton_transition_count=automaton.transition_count,
    )
