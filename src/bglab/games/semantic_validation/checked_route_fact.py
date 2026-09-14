from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from bglab.games.model_visibility import classify_non_player_field

from .closest import ClosestCandidate, SemanticEditKind, align_semantic_chains
from .divergence import DivergenceCode, FirstDivergence, first_divergence
from .model import SemanticAction, SemanticChain
from .schema import strict_json_equal
from .outcome import validate_public_summary


_VALID_STATUSES = frozenset({
    "exact",
    "authority-completed",
    "needs-choice",
    "distant",
    "nearby",
})
_LABEL_RE = re.compile(r"^(?:C[1-5]|R[1-3]\.C[1-5])$")
_STATUS_FLAG_MATRIX = {
    "exact": (True, True),
    "authority-completed": (True, True),
    "needs-choice": (True, True),
    "nearby": (False, True),
    "distant": (False, True),
}
_PUBLIC_BOUNDARY_CODES = {
    "MANDATORY_CHOICE_OMITTED": "PLAYER_CHOICE_REQUIRED",
    "SUBMITTED_ACTION_ILLEGAL": "SUBMITTED_ROUTE_DIVERGED",
    "DECISION_NOT_COMPLETE": "ROUTE_INCOMPLETE",
    "COMPILATION_BUDGET_EXHAUSTED": "AUTHORITY_CHECK_INCONCLUSIVE",
    "SEMANTIC_MAPPING_MISSING": "AUTHORITY_CHECK_UNAVAILABLE",
}
_PUBLIC_BOUNDARY_CODE_VALUES = frozenset(_PUBLIC_BOUNDARY_CODES.values())
_PROJECTED_OUTCOME_ALIASES = frozenset({
    "remainingAfter",
    "totalAfter",
    "scoreAfterCurrentAction",
})
_FACT_PUBLIC_FIELDS = frozenset({
    "label",
    "status",
    "intentExact",
    "commitReady",
    "semanticChain",
    "outcome",
    "matchedPrefix",
    "firstDivergence",
    "publicSummary",
    "intentChanges",
})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("checked-route public fact object keys must be strings")
            frozen[key] = _freeze(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("checked-route public fact contains a non-finite number")
    if value is None or isinstance(value, (str, bool, int, float)):
        return copy.deepcopy(value)
    raise TypeError(
        "checked-route public fact values must be JSON-compatible",
    )


def thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return copy.deepcopy(value)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (not isinstance(value, float) or math.isfinite(value))
    )


