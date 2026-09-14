from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .closest import SemanticEditKind, _freeze_json, align_semantic_chains
from .model import SemanticAction, SemanticChain


class DivergenceCode(StrEnum):
    EXACT = "EXACT"
    CANDIDATE_INSERTED_ACTION = "CANDIDATE_INSERTED_ACTION"
    SUBMITTED_ACTION_OMITTED = "SUBMITTED_ACTION_OMITTED"
    ARGUMENT_CHANGED = "ARGUMENT_CHANGED"
    ACTION_CHANGED = "ACTION_CHANGED"
    ROOT_CHANGED = "ROOT_CHANGED"


@dataclass(frozen=True)
class FirstDivergence:
    code: DivergenceCode
    matched_prefix: int
    submitted_next: SemanticAction | None
    candidate_next: SemanticAction | None
    legal_frontier: tuple[SemanticAction, ...]
    authority_reason: Mapping[str, Any] | None


def _copy_action(value: SemanticAction) -> SemanticAction:
    return SemanticAction.from_mapping(value.action, dict(value.args))


def first_divergence(
    submitted: SemanticChain,
    candidate: SemanticChain,
    *,
    legal_frontier: Sequence[SemanticAction] = (),
    authority_reason: Mapping[str, Any] | None = None,
) -> FirstDivergence:
    prefix = 0
    while (
        prefix < len(submitted.actions)
        and prefix < len(candidate.actions)
        and submitted.actions[prefix] == candidate.actions[prefix]
    ):
        prefix += 1

    submitted_next = (
        _copy_action(submitted.actions[prefix])
        if prefix < len(submitted.actions)
        else None
    )
    candidate_next = (
        _copy_action(candidate.actions[prefix])
        if prefix < len(candidate.actions)
        else None
    )
    frozen_frontier = tuple(_copy_action(item) for item in legal_frontier)
    frozen_reason = _freeze_json(authority_reason) if authority_reason is not None else None

    if submitted_next is None and candidate_next is None:
        code = DivergenceCode.EXACT
    elif prefix == 0:
        code = DivergenceCode.ROOT_CHANGED
    else:
        alignment = align_semantic_chains(submitted, candidate)
        first_edit = next(
            (
                edit
                for edit in alignment.edits
                if edit.submitted_index == prefix or edit.candidate_index == prefix
            ),
            None,
        )
        if first_edit is not None and first_edit.kind is SemanticEditKind.INSERT:
            code = DivergenceCode.CANDIDATE_INSERTED_ACTION
        elif first_edit is not None and first_edit.kind is SemanticEditKind.DELETE:
            code = DivergenceCode.SUBMITTED_ACTION_OMITTED
        elif submitted_next is None:
            code = DivergenceCode.CANDIDATE_INSERTED_ACTION
        elif candidate_next is None:
            code = DivergenceCode.SUBMITTED_ACTION_OMITTED
        elif submitted_next.action == candidate_next.action:
            code = DivergenceCode.ARGUMENT_CHANGED
        else:
            code = DivergenceCode.ACTION_CHANGED

    return FirstDivergence(
        code=code,
        matched_prefix=prefix,
        submitted_next=submitted_next,
        candidate_next=candidate_next,
        legal_frontier=frozen_frontier,
        authority_reason=frozen_reason,
    )
