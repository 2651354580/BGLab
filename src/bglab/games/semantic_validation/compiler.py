from __future__ import annotations

import copy
import hashlib
import inspect
import json
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .closest import ProjectedProgram, _freeze_json, intent_equivalent
from .descriptor import (
    SemanticDescriptor,
    _matches_pattern,
    _rule_accepts_semantic_action,
    project_authority_program,
    semantic_chain_to_authority_query,
)
from .model import SemanticAction, SemanticChain
from .outcome import validate_public_summary


_BEGIN_STEP = {"op": "begin", "action": "turn"}
_UNSPECIFIED_EFFECT_TYPE = object()


class CompilationStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    ILLEGAL = "illegal"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True)
class AuthorityCompilation:
    status: CompilationStatus
    program: ProjectedProgram | None
    matched_facts: int
    requested_facts: int
    legal_frontier: tuple[Mapping[str, Any], ...]
    authority_reason: Mapping[str, Any] | None
    first_unmatched_action: SemanticAction | None
    validation_calls: int
    alternative_programs: tuple[ProjectedProgram, ...] = ()
    reorder_action_index: int | None = None
    reorder_legal_frontier: tuple[Mapping[str, Any], ...] = ()
    validated_engine_prefix: tuple[Mapping[str, Any], ...] = ()
    validated_prefix_summary: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "programId": self.program.program_id if self.program is not None else None,
            "matchedFacts": self.matched_facts,
            "requestedFacts": self.requested_facts,
            "legalFrontier": [dict(item) for item in self.legal_frontier],
            "authorityReason": (
                dict(self.authority_reason)
                if self.authority_reason is not None
                else None
            ),
            "firstUnmatchedAction": (
                self.first_unmatched_action.to_dict()
                if self.first_unmatched_action is not None
                else None
            ),
            "validationCalls": self.validation_calls,
            "alternativeProgramIds": [
                program.program_id for program in self.alternative_programs
            ],
            "reorderActionIndex": self.reorder_action_index,
            "reorderLegalFrontier": [
                dict(item) for item in self.reorder_legal_frontier
            ],
            "validatedEnginePrefix": [
                dict(item) for item in self.validated_engine_prefix
            ],
            **(
                {"validatedPrefixSummary": self.validated_prefix_summary}
                if self.validated_prefix_summary is not None else {}
            ),
        }


def _canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _matches_fact(
    step: Mapping[str, Any],
    fact: Mapping[str, Any],
    *,
    descriptor: SemanticDescriptor | None = None,
    owner_action: SemanticAction | None = None,
    actual_effect_type: Any = _UNSPECIFIED_EFFECT_TYPE,
) -> bool:
    exact = all(
        key in step
        and type(step[key]) is type(value)
        and step[key] == value
        for key, value in fact.items()
    )
    if not exact or descriptor is None or owner_action is None:
        return exact
    return any(
        _matches_pattern(
            step,
            pattern,
            (
                pattern.effect_type
                if actual_effect_type is _UNSPECIFIED_EFFECT_TYPE
                else actual_effect_type
            ),
        )
        for rule in descriptor.rules
        if _rule_accepts_semantic_action(rule, owner_action)
        for pattern in rule.steps
    )


def _ordered_fact_step_indexes(
    steps: Sequence[Mapping[str, Any]],
    facts: Sequence[Mapping[str, Any]],
    *,
    fact_owners: Sequence[int] | None = None,
    submitted: SemanticChain | None = None,
    descriptor: SemanticDescriptor | None = None,
    effect_types: Mapping[int, str] | None = None,
) -> tuple[int, ...]:
    matched_step_indexes: list[int] = []
    for step_index, step in enumerate(steps):
        matched = len(matched_step_indexes)
        owner_action = (
            submitted.actions[fact_owners[matched]]
            if (
                matched < len(facts)
                and fact_owners is not None
                and submitted is not None
            )
            else None
        )
        if matched < len(facts) and _matches_fact(
            step,
            facts[matched],
            descriptor=descriptor,
            owner_action=owner_action,
            actual_effect_type=(
                effect_types.get(step_index, _UNSPECIFIED_EFFECT_TYPE)
                if effect_types is not None
                else _UNSPECIFIED_EFFECT_TYPE
            ),
        ):
            matched_step_indexes.append(step_index)
    return tuple(matched_step_indexes)