def _reject_hidden_keys(value: Any, *, path: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} object keys must be strings")
            owner = classify_non_player_field(key)
            if owner is not None:
                raise ValueError(
                    f"{path} contains non-player field {key} owned by {owner}",
                )
            _reject_hidden_keys(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_hidden_keys(item, path=f"{path}[{index}]")


def _validate_public_action(value: Any, *, path: str) -> None:
    if not isinstance(value, Mapping) or set(value) != {"action", "args"}:
        raise ValueError(f"{path} must contain exactly action and args")
    if not isinstance(value.get("action"), str) or not value["action"]:
        raise ValueError(f"{path}.action must be non-empty text")
    if not isinstance(value.get("args"), Mapping):
        raise ValueError(f"{path}.args must be an object")
    _freeze(value["args"])
    _reject_hidden_keys(value["args"], path=f"{path}.args")


def _validate_public_semantic_chain(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != {"name", "actions"}:
        raise ValueError(
            "checked-route semanticChain must contain exactly name and actions",
        )
    if not isinstance(value.get("name"), str) or not value["name"]:
        raise ValueError("checked-route semanticChain.name must be non-empty text")
    actions = value.get("actions")
    if not isinstance(actions, (list, tuple)) or not actions:
        raise ValueError(
            "checked-route semanticChain.actions must be a non-empty array",
        )
    for index, action in enumerate(actions):
        _validate_public_action(
            action,
            path=f"checked-route semanticChain.actions[{index}]",
        )


def _validate_public_divergence(value: Any) -> None:
    if value is None:
        return
    expected = {"code", "submittedNext", "candidateNext", "authorityReason"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(
            "checked-route firstDivergence fields are invalid",
        )
    if value.get("code") not in {item.value for item in DivergenceCode}:
        raise ValueError("checked-route firstDivergence.code is invalid")
    for field in ("submittedNext", "candidateNext"):
        action = value.get(field)
        if action is not None:
            _validate_public_action(
                action,
                path=f"checked-route firstDivergence.{field}",
            )
    reason = value.get("authorityReason")
    if reason is not None:
        if not isinstance(reason, Mapping) or set(reason) != {"code"}:
            raise ValueError(
                "checked-route firstDivergence.authorityReason fields are invalid",
            )
        if reason.get("code") not in _PUBLIC_BOUNDARY_CODE_VALUES:
            raise ValueError(
                "checked-route firstDivergence.authorityReason.code is invalid",
            )


def project_checked_route_outcome(outcome: Mapping[str, Any]) -> Mapping[str, Any]:
    """Project stable aliases only from their exact typed Authority paths."""

    if not isinstance(outcome, Mapping):
        raise TypeError("checked-route outcome must be a mapping")
    alias_overlap = _PROJECTED_OUTCOME_ALIASES & set(outcome)
    if alias_overlap:
        raise ValueError(
            "raw checked-route outcome contains reserved projected aliases: "
            + ", ".join(sorted(alias_overlap)),
        )
    projected = thaw(_freeze(outcome))
    _reject_hidden_keys(projected, path="checked-route outcome")
    overlap = _FACT_PUBLIC_FIELDS & set(projected)
    if overlap:
        raise ValueError(
            "checked-route outcome collides with public fact fields: "
            + ", ".join(sorted(overlap)),
        )
    remaining = outcome.get("remaining")
    if isinstance(remaining, Mapping):
        projected["remainingAfter"] = thaw(_freeze(remaining))

    end_score = outcome.get("scoreIfGameEnded")
    if isinstance(end_score, Mapping):
        after = end_score.get("after")
        if isinstance(after, Mapping):
            total = after.get("total")
            if _is_number(total):
                projected["totalAfter"] = copy.deepcopy(total)

    immediate_score = outcome.get("scoreAfter")
    if _is_number(immediate_score):
        projected["scoreAfterCurrentAction"] = copy.deepcopy(immediate_score)
    return _freeze(projected)


def _validate_projected_outcome(outcome: Mapping[str, Any]) -> Mapping[str, Any]:
    """Validate one already-projected fact outcome without reclassifying it as raw."""

    if not isinstance(outcome, Mapping):
        raise TypeError("checked-route outcome must be a mapping")
    supplied = thaw(_freeze(outcome))
    _reject_hidden_keys(supplied, path="checked-route outcome")
    overlap = _FACT_PUBLIC_FIELDS & set(supplied)
    if overlap:
        raise ValueError(
            "checked-route outcome collides with public fact fields: "
            + ", ".join(sorted(overlap)),
        )
    raw = {
        key: value
        for key, value in supplied.items()
        if key not in _PROJECTED_OUTCOME_ALIASES
    }
    projected = project_checked_route_outcome(raw)
    if not strict_json_equal(supplied, thaw(projected)):
        raise ValueError(
            "checked-route outcome aliases do not match exact typed paths",
        )
    return projected


def _model_owned_chain(
    chain: SemanticChain,
    action_roles: Mapping[str, str],
) -> SemanticChain:
    return SemanticChain(
        name=str(chain.name),
        actions=tuple(
            SemanticAction.from_mapping(action.action, dict(action.args))
            for action in chain.actions
            if action_roles.get(action.action, "intent") != "derived"
        ),
    )


def _chain_dict(chain: SemanticChain) -> dict[str, Any]:
    return {
        "name": chain.name,
        "actions": [action.to_dict() for action in chain.actions],
    }


def _status(
    candidate: ClosestCandidate,
    action_roles: Mapping[str, str],
) -> str:
    if candidate.alignment.total_cost == 0:
        return "exact"
    if candidate.alignment.edits and all(
        edit.kind is SemanticEditKind.INSERT
        for edit in candidate.alignment.edits
    ):
        inserted_roles = tuple(
            action_roles.get(edit.candidate.action, "intent")
            for edit in candidate.alignment.edits
            if edit.candidate is not None
        )
        if inserted_roles and all(role == "derived" for role in inserted_roles):
            return "authority-completed"
        if inserted_roles and all(
            role in {"derived", "choice"} for role in inserted_roles
        ):
            return "needs-choice"
    if candidate.alignment.root_changed:
        return "distant"
    return "nearby"


def _action_dict(action: SemanticAction | None) -> dict[str, Any] | None:
    return action.to_dict() if action is not None else None


def _divergence_dict(divergence: FirstDivergence) -> Mapping[str, Any] | None:
    if (
        divergence.code is DivergenceCode.EXACT
        and not divergence.legal_frontier
        and divergence.authority_reason is None
    ):
        return None
    return {
        "code": divergence.code.value,
        "submittedNext": _action_dict(divergence.submitted_next),
        "candidateNext": _action_dict(divergence.candidate_next),
        "authorityReason": thaw(divergence.authority_reason),
    }


def _public_authority_reason(
    authority_reason: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    if not isinstance(authority_reason, Mapping):
        return None
    internal_code = authority_reason.get("code")
    if not isinstance(internal_code, str):
        return None
    public_code = _PUBLIC_BOUNDARY_CODES.get(internal_code)
    return {"code": public_code} if public_code is not None else None


def _build_intent_changes(submitted: SemanticChain, candidate: SemanticChain) -> dict[str, Any]:
    alignment = align_semantic_chains(submitted, candidate)
    changed = {edit.submitted_index for edit in alignment.edits if edit.submitted_index is not None}
    return {
        "submittedCount": len(submitted.actions),
        "preserved": [index + 1 for index in range(len(submitted.actions)) if index not in changed],
        "changes": [{
            "kind": edit.kind.value,
            "submittedIndex": edit.submitted_index + 1 if edit.submitted_index is not None else None,
            "candidateIndex": edit.candidate_index + 1 if edit.candidate_index is not None else None,
            "submitted": _action_dict(edit.submitted),
            "candidate": _action_dict(edit.candidate),
        } for edit in alignment.edits],
    }


def _validate_intent_changes(value: Any, candidate_actions: Sequence[Any]) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping) or set(value) != {"submittedCount", "preserved", "changes"}:
        raise ValueError("checked-route intentChanges fields are invalid")
    count, preserved, changes = value["submittedCount"], value["preserved"], value["changes"]
    if type(count) is not int or count < 0:
        raise ValueError("intentChanges submittedCount must be a non-negative integer")
    if not isinstance(preserved, (list, tuple)) or any(type(i) is not int or not 1 <= i <= count for i in preserved):
        raise ValueError("intentChanges preserved indexes are invalid")
    if list(preserved) != sorted(set(preserved)) or not isinstance(changes, (list, tuple)):
        raise ValueError("intentChanges indexes must be unique and changes must be an array")
    submitted_indexes = list(preserved)
    candidate_indexes: list[int] = []
    for change in changes:
        if not isinstance(change, Mapping) or set(change) != {"kind", "submittedIndex", "candidateIndex", "submitted", "candidate"}:
            raise ValueError("intentChanges change fields are invalid")
        kind = change["kind"]
        if kind not in {item.value for item in SemanticEditKind}:
            raise ValueError("intentChanges change kind is invalid")
        for side, absent, limit in (
            ("submitted", kind == SemanticEditKind.INSERT, count),
            ("candidate", kind == SemanticEditKind.DELETE, len(candidate_actions)),
        ):
            index, action = change[side + "Index"], change[side]
            if absent:
                if index is not None or action is not None:
                    raise ValueError("intentChanges absent edit side must be null")
                continue
            if type(index) is not int or not 1 <= index <= limit:
                raise ValueError("intentChanges edit index is invalid")
            _validate_public_action(action, path="intentChanges." + side)
            if side == "submitted":
                submitted_indexes.append(index)
            else:
                candidate_indexes.append(index)
                if not strict_json_equal(thaw(action), candidate_actions[index - 1]):
                    raise ValueError("intentChanges candidate drifted from semanticChain")
    if sorted(submitted_indexes) != list(range(1, count + 1)) or len(candidate_indexes) != len(set(candidate_indexes)):
        raise ValueError("intentChanges must account for each submitted action exactly once")
    # Remaining positions on each side must be the unchanged matches.
    if len(candidate_actions) - len(candidate_indexes) != len(preserved):
        raise ValueError("intentChanges preserved count does not match semanticChain")


@dataclass(frozen=True, slots=True)
class CheckedRouteFact:
    label: str
    status: str
    intent_exact: bool
    commit_ready: bool
    semantic_chain: Mapping[str, Any]
    outcome: Mapping[str, Any]
    matched_prefix: int = 0
    first_divergence: Mapping[str, Any] | None = None
    public_summary: str | None = None
    intent_changes: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or _LABEL_RE.fullmatch(self.label) is None:
            raise ValueError("checked-route label must be C1-C5 or R1.C1-R3.C5")
        if self.status not in _VALID_STATUSES:
            raise ValueError("checked-route status is not a stable public status")
        if not isinstance(self.intent_exact, bool):
            raise TypeError("checked-route intent_exact must be bool")
        if not isinstance(self.commit_ready, bool):
            raise TypeError("checked-route commit_ready must be bool")
        expected_flags = _STATUS_FLAG_MATRIX[self.status]
        if (self.intent_exact, self.commit_ready) != expected_flags:
            raise ValueError(
                "checked-route status and intent/commit flags are inconsistent",
            )
        if not isinstance(self.semantic_chain, Mapping):
            raise TypeError("checked-route semantic_chain must be a mapping")
        if not isinstance(self.outcome, Mapping):
            raise TypeError("checked-route outcome must be a mapping")
        if (
            isinstance(self.matched_prefix, bool)
            or not isinstance(self.matched_prefix, int)
            or self.matched_prefix < 0
        ):
            raise ValueError("checked-route matched_prefix must be a non-negative int")
        if self.first_divergence is not None and not isinstance(
            self.first_divergence,
            Mapping,
        ):
            raise TypeError("checked-route first_divergence must be a mapping or None")
        validate_public_summary(self.public_summary)
        _validate_public_semantic_chain(self.semantic_chain)
        _validate_intent_changes(self.intent_changes, self.semantic_chain["actions"])
        action_count = len(self.semantic_chain["actions"])
        if self.matched_prefix > action_count:
            raise ValueError(
                "checked-route matched_prefix exceeds semanticChain action count",
            )
        _validate_public_divergence(self.first_divergence)
        projected_outcome = _validate_projected_outcome(self.outcome)
        object.__setattr__(self, "semantic_chain", _freeze(self.semantic_chain))
        object.__setattr__(self, "outcome", projected_outcome)
        object.__setattr__(self, "intent_changes", _freeze(self.intent_changes))
        object.__setattr__(
            self,
            "first_divergence",
            _freeze(self.first_divergence)
            if self.first_divergence is not None
            else None,
        )

    def with_label(self, label: str) -> CheckedRouteFact:
        """Return the same immutable fact with one display label."""

        return replace(self, label=label)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CheckedRouteFact:
        if not isinstance(value, Mapping):
            raise TypeError("checked-route fact must be an object")
        expected = _FACT_PUBLIC_FIELDS - {"publicSummary", "intentChanges"}
        if not expected <= set(value) or set(value) - _FACT_PUBLIC_FIELDS:
            missing = sorted(expected - set(value))
            unknown = sorted(str(key) for key in set(value) - _FACT_PUBLIC_FIELDS)
            details = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if unknown:
                details.append("unknown=" + ",".join(unknown))
            raise ValueError(
                "checked-route fact fields are invalid: " + "; ".join(details),
            )
        return cls(
            label=value["label"],
            status=value["status"],
            intent_exact=value["intentExact"],
            commit_ready=value["commitReady"],
            semantic_chain=value["semanticChain"],
            outcome=value["outcome"],
            matched_prefix=value["matchedPrefix"],
            first_divergence=value["firstDivergence"],
            public_summary=validate_public_summary(
                value.get("publicSummary"), required="publicSummary" in value,
            ),
            intent_changes=value.get("intentChanges"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "status": self.status,
            "intentExact": self.intent_exact,
            "commitReady": self.commit_ready,
            "semanticChain": thaw(self.semantic_chain),
            "outcome": thaw(self.outcome),
            "matchedPrefix": self.matched_prefix,
            "firstDivergence": thaw(self.first_divergence),
            **({"publicSummary": self.public_summary} if self.public_summary is not None else {}),
            **({"intentChanges": thaw(self.intent_changes)} if self.intent_changes is not None else {}),
        }


def build_checked_route_fact(
    *,
    submitted: SemanticChain,
    candidate: ClosestCandidate,
    action_roles: Mapping[str, str] | None = None,
    authority_reason: Mapping[str, Any] | None = None,
) -> CheckedRouteFact:
    """Build one request-local public fact from one Authority candidate."""

    if not isinstance(candidate, ClosestCandidate):
        raise TypeError("candidate must be ClosestCandidate")
    roles = action_roles or {}
    candidate_chain = _model_owned_chain(candidate.program.chain, roles)
    visible_chain = SemanticChain(
        name=str(submitted.name),
        actions=candidate_chain.actions,
    )
    semantic_chain = _chain_dict(visible_chain)
    _validate_public_semantic_chain(semantic_chain)
    divergence = first_divergence(
        submitted,
        visible_chain,
        authority_reason=(
            _public_authority_reason(authority_reason)
        ),
    )
    return CheckedRouteFact(
        label=candidate.label,
        status=_status(candidate, roles),
        intent_exact=candidate.intent_exact,
        commit_ready=candidate.commit_ready,
        semantic_chain=semantic_chain,
        outcome=project_checked_route_outcome(candidate.program.outcome),
        public_summary=candidate.program.public_summary,
        matched_prefix=divergence.matched_prefix,
        first_divergence=_divergence_dict(divergence),
        intent_changes=_build_intent_changes(_model_owned_chain(submitted, roles), visible_chain),
    )


def structured_checked_route_table(
    facts: Sequence[CheckedRouteFact],
) -> tuple[dict[str, Any], ...]:
    if not facts:
        raise ValueError("checked-route table must be non-empty")
    if any(not isinstance(fact, CheckedRouteFact) for fact in facts):
        raise TypeError("checked-route table accepts CheckedRouteFact values only")
    labels = tuple(fact.label for fact in facts)
    if len(set(labels)) != len(labels):
        raise ValueError("checked-route table labels must be unique")
    rows: list[dict[str, Any]] = []
    for fact in facts:
        row = fact.to_dict()
        outcome = row.pop("outcome")
        overlap = set(row) & set(outcome)
        if overlap:
            raise ValueError(
                "checked-route outcome collides with public fact fields: "
                + ", ".join(sorted(overlap)),
            )
        row.update(outcome)
        rows.append(row)
    return tuple(rows)


__all__ = [
    "CheckedRouteFact",
    "build_checked_route_fact",
    "project_checked_route_outcome",
    "structured_checked_route_table",
    "thaw",
]
