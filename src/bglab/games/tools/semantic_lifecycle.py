from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

from bglab.games.semantic_validation.fingerprint import canonical_steps_fingerprint


_CANDIDATE_LABEL_RE = re.compile(r"C[1-5]\Z")
_DEFERRED_CANDIDATE_LABEL_RE = re.compile(r"(?:C[1-5]|R[1-3]\.C[1-5])\Z")
_COMMIT_FENCE_STATUSES = frozenset(
    {
        "prepared",
        "authority_committed",
        "sink_confirmed",
        "sink_failed",
        "indeterminate",
    },
)
_SEMANTIC_LIFECYCLE_VERSION = 4
_LEGACY_SEMANTIC_LIFECYCLE_VERSION = 2


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()},
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return copy.deepcopy(value)
    raise TypeError("value must contain only JSON-compatible data")


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return copy.deepcopy(value)


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _version_value(value: Any) -> bool:
    return (
        _nonempty_string(value)
    ) or (
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
    )


def _semantic_chain_actions(value: Any) -> list[dict[str, Any]] | None:
    """Return the exact model-owned action sequence, ignoring display metadata."""

    if not isinstance(value, Mapping):
        return None
    actions = value.get("actions")
    if not isinstance(actions, (list, tuple)) or not actions:
        return None
    normalized: list[dict[str, Any]] = []
    for action in actions:
        if not isinstance(action, Mapping) or not action:
            return None
        try:
            thawed = _thaw_json(_freeze_json(action))
        except TypeError:
            return None
        if not isinstance(thawed, dict):
            return None
        normalized.append(thawed)
    return normalized


def semantic_chain_fingerprint(value: Any) -> str:
    """Fingerprint only ordered semantic actions, never a model-facing name."""

    actions = _semantic_chain_actions(value)
    if actions is None:
        raise ValueError("semantic chain must contain non-empty actions")
    return _fingerprint({"actions": actions})


@dataclass(frozen=True, slots=True)
class SemanticDecisionIdentity:
    session_id: str
    game_id: str
    turn_group_id: str
    decision_id: str
    seat: int
    state_hash: str
    snapshot_revision: int | str
    rules_version: int | str
    adapter_version: int | str
    action_protocol: str
    input_surface_hash: str
    retrieval_surface_hash: str

    def __post_init__(self) -> None:
        string_values = (
            self.session_id,
            self.game_id,
            self.turn_group_id,
            self.decision_id,
            self.state_hash,
            self.action_protocol,
            self.input_surface_hash,
            self.retrieval_surface_hash,
        )
        if any(not _nonempty_string(value) for value in string_values):
            raise ValueError("identity string fields must be non-empty")
        if (
            isinstance(self.seat, bool)
            or not isinstance(self.seat, int)
            or self.seat < 0
        ):
            raise ValueError("seat must be a non-negative integer")
        if any(
            not _version_value(value)
            for value in (
                self.snapshot_revision,
                self.rules_version,
                self.adapter_version,
            )
        ):
            raise ValueError("identity versions must be non-empty")

    @classmethod
    def from_ctx(
        cls,
        ctx: Any,
    ) -> SemanticDecisionIdentity | None:
        required = {
            "session_id": "_session_id",
            "game_id": "_game_id",
            "turn_group_id": "_turn_group_id",
            "decision_id": "_decision_id",
            "seat": "_seat",
            "state_hash": "_authority_state_hash",
            "snapshot_revision": "_snapshot_revision",
            "rules_version": "_rules_version",
            "adapter_version": "_adapter_version",
            "action_protocol": "_action_protocol",
            "input_surface_hash": "_semantic_input_surface_hash",
            "retrieval_surface_hash": "_semantic_retrieval_surface_hash",
        }
        if not isinstance(ctx, Mapping):
            return None
        if any(key not in ctx for key in required.values()):
            return None
        values = {name: ctx[key] for name, key in required.items()}
        string_fields = (
            "session_id",
            "game_id",
            "turn_group_id",
            "decision_id",
            "state_hash",
            "action_protocol",
            "input_surface_hash",
            "retrieval_surface_hash",
        )
        if any(not _nonempty_string(values[name]) for name in string_fields):
            return None
        seat = values["seat"]
        if isinstance(seat, bool) or not isinstance(seat, int) or seat < 0:
            return None
        if any(
            not _version_value(values[name])
            for name in ("snapshot_revision", "rules_version", "adapter_version")
        ):
            return None
        return cls(**values)

    @classmethod
    def from_dict(
        cls,
        value: Any,
    ) -> SemanticDecisionIdentity | None:
        fields = {
            "sessionId": "session_id",
            "gameId": "game_id",
            "turnGroupId": "turn_group_id",
            "decisionId": "decision_id",
            "seat": "seat",
            "stateHash": "state_hash",
            "snapshotRevision": "snapshot_revision",
            "rulesVersion": "rules_version",
            "adapterVersion": "adapter_version",
            "actionProtocol": "action_protocol",
            "inputSurfaceHash": "input_surface_hash",
            "retrievalSurfaceHash": "retrieval_surface_hash",
        }
        if not isinstance(value, Mapping) or set(value) != set(fields):
            return None
        try:
            return cls(**{target: value[source] for source, target in fields.items()})
        except (TypeError, ValueError):
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "gameId": self.game_id,
            "turnGroupId": self.turn_group_id,
            "decisionId": self.decision_id,
            "seat": self.seat,
            "stateHash": self.state_hash,
            "snapshotRevision": copy.deepcopy(self.snapshot_revision),
            "rulesVersion": copy.deepcopy(self.rules_version),
            "adapterVersion": copy.deepcopy(self.adapter_version),
            "actionProtocol": self.action_protocol,
            "inputSurfaceHash": self.input_surface_hash,
            "retrievalSurfaceHash": self.retrieval_surface_hash,
        }