def _ordered_fact_prefix(
    steps: Sequence[Mapping[str, Any]],
    facts: Sequence[Mapping[str, Any]],
    *,
    fact_owners: Sequence[int] | None = None,
    submitted: SemanticChain | None = None,
    descriptor: SemanticDescriptor | None = None,
    effect_types: Mapping[int, str] | None = None,
) -> int:
    return len(_ordered_fact_step_indexes(
        steps,
        facts,
        fact_owners=fact_owners,
        submitted=submitted,
        descriptor=descriptor,
        effect_types=effect_types,
    ))


def _complete_semantic_fact_count(
    fact_owners: Sequence[int],
    matched_facts: int,
    action_count: int,
) -> int:
    """Return facts belonging to the longest wholly matched action prefix."""

    complete_fact_count = 0
    for action_index in range(action_count):
        owned = tuple(
            index for index, owner in enumerate(fact_owners)
            if owner == action_index
        )
        if not owned or owned[-1] >= matched_facts:
            break
        complete_fact_count = owned[-1] + 1
    return complete_fact_count


def _fact_plan(
    descriptor: SemanticDescriptor,
    submitted: SemanticChain,
    *,
    relaxed_action_indexes: frozenset[int] = frozenset(),
) -> tuple[tuple[Mapping[str, Any], ...], tuple[int, ...]]:
    facts: tuple[Mapping[str, Any], ...] = ()
    owners: list[int] = []
    for index in range(len(submitted.actions)):
        prefix = SemanticChain(
            name=submitted.name,
            actions=submitted.actions[: index + 1],
        )
        current = tuple(
            copy.deepcopy(item)
            for item in semantic_chain_to_authority_query(
                descriptor,
                prefix,
                relaxed_action_indexes=frozenset(
                    item for item in relaxed_action_indexes if item <= index
                ),
            )["stepsContain"]
        )
        if len(current) < len(facts) or current[: len(facts)] != facts:
            raise ValueError("semantic authority facts are not prefix-monotonic")
        owners.extend([index] * (len(current) - len(facts)))
        facts = current
    return facts, tuple(owners)


def matched_semantic_prefix_length(
    descriptor: SemanticDescriptor,
    submitted: SemanticChain,
    matched_facts: int,
) -> int:
    """Return the number of whole leading semantic actions proven by Authority."""

    facts, owners = _fact_plan(descriptor, submitted)
    if (
        isinstance(matched_facts, bool)
        or not isinstance(matched_facts, int)
        or not 0 <= matched_facts <= len(facts)
    ):
        raise ValueError("matched_facts must select an authority fact prefix")
    prefix_length = 0
    for action_index in range(len(submitted.actions)):
        owned_facts = tuple(
            fact_index
            for fact_index, owner in enumerate(owners)
            if owner == action_index
        )
        if not owned_facts or owned_facts[-1] >= matched_facts:
            break
        prefix_length += 1
    return prefix_length


def semantic_prefix_fact_count(
    descriptor: SemanticDescriptor,
    submitted: SemanticChain,
    prefix_length: int,
) -> int:
    """Return how many leading authority facts belong to a semantic prefix."""

    if (
        isinstance(prefix_length, bool)
        or not isinstance(prefix_length, int)
        or not 0 <= prefix_length <= len(submitted.actions)
    ):
        raise ValueError("prefix_length must select a semantic action prefix")
    _, owners = _fact_plan(descriptor, submitted)
    return sum(owner < prefix_length for owner in owners)