@dataclass(frozen=True, slots=True)
class BoundCandidate:
    label: str
    program_id: str
    engine_steps: tuple[Mapping[str, Any], ...]
    canonical_steps_fingerprint: str
    binding_fingerprint: str
    intent_exact: bool
    commit_ready: bool
    semantic_chain: Mapping[str, Any]

    def __post_init__(self) -> None:
        if _CANDIDATE_LABEL_RE.fullmatch(self.label) is None:
            raise ValueError("candidate label must be C1-C5")
        if not _nonempty_string(self.program_id):
            raise ValueError("program_id must be a non-empty string")
        if not isinstance(self.engine_steps, (list, tuple)) or not self.engine_steps:
            raise ValueError("engine_steps must be a non-empty sequence")
        if any(not isinstance(step, Mapping) or not step for step in self.engine_steps):
            raise ValueError("every engine step must be a non-empty object")
        if not _nonempty_string(self.canonical_steps_fingerprint):
            raise ValueError("canonical_steps_fingerprint must be non-empty")
        if not _nonempty_string(self.binding_fingerprint):
            raise ValueError("binding_fingerprint must be non-empty")
        if not isinstance(self.intent_exact, bool):
            raise ValueError("intent_exact must be a boolean")
        if not isinstance(self.commit_ready, bool):
            raise ValueError("commit_ready must be a boolean")
        if not isinstance(self.semantic_chain, Mapping) or not self.semantic_chain:
            raise ValueError("semantic_chain must be a non-empty object")
        object.__setattr__(
            self,
            "engine_steps",
            tuple(_freeze_json(step) for step in self.engine_steps),
        )
        object.__setattr__(self, "semantic_chain", _freeze_json(self.semantic_chain))

    @classmethod
    def from_dict(cls, value: Any) -> BoundCandidate | None:
        required = {
            "label",
            "programId",
            "engineSteps",
            "canonicalStepsFingerprint",
            "bindingFingerprint",
            "intentExact",
            "commitReady",
            "semanticChain",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            return None
        try:
            return cls(
                label=value["label"],
                program_id=value["programId"],
                engine_steps=value["engineSteps"],
                canonical_steps_fingerprint=value["canonicalStepsFingerprint"],
                binding_fingerprint=value["bindingFingerprint"],
                intent_exact=value["intentExact"],
                commit_ready=value["commitReady"],
                semantic_chain=value["semanticChain"],
            )
        except (TypeError, ValueError):
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "programId": self.program_id,
            "engineSteps": _thaw_json(self.engine_steps),
            "canonicalStepsFingerprint": self.canonical_steps_fingerprint,
            "bindingFingerprint": self.binding_fingerprint,
            "intentExact": self.intent_exact,
            "commitReady": self.commit_ready,
            "semanticChain": _thaw_json(self.semantic_chain),
        }