def semantic_intent_facts(
    descriptor: SemanticDescriptor,
    submitted: SemanticChain,
    *,
    relaxed_action_indexes: frozenset[int] = frozenset(),
) -> tuple[Mapping[str, Any], ...]:
    """Project every model-owned intent action as an unordered Authority filter."""

    facts, owners = _fact_plan(
        descriptor,
        submitted,
        relaxed_action_indexes=relaxed_action_indexes,
    )
    return tuple(
        fact
        for fact, owner in zip(facts, owners)
        if descriptor.action_roles.get(
            submitted.actions[owner].action,
            "intent",
        ) == "intent"
    )


def reorder_legal_semantic_actions(
    descriptor: SemanticDescriptor,
    submitted: SemanticChain,
    compilation: AuthorityCompilation,
) -> tuple[SemanticChain, ...]:
    """Return every one-move ordering accepted by the current legal frontier.

    Models sometimes emit a reward choice before the placement or member action
    that opens it.  This transformation preserves every submitted semantic
    action and argument; it changes only the order needed for Authority to test
    the model's actual action set.
    """

    frontier_source = (
        compilation.reorder_legal_frontier
        or compilation.legal_frontier
    )
    if compilation.program is not None or not frontier_source:
        return ()
    insertion_index = (
        compilation.reorder_action_index
        if compilation.reorder_action_index is not None
        else matched_semantic_prefix_length(
            descriptor,
            submitted,
            compilation.matched_facts,
        )
    )
    facts, owners = _fact_plan(descriptor, submitted)

    def transaction_step(value: Mapping[str, Any]) -> dict[str, Any]:
        normalized = copy.deepcopy(dict(value))
        if "op" not in normalized and isinstance(normalized.get("type"), str):
            normalized["op"] = normalized.pop("type")
        return normalized

    frontier = tuple(transaction_step(item) for item in frontier_source)
    reordered: list[SemanticChain] = []
    for action_index in range(insertion_index + 1, len(submitted.actions)):
        owned = tuple(
            fact
            for fact, owner in zip(facts, owners)
            if owner == action_index
        )
        if not owned:
            continue
        owner_action = submitted.actions[action_index]
        if not any(
            _matches_fact(
                step,
                owned[0],
                descriptor=descriptor,
                owner_action=owner_action,
            )
            for step in frontier
        ):
            continue
        actions = list(submitted.actions)
        action = actions.pop(action_index)
        actions.insert(insertion_index, action)
        reordered.append(SemanticChain(name=submitted.name, actions=tuple(actions)))
    return tuple(reordered)


def reorder_next_legal_semantic_action(
    descriptor: SemanticDescriptor,
    submitted: SemanticChain,
    compilation: AuthorityCompilation,
) -> SemanticChain | None:
    """Compatibility helper returning the first legal one-move ordering."""

    alternatives = reorder_legal_semantic_actions(
        descriptor,
        submitted,
        compilation,
    )
    return alternatives[0] if alternatives else None


def _matching_next_actions(
    next_actions: Sequence[Any],
    facts: Sequence[Mapping[str, Any]],
    matched: int,
    descriptor: SemanticDescriptor,
    *,
    fact_owners: Sequence[int] | None = None,
    submitted: SemanticChain | None = None,
) -> tuple[dict[str, Any], ...]:
    def transaction_step(action: Mapping[str, Any]) -> dict[str, Any]:
        normalized = copy.deepcopy(dict(action))
        if "op" not in normalized and isinstance(normalized.get("type"), str):
            normalized["op"] = normalized.pop("type")
        return normalized

    ordered = sorted(
        (
            transaction_step(action)
            for action in next_actions
            if isinstance(action, Mapping)
        ),
        key=_canonical,
    )
    if matched >= len(facts):
        # The submitted semantic prefix is fully accounted for. Explore every
        # legal continuation so an optional finish/skip action cannot hide the
        # actual member or target actions that complete this decision.
        return tuple(ordered)
    if matched < len(facts):
        owner_action = (
            submitted.actions[fact_owners[matched]]
            if fact_owners is not None and submitted is not None
            else None
        )
        exact = tuple(
            action for action in ordered
            if _matches_fact(
                action,
                facts[matched],
                descriptor=descriptor,
                owner_action=owner_action,
                actual_effect_type=action.get(
                    "choiceType",
                    _UNSPECIFIED_EFFECT_TYPE,
                ),
            )
        )
        if exact:
            return exact
    bridging = tuple(
        action for action in ordered
        if _is_intent_preserving_bridge(action, descriptor)
    )
    if bridging:
        return bridging
    return tuple(ordered) if len(ordered) == 1 else ()