@dataclass(frozen=True, slots=True)
class DeferredAuthorityCandidate:
    """Authority-validated candidate retained internally for exact later reuse."""

    source_label: str
    program_id: str
    engine_steps: tuple[Mapping[str, Any], ...]
    canonical_steps_fingerprint: str
    binding_fingerprint: str
    intent_exact: bool
    semantic_chain: Mapping[str, Any]
    semantic_chain_fingerprint: str

    def __post_init__(self) -> None:
        if _DEFERRED_CANDIDATE_LABEL_RE.fullmatch(self.source_label) is None:
            raise ValueError("deferred source label must be C1-C5 or R1.C1-R3.C5")
        if not _nonempty_string(self.program_id):
            raise ValueError("program_id must be a non-empty string")
        if not isinstance(self.engine_steps, (list, tuple)) or not self.engine_steps:
            raise ValueError("engine_steps must be a non-empty sequence")
        if any(not isinstance(step, Mapping) or not step for step in self.engine_steps):
            raise ValueError("every engine step must be a non-empty object")
        if not _nonempty_string(self.canonical_steps_fingerprint):
            raise ValueError("canonical_steps_fingerprint must be non-empty")
        if not _nonempty_string(self.binding_fingerprint):
            raise ValueError("binding_fingerprint must be non-empty")
        if not isinstance(self.intent_exact, bool):
            raise ValueError("intent_exact must be a boolean")
        if not isinstance(self.semantic_chain, Mapping) or not self.semantic_chain:
            raise ValueError("semantic_chain must be a non-empty object")
        if not _nonempty_string(self.semantic_chain_fingerprint):
            raise ValueError("semantic_chain_fingerprint must be non-empty")
        if semantic_chain_fingerprint(self.semantic_chain) != self.semantic_chain_fingerprint:
            raise ValueError("semantic chain fingerprint does not match")
        object.__setattr__(
            self,
            "engine_steps",
            tuple(_freeze_json(step) for step in self.engine_steps),
        )
        object.__setattr__(self, "semantic_chain", _freeze_json(self.semantic_chain))

    @classmethod
    def from_dict(cls, value: Any) -> DeferredAuthorityCandidate | None:
        required = {
            "sourceLabel",
            "programId",
            "engineSteps",
            "canonicalStepsFingerprint",
            "bindingFingerprint",
            "intentExact",
            "semanticChain",
            "semanticChainFingerprint",
        }
        if not isinstance(value, Mapping) or set(value) != required:
            return None
        try:
            return cls(
                source_label=value["sourceLabel"],
                program_id=value["programId"],
                engine_steps=value["engineSteps"],
                canonical_steps_fingerprint=value["canonicalStepsFingerprint"],
                binding_fingerprint=value["bindingFingerprint"],
                intent_exact=value["intentExact"],
                semantic_chain=value["semanticChain"],
                semantic_chain_fingerprint=value["semanticChainFingerprint"],
            )
        except (TypeError, ValueError):
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceLabel": self.source_label,
            "programId": self.program_id,
            "engineSteps": _thaw_json(self.engine_steps),
            "canonicalStepsFingerprint": self.canonical_steps_fingerprint,
            "bindingFingerprint": self.binding_fingerprint,
            "intentExact": self.intent_exact,
            "semanticChain": _thaw_json(self.semantic_chain),
            "semanticChainFingerprint": self.semantic_chain_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class CommitFence:
    candidate_label: str
    delivery_fingerprint: str
    status: str

    def __post_init__(self) -> None:
        if _CANDIDATE_LABEL_RE.fullmatch(self.candidate_label) is None:
            raise ValueError("candidate_label must be C1-C5")
        if not _nonempty_string(self.delivery_fingerprint):
            raise ValueError("delivery_fingerprint must be non-empty")
        if self.status not in _COMMIT_FENCE_STATUSES:
            raise ValueError("invalid commit fence status")

    @classmethod
    def from_dict(cls, value: Any) -> CommitFence | None:
        required = {"candidateLabel", "deliveryFingerprint", "status"}
        if not isinstance(value, Mapping) or set(value) != required:
            return None
        try:
            return cls(
                candidate_label=value["candidateLabel"],
                delivery_fingerprint=value["deliveryFingerprint"],
                status=value["status"],
            )
        except (TypeError, ValueError):
            return None

    def to_dict(self) -> dict[str, str]:
        return {
            "candidateLabel": self.candidate_label,
            "deliveryFingerprint": self.delivery_fingerprint,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class SemanticLifecycle:
    identity: SemanticDecisionIdentity
    candidates: Mapping[str, BoundCandidate]
    deferred_candidates: Mapping[str, DeferredAuthorityCandidate] = field(
        default_factory=dict,
    )
    commit_fence: CommitFence | None = None
    version: int = _SEMANTIC_LIFECYCLE_VERSION
    checked_candidates: Mapping[str, BoundCandidate] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.version != _SEMANTIC_LIFECYCLE_VERSION:
            raise ValueError("semantic lifecycle version must be 4")
        if not isinstance(self.identity, SemanticDecisionIdentity):
            raise TypeError("identity must be SemanticDecisionIdentity")
        if not isinstance(self.candidates, Mapping):
            raise ValueError("candidates must be a mapping")
        if not isinstance(self.deferred_candidates, Mapping):
            raise ValueError("deferred_candidates must be a mapping")
        if not isinstance(self.checked_candidates, Mapping):
            raise ValueError("checked_candidates must be a mapping")
        if not self.candidates and not self.deferred_candidates and not self.checked_candidates:
            raise ValueError("lifecycle must bind a commit-ready or deferred candidate")
        if len(self.candidates) > 5:
            raise ValueError("at most five candidates may be bound")
        if len(self.deferred_candidates) > 15:
            raise ValueError("at most fifteen deferred candidates may be bound")
        expected_labels = {
            f"C{index}" for index in range(1, len(self.candidates) + 1)
        }
        if set(self.candidates) != expected_labels:
            raise ValueError("candidate labels must be contiguous from C1")
        ordered: dict[str, BoundCandidate] = {}
        for label in sorted(self.candidates, key=lambda item: int(item[1:])):
            candidate = self.candidates[label]
            if not isinstance(candidate, BoundCandidate) or candidate.label != label:
                raise ValueError("candidate mapping keys must match candidate labels")
            if not validate_candidate_canonical_steps_fingerprint(candidate):
                raise ValueError("candidate canonical steps fingerprint does not match")
            ordered[label] = candidate
        ordered_deferred: dict[str, DeferredAuthorityCandidate] = {}
        for label in sorted(self.deferred_candidates):
            candidate = self.deferred_candidates[label]
            if (
                not isinstance(candidate, DeferredAuthorityCandidate)
                or candidate.source_label != label
            ):
                raise ValueError(
                    "deferred candidate mapping keys must match source labels",
                )
            if not validate_deferred_candidate_canonical_steps_fingerprint(candidate):
                raise ValueError(
                    "deferred candidate canonical steps fingerprint does not match",
                )
            ordered_deferred[label] = candidate
        if self.commit_fence is not None:
            candidate = ordered.get(self.commit_fence.candidate_label)
            if candidate is None:
                raise ValueError("commit fence candidate is not bound")
            if self.commit_fence.delivery_fingerprint != semantic_delivery_fingerprint(
                self.identity,
                candidate,
            ):
                raise ValueError("commit fence delivery fingerprint does not match")
        object.__setattr__(self, "candidates", MappingProxyType(ordered))
        object.__setattr__(
            self,
            "deferred_candidates",
            MappingProxyType(ordered_deferred),
        )
        checked = {}
        for fingerprint, candidate in self.checked_candidates.items():
            if (
                not isinstance(candidate, BoundCandidate)
                or candidate.commit_ready is not True
                or fingerprint != semantic_delivery_fingerprint(self.identity, candidate)
            ):
                raise ValueError("checked candidate must match its full delivery fingerprint")
            checked[fingerprint] = candidate
        object.__setattr__(self, "checked_candidates", MappingProxyType(checked))


def all_checked_candidates(lifecycle: SemanticLifecycle) -> tuple[BoundCandidate, ...]:
    """Deduplicate exact bindings; a truncated public ID collision remains ambiguous."""
    checked = dict(lifecycle.checked_candidates)
    for candidate in lifecycle.candidates.values():
        checked[semantic_delivery_fingerprint(lifecycle.identity, candidate)] = candidate
    return tuple(checked.values())


def retain_checked_candidates(
    current: SemanticLifecycle | None,
    identity: SemanticDecisionIdentity,
    candidates: Mapping[str, BoundCandidate],
) -> SemanticLifecycle | None:
    """A read-only Check changes the displayed batch, not earlier valid bindings."""
    checked = {}
    if current is not None and not semantic_identity_is_stale(current.identity, identity):
        if current.commit_fence is not None:
            raise ValueError("cannot replace a lifecycle with an active commit fence")
        checked.update({
            semantic_delivery_fingerprint(identity, candidate): replace(candidate, label="C1")
            for candidate in all_checked_candidates(current)
        })
    if not candidates and not checked:
        return None
    return SemanticLifecycle(identity=identity, candidates=candidates, checked_candidates=checked)


def semantic_identity_from_ctx(
    ctx: Any,
) -> SemanticDecisionIdentity | None:
    return SemanticDecisionIdentity.from_ctx(ctx)


def semantic_identity_is_stale(
    stored: Any,
    current: Any,
) -> bool:
    if not isinstance(stored, SemanticDecisionIdentity):
        return True
    if not isinstance(current, SemanticDecisionIdentity):
        return True
    return stored != current


def validate_candidate_canonical_steps_fingerprint(
    candidate: Any,
) -> bool:
    if not isinstance(candidate, BoundCandidate):
        return False
    steps = _thaw_json(candidate.engine_steps)
    return (
        candidate.canonical_steps_fingerprint
        == canonical_steps_fingerprint(steps)
    )


def validate_deferred_candidate_canonical_steps_fingerprint(
    candidate: Any,
) -> bool:
    if not isinstance(candidate, DeferredAuthorityCandidate):
        return False
    steps = _thaw_json(candidate.engine_steps)
    return (
        candidate.canonical_steps_fingerprint
        == canonical_steps_fingerprint(steps)
    )


def match_deferred_candidate(
    lifecycle: Any,
    semantic_chain: Any,
) -> DeferredAuthorityCandidate | None:
    """Return one exact authority candidate; ambiguity always fails closed."""

    if not isinstance(lifecycle, SemanticLifecycle):
        return None
    actions = _semantic_chain_actions(semantic_chain)
    if actions is None:
        return None
    fingerprint = _fingerprint({"actions": actions})
    matches = []
    for candidate in lifecycle.deferred_candidates.values():
        if candidate.semantic_chain_fingerprint != fingerprint:
            continue
        if not validate_deferred_candidate_canonical_steps_fingerprint(candidate):
            continue
        candidate_actions = _semantic_chain_actions(candidate.semantic_chain)
        if candidate_actions == actions:
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def match_bound_candidate(
    lifecycle: Any,
    semantic_chain: Any,
) -> BoundCandidate | None:
    """Return one exact public binding; labels and display names are irrelevant."""

    if not isinstance(lifecycle, SemanticLifecycle):
        return None
    actions = _semantic_chain_actions(semantic_chain)
    if actions is None:
        return None
    fingerprint = _fingerprint({"actions": actions})
    matches = []
    for candidate in all_checked_candidates(lifecycle):
        if not validate_candidate_canonical_steps_fingerprint(candidate):
            continue
        try:
            candidate_fingerprint = semantic_chain_fingerprint(
                candidate.semantic_chain,
            )
        except ValueError:
            continue
        if candidate_fingerprint != fingerprint:
            continue
        if _semantic_chain_actions(candidate.semantic_chain) == actions:
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def promote_deferred_candidate(
    candidate: DeferredAuthorityCandidate,
) -> BoundCandidate:
    if not validate_deferred_candidate_canonical_steps_fingerprint(candidate):
        raise ValueError("deferred candidate canonical steps fingerprint does not match")
    return BoundCandidate(
        label="C1",
        program_id=candidate.program_id,
        engine_steps=candidate.engine_steps,
        canonical_steps_fingerprint=candidate.canonical_steps_fingerprint,
        binding_fingerprint=candidate.binding_fingerprint,
        intent_exact=True,
        commit_ready=True,
        semantic_chain=candidate.semantic_chain,
    )


def semantic_delivery_fingerprint(
    identity: SemanticDecisionIdentity,
    candidate: BoundCandidate,
) -> str:
    if not validate_candidate_canonical_steps_fingerprint(candidate):
        raise ValueError("candidate canonical steps fingerprint does not match")
    return _fingerprint(
        {
            "identity": identity.to_dict(),
            "canonicalStepsFingerprint": (
                candidate.canonical_steps_fingerprint
            ),
            "bindingFingerprint": candidate.binding_fingerprint,
        },
    )


def semantic_route_id(identity: SemanticDecisionIdentity, candidate: BoundCandidate) -> str:
    """Public reference to a concrete transaction, independent of its display rank.

    Recomputed from the persisted binding: no mutable ID counter or parallel store.
    Resolution must match exactly one candidate, so even a collision fails closed.
    """
    return "r" + semantic_delivery_fingerprint(identity, candidate)[:24]


def serialize_semantic_lifecycle(
    lifecycle: SemanticLifecycle,
) -> dict[str, Any]:
    payload = {
        "version": lifecycle.version,
        "identity": lifecycle.identity.to_dict(),
        "candidates": {
            label: candidate.to_dict()
            for label, candidate in lifecycle.candidates.items()
        },
        "deferredCandidates": {
            label: candidate.to_dict()
            for label, candidate in lifecycle.deferred_candidates.items()
        },
        "checkedCandidates": {
            fingerprint: candidate.to_dict()
            for fingerprint, candidate in lifecycle.checked_candidates.items()
        },
        "commitFence": (
            lifecycle.commit_fence.to_dict()
            if lifecycle.commit_fence is not None
            else None
        ),
    }
    return {
        **payload,
        "lifecycleFingerprint": _fingerprint(payload),
    }


def restore_semantic_lifecycle(
    value: Any,
    current_identity: SemanticDecisionIdentity,
) -> SemanticLifecycle | None:
    required = {
        "version",
        "identity",
        "candidates",
        "deferredCandidates",
        "checkedCandidates",
        "commitFence",
        "lifecycleFingerprint",
    }
    v3_required = required - {"checkedCandidates"}
    legacy_required = v3_required - {"deferredCandidates"}
    if not isinstance(value, Mapping):
        return None
    version = value.get("version")
    if version == _SEMANTIC_LIFECYCLE_VERSION:
        if set(value) != required:
            return None
    elif version == 3:
        if set(value) != v3_required:
            return None
    elif version == _LEGACY_SEMANTIC_LIFECYCLE_VERSION:
        if set(value) != legacy_required:
            return None
    else:
        return None
    if not _nonempty_string(value.get("lifecycleFingerprint")):
        return None
    identity = SemanticDecisionIdentity.from_dict(value.get("identity"))
    if identity is None or semantic_identity_is_stale(identity, current_identity):
        return None
    raw_candidates = value.get("candidates")
    if not isinstance(raw_candidates, Mapping):
        return None
    candidates: dict[str, BoundCandidate] = {}
    for label, raw_candidate in raw_candidates.items():
        candidate = BoundCandidate.from_dict(raw_candidate)
        if candidate is None or candidate.label != label:
            return None
        if not validate_candidate_canonical_steps_fingerprint(candidate):
            return None
        candidates[label] = candidate
    deferred_candidates: dict[str, DeferredAuthorityCandidate] = {}
    if version in (3, _SEMANTIC_LIFECYCLE_VERSION):
        raw_deferred = value.get("deferredCandidates")
        if not isinstance(raw_deferred, Mapping):
            return None
        for label, raw_candidate in raw_deferred.items():
            candidate = DeferredAuthorityCandidate.from_dict(raw_candidate)
            if candidate is None or candidate.source_label != label:
                return None
            if not validate_deferred_candidate_canonical_steps_fingerprint(candidate):
                return None
            deferred_candidates[label] = candidate
    checked_candidates: dict[str, BoundCandidate] = {}
    if version == _SEMANTIC_LIFECYCLE_VERSION:
        raw_checked = value.get("checkedCandidates")
        if not isinstance(raw_checked, Mapping):
            return None
        for fingerprint, raw_candidate in raw_checked.items():
            candidate = BoundCandidate.from_dict(raw_candidate)
            if candidate is None:
                return None
            checked_candidates[fingerprint] = candidate
    raw_fence = value.get("commitFence")
    fence = None if raw_fence is None else CommitFence.from_dict(raw_fence)
    if raw_fence is not None and fence is None:
        return None
    try:
        lifecycle = SemanticLifecycle(
            identity=identity,
            candidates=candidates,
            deferred_candidates=deferred_candidates,
            commit_fence=fence,
            checked_candidates=checked_candidates,
        )
    except (TypeError, ValueError):
        return None
    if version in (_LEGACY_SEMANTIC_LIFECYCLE_VERSION, 3):
        legacy_payload = {
            key: value[key]
            for key in value if key != "lifecycleFingerprint"
        }
        expected_fingerprint = _fingerprint(legacy_payload)
    else:
        expected_fingerprint = serialize_semantic_lifecycle(lifecycle)[
            "lifecycleFingerprint"
        ]
    if value.get("lifecycleFingerprint") != expected_fingerprint:
        return None
    return lifecycle


__all__ = [
    "BoundCandidate",
    "CommitFence",
    "DeferredAuthorityCandidate",
    "SemanticDecisionIdentity",
    "SemanticLifecycle",
    "all_checked_candidates",
    "match_bound_candidate",
    "match_deferred_candidate",
    "promote_deferred_candidate",
    "retain_checked_candidates",
    "restore_semantic_lifecycle",
    "semantic_chain_fingerprint",
    "semantic_delivery_fingerprint",
    "semantic_identity_from_ctx",
    "semantic_identity_is_stale",
    "serialize_semantic_lifecycle",
    "validate_candidate_canonical_steps_fingerprint",
    "validate_deferred_candidate_canonical_steps_fingerprint",
]