def _is_intent_preserving_bridge(
    action: Mapping[str, Any],
    descriptor: SemanticDescriptor,
) -> bool:
    op = action.get("op")
    if not isinstance(op, str):
        return False
    if op in descriptor.mechanical_ops:
        return True
    roles: set[str] = set()
    for rule in descriptor.rules:
        pattern = rule.steps[0]
        if pattern.op != op:
            continue
        if any(action.get(key) != expected for key, expected in pattern.where.items()):
            continue
        if any(
            not isinstance(action.get(key), str)
            or regex.fullmatch(action[key]) is None
            for key, regex in pattern.where_regex.items()
        ):
            continue
        roles.add(descriptor.action_roles.get(rule.semantic_action, "intent"))
    return bool(roles) and roles <= {"derived", "choice"}


def _program_from_validation(
    validation: Mapping[str, Any],
    validated: Sequence[Mapping[str, Any]],
    *,
    automatic_step_indexes: Sequence[int] = (),
) -> dict[str, Any]:
    steps = [copy.deepcopy(dict(step)) for step in validated]
    fingerprint = hashlib.sha256(_canonical(steps).encode("utf-8")).hexdigest()
    outcome = validation.get("outcome")
    if not isinstance(outcome, Mapping):
        outcome = validation.get("netOutcome")
    if not isinstance(outcome, Mapping):
        outcome = {}
    causal_trace = validation.get("causalTrace")
    return {
        "programId": f"semantic-{fingerprint[:20]}",
        "complete": True,
        "steps": steps,
        "automaticStepIndexes": list(automatic_step_indexes),
        "outcome": copy.deepcopy(dict(outcome)),
        **({"publicSummary": validation["publicSummary"]} if "publicSummary" in validation else {}),
        "causalTrace": (
            copy.deepcopy(causal_trace)
            if isinstance(causal_trace, list)
            else []
        ),
    }


def _intent_equivalent(
    submitted: SemanticChain,
    candidate: SemanticChain,
    action_roles: Mapping[str, str],
) -> bool:
    return intent_equivalent(submitted, candidate, action_roles)


def _frozen_frontier(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        _freeze_json(dict(item))
        for item in value
        if isinstance(item, Mapping)
    )


def _reason(code: str, **facts: Any) -> Mapping[str, Any]:
    return _freeze_json({"code": code, **facts})


def compile_semantic_chain(
    worker: Any,
    descriptor: SemanticDescriptor,
    submitted: SemanticChain,
    *,
    decision_id: str,
    state_hash: str,
    max_validation_calls: int = 64,
    max_depth: int = 64,
) -> AuthorityCompilation:
    if not isinstance(decision_id, str) or not decision_id:
        raise ValueError("semantic compilation decision_id must be non-empty")
    if not isinstance(state_hash, str) or not state_hash:
        raise ValueError("semantic compilation state_hash must be non-empty")
    for value, field in (
        (max_validation_calls, "max_validation_calls"),
        (max_depth, "max_depth"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"semantic compilation {field} must be positive")
    try:
        facts, fact_owners = _fact_plan(descriptor, submitted)
    except ValueError as exc:
        return AuthorityCompilation(
            status=CompilationStatus.ILLEGAL,
            program=None,
            matched_facts=0,
            requested_facts=0,
            legal_frontier=(),
            authority_reason=_reason(
                "SEMANTIC_MAPPING_MISSING",
                detail=str(exc),
            ),
            first_unmatched_action=(
                submitted.actions[0] if submitted.actions else None
            ),
            validation_calls=0,
        )

    initial_steps: tuple[dict[str, Any], ...] = (
        ()
        if facts and facts[0].get("op") == "begin"
        else (copy.deepcopy(_BEGIN_STEP),)
    )
    frontier: deque[
        tuple[tuple[dict[str, Any], ...], frozenset[int]]
    ] = deque()
    proposed_steps = (*initial_steps, *(copy.deepcopy(dict(fact)) for fact in facts))
    if proposed_steps != initial_steps and len(proposed_steps) <= max_depth:
        # Validate the submitted route as a whole before exploring prefixes.
        # Finite enumerators may choose one canonical order for independent
        # payments/discards; that order is not a restriction on engine legality.
        # These facts are only a proposal: normal full-prefix, projection and
        # intent checks below still apply, and an incomplete proposal falls back.
        frontier.append((proposed_steps, frozenset()))
    frontier.append((initial_steps, frozenset()))
    seen: set[str] = set()
    best_matched = 0
    best_prefix_length = 0
    best_validation: Mapping[str, Any] = {}
    best_trusted_engine_prefix: tuple[Mapping[str, Any], ...] = ()
    best_trusted_fact_count = 0
    validation_calls = 0
    compiled_programs: list[ProjectedProgram] = []
    compiled_outcomes: set[str] = set()
    reorder_action_index: int | None = None
    reorder_legal_frontier: tuple[Mapping[str, Any], ...] = ()
    completion_frontier_validation: Mapping[str, Any] | None = None
    completion_frontier_depth: int | None = None

    def ordered_compiled_programs() -> list[ProjectedProgram]:
        return sorted(
            compiled_programs,
            key=lambda program: (
                program.chain.actions != submitted.actions,
                bool(program.outcome.get("unexecutedActionAbandoned") is True),
                len(program.chain.actions),
                program.semantic_signature,
                program.program_id,
            ),
        )

    while frontier and validation_calls < max_validation_calls:
        requested_steps, inserted_step_indexes = frontier.popleft()
        validation_calls += 1
        validation = worker.validate_transaction(
            {"steps": [copy.deepcopy(item) for item in requested_steps]},
        )
        if inspect.isawaitable(validation):
            if inspect.iscoroutine(validation):
                validation.close()
            raise RuntimeError(
                "ASYNC_VALIDATION_UNSUPPORTED: use the async semantic compiler",
            )
        if not isinstance(validation, Mapping):
            raise RuntimeError("semantic validation result must be an object")
        raw_validated = validation.get("validatedPrefix")
        validated_prefix_valid = (
            "validatedPrefix" in validation
            and isinstance(raw_validated, list)
            and all(isinstance(item, Mapping) for item in raw_validated)
        )
        validated = tuple(
            copy.deepcopy(dict(item))
            for item in (raw_validated if validated_prefix_valid else ())
        )
        validation_effect_types: dict[int, str] = {}
        raw_trace = validation.get("causalTrace")
        if isinstance(raw_trace, list):
            for event in raw_trace:
                if not isinstance(event, Mapping):
                    continue
                action_step = event.get("actionStep")
                choice_type = event.get("choiceType")
                if (
                    isinstance(action_step, int)
                    and not isinstance(action_step, bool)
                    and isinstance(choice_type, str)
                    and choice_type
                ):
                    validation_effect_types[action_step] = choice_type
        matched_step_indexes = _ordered_fact_step_indexes(
            validated,
            facts,
            fact_owners=fact_owners,
            submitted=submitted,
            descriptor=descriptor,
            effect_types=validation_effect_types,
        )
        matched = len(matched_step_indexes)
        trusted_fact_count = _complete_semantic_fact_count(
            fact_owners,
            matched,
            len(submitted.actions),
        )
        trusted_engine_prefix = (
            validated[: matched_step_indexes[trusted_fact_count - 1] + 1]
            if trusted_fact_count > 0
            else ()
        )
        if (
            trusted_fact_count,
            len(trusted_engine_prefix),
        ) > (
            best_trusted_fact_count,
            len(best_trusted_engine_prefix),
        ):
            best_trusted_fact_count = trusted_fact_count
            best_trusted_engine_prefix = _frozen_frontier(
                trusted_engine_prefix,
            )
        projection_automatic_indexes = set(inserted_step_indexes)
        raw_auto_advanced = validation.get("autoAdvancedSteps")
        if (
            isinstance(raw_auto_advanced, list)
            and all(isinstance(item, Mapping) for item in raw_auto_advanced)
            and len(raw_auto_advanced) <= len(validated)
        ):
            auto_start = len(validated) - len(raw_auto_advanced)
            if all(
                _canonical(validated[auto_start + offset])
                == _canonical(item)
                for offset, item in enumerate(raw_auto_advanced)
            ):
                projection_automatic_indexes.update(
                    step_index
                    for step_index in range(auto_start, len(validated))
                    if not any(
                        _matches_fact(
                            validated[step_index],
                            fact,
                            descriptor=descriptor,
                            owner_action=submitted.actions[owner],
                            actual_effect_type=validation_effect_types.get(
                                step_index,
                                _UNSPECIFIED_EFFECT_TYPE,
                            ),
                        )
                        for fact, owner in zip(facts, fact_owners)
                    )
                )
        if (matched, len(validated)) >= (best_matched, best_prefix_length):
            best_matched = matched
            best_prefix_length = len(validated)
            best_validation = copy.deepcopy(dict(validation))

        complete_prefix_valid = (
            validated_prefix_valid
            and len(validated) >= len(requested_steps)
            and validated[: len(requested_steps)] == requested_steps
            and matched == len(facts)
        )
        if (
            validation.get("ok") is True
            and validation.get("complete") is True
            and complete_prefix_valid
        ):
            try:
                program = project_authority_program(
                    descriptor,
                    _program_from_validation(
                        validation,
                        validated,
                        automatic_step_indexes=tuple(sorted(
                            projection_automatic_indexes,
                        )),
                    ),
                )
            except ValueError:
                program = None
            if program is not None and _intent_equivalent(
                submitted,
                program.chain,
                descriptor.action_roles,
            ):
                if program.outcome_key not in compiled_outcomes:
                    compiled_outcomes.add(program.outcome_key)
                    compiled_programs.append(program)
                if (
                    program.chain.actions == submitted.actions
                    or len(compiled_programs) >= 5
                ):
                    ordered = ordered_compiled_programs()
                    return AuthorityCompilation(
                        status=CompilationStatus.COMPLETE,
                        program=ordered[0],
                        matched_facts=matched,
                        requested_facts=len(facts),
                        legal_frontier=(),
                        authority_reason=None,
                        first_unmatched_action=None,
                        validation_calls=validation_calls,
                        alternative_programs=tuple(ordered[1:]),
                        validated_engine_prefix=_frozen_frontier(validated),
                    )

        next_actions = validation.get("nextActions")
        normalized_next_actions = tuple(
            (
                {
                    **copy.deepcopy(dict(action)),
                    "op": action["type"],
                }
                if "op" not in action and isinstance(action.get("type"), str)
                else copy.deepcopy(dict(action))
            )
            for action in (
                next_actions
                if validated_prefix_valid and isinstance(next_actions, list)
                else ()
            )
            if isinstance(action, Mapping)
        )
        if (
            matched == len(facts)
            and validation.get("complete") is not True
            and normalized_next_actions
            and (
                completion_frontier_depth is None
                or len(validated) < completion_frontier_depth
            )
        ):
            # Preserve the first unresolved choice after every submitted
            # semantic fact has matched. Deeper exploratory branches may end
            # with an empty frontier but add Player intent (for example
            # finish_action); they must not erase the choice the model omitted.
            completion_frontier_validation = copy.deepcopy(dict(validation))
            completion_frontier_depth = len(validated)
        if reorder_action_index is None and matched < len(facts):
            expected_owner = fact_owners[matched]
            expected_action = submitted.actions[expected_owner]
            expected_matches = any(
                _matches_fact(
                    step,
                    facts[matched],
                    descriptor=descriptor,
                    owner_action=expected_action,
                )
                for step in normalized_next_actions
            )
            later_match = any(
                owner > expected_owner
                and any(
                    _matches_fact(
                        step,
                        fact,
                        descriptor=descriptor,
                        owner_action=submitted.actions[owner],
                    )
                    for step in normalized_next_actions
                )
                for fact, owner in zip(facts[matched + 1 :], fact_owners[matched + 1 :])
            )
            if not expected_matches and later_match:
                reorder_action_index = expected_owner
                reorder_legal_frontier = tuple(
                    _freeze_json(step) for step in normalized_next_actions
                )
        candidates = _matching_next_actions(
            (
                next_actions
                if validated_prefix_valid and isinstance(next_actions, list)
                else ()
            ),
            facts,
            matched,
            descriptor,
            fact_owners=fact_owners,
            submitted=submitted,
        )
        for action in reversed(candidates):
            transaction_action = copy.deepcopy(dict(action))
            transaction_action.pop("choiceType", None)
            steps = (*validated, transaction_action)
            if len(steps) > max_depth:
                continue
            fingerprint = _canonical(steps)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            frontier.appendleft((
                steps,
                frozenset(projection_automatic_indexes),
            ))

    if compiled_programs:
        ordered = ordered_compiled_programs()
        return AuthorityCompilation(
            status=CompilationStatus.COMPLETE,
            program=ordered[0],
            matched_facts=best_matched,
            requested_facts=len(facts),
            legal_frontier=(),
            authority_reason=None,
            first_unmatched_action=None,
            validation_calls=validation_calls,
            alternative_programs=tuple(ordered[1:]),
            validated_engine_prefix=best_trusted_engine_prefix,
        )

    frontier_validation = completion_frontier_validation or best_validation
    legal_frontier = _frozen_frontier(frontier_validation.get("nextActions"))
    if frontier:
        status = CompilationStatus.BUDGET_EXHAUSTED
        reason = _reason(
            "COMPILATION_BUDGET_EXHAUSTED",
            decisionId=decision_id,
            stateHash=state_hash,
            maxValidationCalls=max_validation_calls,
        )
    elif best_matched >= len(facts) and len(legal_frontier) > 1:
        status = CompilationStatus.INCOMPLETE
        reason = _reason(
            "MANDATORY_CHOICE_OMITTED",
            decisionId=decision_id,
            stateHash=state_hash,
        )
    elif best_matched < len(facts):
        status = CompilationStatus.ILLEGAL
        reason = _reason(
            "SUBMITTED_ACTION_ILLEGAL",
            decisionId=decision_id,
            stateHash=state_hash,
            submittedFact=copy.deepcopy(dict(facts[best_matched])),
        )
    else:
        status = CompilationStatus.INCOMPLETE
        reason = _reason(
            "DECISION_NOT_COMPLETE",
            decisionId=decision_id,
            stateHash=state_hash,
        )
    unmatched_index = (
        fact_owners[best_matched]
        if best_matched < len(fact_owners)
        else None
    )
    return AuthorityCompilation(
        status=status,
        program=None,
        matched_facts=best_matched,
        requested_facts=len(facts),
        legal_frontier=legal_frontier,
        authority_reason=reason,
        first_unmatched_action=(
            submitted.actions[unmatched_index]
            if unmatched_index is not None
            else None
        ),
        validation_calls=validation_calls,
        reorder_action_index=reorder_action_index,
        reorder_legal_frontier=reorder_legal_frontier,
        validated_engine_prefix=best_trusted_engine_prefix,
        # Keep the package's public explanation at the failed intent, including
        # any deterministic continuation. Do not render raw intermediate state
        # or infer game-specific affordability in the shared compiler.
        validated_prefix_summary=(
            validate_public_summary(best_validation.get("publicSummary"))
            if status is CompilationStatus.ILLEGAL and best_matched > 0
            else None
        ),
    )


__all__ = [
    "AuthorityCompilation",
    "CompilationStatus",
    "compile_semantic_chain",
]
