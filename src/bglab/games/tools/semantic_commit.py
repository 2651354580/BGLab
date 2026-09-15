from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import inspect
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from bglab.games.tools.semantic_check import (
    CheckHandler,
    CheckOutcome,
    CheckPorts,
    CheckRequest,
)
from bglab.games.tools.semantic_lifecycle import (
    BoundCandidate,
    CommitFence,
    SemanticDecisionIdentity,
    SemanticLifecycle,
    all_checked_candidates,
    match_bound_candidate,
    semantic_delivery_fingerprint,
    semantic_identity_is_stale,
    semantic_route_id,
    semantic_route_number,
    validate_candidate_canonical_steps_fingerprint,
)
from bglab.games.semantic_validation.closest import commit_equivalent
from bglab.games.semantic_validation.model import SemanticAction, SemanticChain


_CANDIDATE_LABEL_RE = re.compile(r"C[1-9]\Z")
_ATTEMPT_CLOSED_CODES = frozenset(
    {
        "STALE_ATTEMPT_REJECTED",
        "ATTEMPT_CLOSED",
        "ATTEMPT_EXPIRED",
        "ATTEMPT_DEADLINE_EXCEEDED",
    }
)
_CONTROL_EXCEPTIONS = (
    asyncio.CancelledError,
    concurrent.futures.CancelledError,
    SystemExit,
    KeyboardInterrupt,
    GeneratorExit,
)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
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


def _semantic_chain_from_mapping(value: Any) -> SemanticChain | None:
    if not isinstance(value, Mapping):
        return None
    raw_actions = value.get("actions")
    if not isinstance(raw_actions, (list, tuple)) or not raw_actions:
        return None
    actions: list[SemanticAction] = []
    for raw_action in raw_actions:
        if not isinstance(raw_action, Mapping):
            return None
        action = raw_action.get("action")
        arguments = raw_action.get("args")
        if not isinstance(action, str) or not isinstance(arguments, Mapping):
            return None
        actions.append(SemanticAction.from_mapping(action, arguments))
    name = value.get("name", "")
    return SemanticChain(
        name=name if isinstance(name, str) else "",
        actions=tuple(actions),
    )


@dataclass(frozen=True, slots=True)
class CommitRequest:
    route_id: int | str | None = None
    chains: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def from_arguments(cls, arguments: Mapping[str, Any]) -> CommitRequest:
        if not isinstance(arguments, Mapping):
            raise TypeError("commit arguments must be an object")
        if arguments.get("operation") != "commit":
            raise ValueError("commit request operation must be commit")
        if set(arguments) - {"operation", "id", "chains"}:
            raise ValueError("commit request accepts only operation, id, and chains")
        has_id = "id" in arguments
        has_chains = "chains" in arguments
        if has_id == has_chains:
            raise ValueError("commit request requires exactly one of id or chains")
        if has_id:
            route_id = arguments["id"]
            numeric = isinstance(route_id, int) and not isinstance(route_id, bool) and route_id >= 1
            legacy = isinstance(route_id, str) and re.fullmatch(r"r[0-9a-f]{24}", route_id) is not None
            if not numeric and not legacy:
                raise ValueError("commit id must be a positive integer returned by Check")
            return cls(route_id=route_id)
        chains = arguments["chains"]
        if not isinstance(chains, (list, tuple)):
            raise ValueError("commit chains must be an array")
        if len(chains) != 1:
            raise ValueError("commit requires exactly one action chain")
        return cls(chains=tuple(_freeze_json(chain) for chain in chains))


class CommitResolutionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CommitProvenance:
    source: str
    candidate_label: str
    program_id: str
    binding_fingerprint: str
    semantic_chain: Mapping[str, Any]
    compilation_status: str | None = None

    def __post_init__(self) -> None:
        if self.source not in {"id", "chains"}:
            raise ValueError("commit provenance source must be id or chains")
        if _CANDIDATE_LABEL_RE.fullmatch(self.candidate_label) is None:
            raise ValueError("commit provenance candidate label must be C1-C9")
        if not isinstance(self.program_id, str) or not self.program_id:
            raise ValueError("commit provenance program_id must be non-empty")
        if (
            not isinstance(self.binding_fingerprint, str)
            or not self.binding_fingerprint
        ):
            raise ValueError("commit provenance binding fingerprint must be non-empty")
        if not isinstance(self.semantic_chain, Mapping) or not self.semantic_chain:
            raise ValueError("commit provenance semantic chain must be non-empty")
        object.__setattr__(self, "semantic_chain", _freeze_json(self.semantic_chain))


@dataclass(frozen=True, slots=True)
class BoundTransaction:
    identity: SemanticDecisionIdentity
    bound_candidate: BoundCandidate
    canonical_engine_steps: tuple[Mapping[str, Any], ...]
    canonical_steps_fingerprint: str
    delivery_fingerprint: str
    provenance: CommitProvenance

    def __post_init__(self) -> None:
        if not isinstance(self.identity, SemanticDecisionIdentity):
            raise TypeError("transaction identity must be SemanticDecisionIdentity")
        if not isinstance(self.bound_candidate, BoundCandidate):
            raise TypeError("transaction candidate must be BoundCandidate")
        if not validate_candidate_canonical_steps_fingerprint(self.bound_candidate):
            raise ValueError("candidate canonical steps fingerprint does not match")
        if not isinstance(self.provenance, CommitProvenance):
            raise TypeError("transaction provenance must be CommitProvenance")
        expected_steps = tuple(
            _freeze_json(step) for step in self.bound_candidate.engine_steps
        )
        supplied_steps = tuple(
            _freeze_json(step) for step in self.canonical_engine_steps
        )
        if supplied_steps != expected_steps:
            raise ValueError("transaction canonical steps do not match binding")
        if (
            self.canonical_steps_fingerprint
            != self.bound_candidate.canonical_steps_fingerprint
        ):
            raise ValueError("transaction canonical steps fingerprint does not match")
        expected_delivery = semantic_delivery_fingerprint(
            self.identity,
            self.bound_candidate,
        )
        if self.delivery_fingerprint != expected_delivery:
            raise ValueError("transaction delivery fingerprint does not match")
        if (
            self.provenance.candidate_label != self.bound_candidate.label
            or self.provenance.program_id != self.bound_candidate.program_id
            or self.provenance.binding_fingerprint
            != self.bound_candidate.binding_fingerprint
        ):
            raise ValueError("transaction provenance does not match binding")
        object.__setattr__(self, "canonical_engine_steps", supplied_steps)

    @classmethod
    def from_candidate(
        cls,
        identity: SemanticDecisionIdentity,
        candidate: BoundCandidate,
        *,
        source: str,
        compilation_status: str | None = None,
    ) -> BoundTransaction:
        if not candidate.commit_ready:
            raise ValueError("candidate is not ready for commit")
        return cls(
            identity=identity,
            bound_candidate=candidate,
            canonical_engine_steps=candidate.engine_steps,
            canonical_steps_fingerprint=candidate.canonical_steps_fingerprint,
            delivery_fingerprint=semantic_delivery_fingerprint(identity, candidate),
            provenance=CommitProvenance(
                source=source,
                candidate_label=candidate.label,
                program_id=candidate.program_id,
                binding_fingerprint=candidate.binding_fingerprint,
                semantic_chain=candidate.semantic_chain,
                compilation_status=compilation_status,
            ),
        )

    def to_validator_payload(self) -> dict[str, Any]:
        return {"steps": _thaw_json(self.canonical_engine_steps)}


class CommitHandler:
    def __init__(
        self,
        check_handler: CheckHandler | None = None,
        check_ports: CheckPorts | None = None,
        action_roles: Mapping[str, str] | None = None,
    ) -> None:
        self._check_handler = check_handler
        self._check_ports = check_ports
        self._action_roles = dict(action_roles or {})

    def resolve(
        self,
        request: CommitRequest,
        identity: SemanticDecisionIdentity,
        lifecycle: SemanticLifecycle | None,
    ) -> BoundTransaction:
        if not isinstance(request, CommitRequest):
            raise TypeError("request must be CommitRequest")
        if not isinstance(identity, SemanticDecisionIdentity):
            raise TypeError("identity must be SemanticDecisionIdentity")
        if request.route_id is not None:
            return self._resolve_id(request, identity, lifecycle)
        return self._resolve_chains(request, identity, lifecycle)

    def _resolve_id(
        self,
        request: CommitRequest,
        identity: SemanticDecisionIdentity,
        lifecycle: SemanticLifecycle | None,
    ) -> BoundTransaction:
        route_id = request.route_id
        if not (
            isinstance(route_id, int) and not isinstance(route_id, bool) and route_id >= 1
            or isinstance(route_id, str) and re.fullmatch(r"r[0-9a-f]{24}", route_id)
        ):
            raise CommitResolutionError("UNKNOWN_ROUTE_ID", "invalid Check route number")
        if lifecycle is None or semantic_identity_is_stale(lifecycle.identity, identity):
            raise CommitResolutionError(
                "CHECK_REQUIRED",
                "commit id is valid only for a Check in the current decision",
            )
        matches = [candidate for candidate in all_checked_candidates(lifecycle)
                   if (semantic_route_id(identity, candidate) if isinstance(request.route_id, str)
                       else semantic_route_number(lifecycle, candidate)) == request.route_id]
        if len(matches) != 1:
            raise CommitResolutionError(
                "UNKNOWN_ROUTE_ID",
                "commit id is not a ready route checked in the current decision",
            )
        candidate = matches[0]
        try:
            return BoundTransaction.from_candidate(
                identity,
                candidate,
                source="id",
            )
        except (TypeError, ValueError) as exc:
            raise CommitResolutionError(
                "INVALID_SEMANTIC_BINDING",
                str(exc),
            ) from exc

    def _resolve_chains(
        self,
        request: CommitRequest,
        identity: SemanticDecisionIdentity,
        lifecycle: SemanticLifecycle | None,
    ) -> BoundTransaction:
        if self._check_handler is None or self._check_ports is None:
            raise CommitResolutionError(
                "SEMANTIC_CHECK_UNAVAILABLE",
                "direct chain commit is not connected to check ports",
            )
        try:
            check_request = CheckRequest.from_arguments(
                {
                    "operation": "check",
                    "chains": _thaw_json(request.chains),
                },
                decision_id=identity.decision_id,
                state_hash=identity.state_hash,
            )
            normalized = self._check_handler.normalize(check_request)
        except (RuntimeError, TypeError, ValueError) as exc:
            raise CommitResolutionError(
                "SEMANTIC_VALIDATION_FAILED",
                str(exc),
            ) from exc
        if not isinstance(normalized, CheckOutcome):
            raise CommitResolutionError(
                "SEMANTIC_VALIDATION_FAILED",
                "check handler returned an invalid normalization outcome",
            )
        if not normalized.ok or normalized.normalized_payload is None:
            raise CommitResolutionError(
                normalized.error_code or "SEMANTIC_VALIDATION_FAILED",
                normalized.error_message or "direct chain normalization failed",
            )
        payload = normalized.normalized_payload
        normalized_chain = payload.to_dict()["chains"][0]
        current_lifecycle = (
            lifecycle
            if lifecycle is not None
            and not semantic_identity_is_stale(lifecycle.identity, identity)
            else None
        )
        ready = match_bound_candidate(current_lifecycle, normalized_chain)
        if ready is not None:
            try:
                return BoundTransaction.from_candidate(
                    identity,
                    ready,
                    source="chains",
                )
            except (TypeError, ValueError) as exc:
                raise CommitResolutionError(
                    "INVALID_SEMANTIC_BINDING",
                    str(exc),
                ) from exc
        try:
            outcome = self._check_handler.handle(check_request, self._check_ports)
        except _CONTROL_EXCEPTIONS:
            raise
        except Exception as exc:
            raise CommitResolutionError(
                "SEMANTIC_VALIDATION_FAILED",
                str(exc),
            ) from exc
        if not isinstance(outcome, CheckOutcome):
            raise CommitResolutionError(
                "SEMANTIC_VALIDATION_FAILED",
                "direct chain validation returned an invalid outcome",
            )
        if not outcome.ok:
            raise CommitResolutionError(
                outcome.error_code or "SEMANTIC_VALIDATION_FAILED",
                outcome.error_message or "direct chain validation failed",
            )
        if outcome.compilation_status != "complete":
            raise CommitResolutionError(
                "CHECK_REQUIRED",
                "direct Commit accepts only an already complete legal chain; use Check first",
            )

        submitted_chain = payload.chains[0]
        direct_matches: list[BoundCandidate] = []
        for candidate in outcome.candidates:
            candidate_chain = _semantic_chain_from_mapping(candidate.semantic_chain)
            if (
                candidate.commit_ready is not True
                or candidate.intent_exact is not True
                or candidate_chain is None
                or not commit_equivalent(
                    submitted_chain,
                    candidate_chain,
                    self._action_roles,
                )
            ):
                continue
            bound = BoundCandidate.from_dict({
                "label": candidate.label,
                "programId": candidate.program_id,
                "engineSteps": candidate.engine_steps,
                "canonicalStepsFingerprint": candidate.canonical_steps_fingerprint,
                "bindingFingerprint": candidate.binding_fingerprint,
                "intentExact": candidate.intent_exact,
                "commitReady": candidate.commit_ready,
                "semanticChain": candidate.semantic_chain,
            })
            if bound is not None:
                direct_matches.append(bound)
        if len(direct_matches) != 1:
            raise CommitResolutionError(
                "CHECK_REQUIRED",
                "direct Commit route needs correction, completion, or disambiguation; use Check first",
            )
        try:
            return BoundTransaction.from_candidate(
                identity,
                direct_matches[0],
                source="chains",
                compilation_status=outcome.compilation_status,
            )
        except (TypeError, ValueError) as exc:
            raise CommitResolutionError(
                "INVALID_SEMANTIC_BINDING",
                str(exc),
            ) from exc


@dataclass(frozen=True, slots=True)
class CommitPorts:
    validator: Callable[[dict[str, Any]], Any] | None
    sink: Callable[[Any], Any] | None
    load_lifecycle: Callable[[], SemanticLifecycle | None] | None
    persist_lifecycle: Callable[[SemanticLifecycle], None] | None
    attempt_is_open: Callable[[], bool] | None
    mark_submitted: Callable[[Mapping[str, Any]], None] | None


@dataclass(frozen=True, slots=True)
class CommitOutcome:
    status: str
    delivery_fingerprint: str
    fence_status: str | None = None
    public_result: Mapping[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    idempotent: bool = False

    def __post_init__(self) -> None:
        if self.public_result is not None:
            object.__setattr__(
                self,
                "public_result",
                _freeze_json(self.public_result),
            )

    @property
    def ok(self) -> bool:
        return self.error_code is None and self.status in {
            "committed",
            "already_committed",
        }


class CommitCoordinator:
    def commit(
        self,
        identity: SemanticDecisionIdentity,
        transaction: BoundTransaction,
        ports: CommitPorts,
    ) -> CommitOutcome | Awaitable[CommitOutcome]:
        if not isinstance(identity, SemanticDecisionIdentity):
            raise TypeError("identity must be SemanticDecisionIdentity")
        if not isinstance(transaction, BoundTransaction):
            raise TypeError("transaction must be BoundTransaction")
        if not isinstance(ports, CommitPorts):
            raise TypeError("ports must be CommitPorts")
        if transaction.identity != identity:
            return self._error(
                transaction,
                "STALE_SEMANTIC_BINDING",
                "transaction identity does not match the current decision",
            )
        try:
            current = ports.load_lifecycle() if callable(ports.load_lifecycle) else None
        except _CONTROL_EXCEPTIONS:
            raise
        except Exception as exc:
            return self._error(
                transaction,
                "LIFECYCLE_LOAD_FAILED",
                str(exc),
            )
        if current is not None and semantic_identity_is_stale(
            current.identity,
            identity,
        ):
            return self._error(
                transaction,
                "STALE_SEMANTIC_BINDING",
                "persisted lifecycle belongs to a stale decision",
            )
        if current is not None and current.commit_fence is not None:
            existing = current.commit_fence
            if existing.delivery_fingerprint != transaction.delivery_fingerprint:
                return self._error(
                    transaction,
                    "SECOND_COMMIT_REJECTED",
                    "this decision already has a different delivery",
                    fence_status=existing.status,
                )
            if existing.status == "sink_confirmed":
                return CommitOutcome(
                    status="already_committed",
                    delivery_fingerprint=transaction.delivery_fingerprint,
                    fence_status=existing.status,
                    idempotent=True,
                )
            code = (
                "COMMIT_CONFIRMATION_REQUIRED"
                if existing.status in {"authority_committed", "sink_failed"}
                else "COMMIT_OUTCOME_INDETERMINATE"
            )
            return self._error(
                transaction,
                code,
                "an unresolved delivery fence requires host reconciliation",
                fence_status=existing.status,
            )
        if not self._attempt_is_open(ports):
            return self._error(
                transaction,
                "ATTEMPT_CLOSED",
                "the current commit attempt is closed",
            )
        if not callable(ports.validator):
            return self._error(
                transaction,
                "VALIDATOR_UNAVAILABLE",
                "the authority validator is unavailable",
            )
        if not callable(ports.sink):
            return self._error(
                transaction,
                "SINK_UNAVAILABLE",
                "the action sink is unavailable",
            )
        if not callable(ports.persist_lifecycle):
            return self._error(
                transaction,
                "PERSISTENCE_UNAVAILABLE",
                "semantic lifecycle persistence is unavailable",
            )
        if not callable(ports.mark_submitted):
            return self._error(
                transaction,
                "SUBMISSION_MARKER_UNAVAILABLE",
                "the submitted marker callback is unavailable",
            )

        lifecycle = self._bind_transaction(current, transaction)
        prepared = self._with_fence(lifecycle, transaction, "prepared")
        try:
            ports.persist_lifecycle(prepared)
        except _CONTROL_EXCEPTIONS:
            raise
        except Exception as exc:
            return self._error(
                transaction,
                "PERSISTENCE_FAILED",
                str(exc),
            )
        if not self._attempt_is_open(ports):
            indeterminate = self._with_fence(
                prepared,
                transaction,
                "indeterminate",
            )
            self._persist_best_effort(ports, indeterminate)
            return self._error(
                transaction,
                "COMMIT_OUTCOME_INDETERMINATE",
                "attempt closed before authority dispatch",
                fence_status="indeterminate",
            )
        try:
            result = ports.validator(transaction.to_validator_payload())
        except _CONTROL_EXCEPTIONS:
            indeterminate = self._with_fence(
                prepared,
                transaction,
                "indeterminate",
            )
            self._persist_best_effort(ports, indeterminate)
            raise
        except Exception as exc:
            return self._validator_exception(
                transaction,
                prepared,
                ports,
                exc,
            )
        if inspect.isawaitable(result):
            return self._await_validator(
                result,
                transaction,
                prepared,
                ports,
            )
        return self._finish_validator(
            result,
            transaction,
            prepared,
            ports,
        )

    async def _await_validator(
        self,
        pending: Awaitable[Any],
        transaction: BoundTransaction,
        prepared: SemanticLifecycle,
        ports: CommitPorts,
    ) -> CommitOutcome:
        try:
            result = await pending
        except _CONTROL_EXCEPTIONS:
            indeterminate = self._with_fence(
                prepared,
                transaction,
                "indeterminate",
            )
            self._persist_best_effort(ports, indeterminate)
            raise
        except Exception as exc:
            return self._validator_exception(
                transaction,
                prepared,
                ports,
                exc,
            )
        return self._finish_validator(
            result,
            transaction,
            prepared,
            ports,
        )

    def _finish_validator(
        self,
        result: Any,
        transaction: BoundTransaction,
        prepared: SemanticLifecycle,
        ports: CommitPorts,
    ) -> CommitOutcome:
        try:
            public = self._public_result(result)
        except _CONTROL_EXCEPTIONS:
            indeterminate = self._with_fence(
                prepared,
                transaction,
                "indeterminate",
            )
            self._persist_best_effort(ports, indeterminate)
            raise
        if public is None:
            return self._validator_exception(
                transaction,
                prepared,
                ports,
                TypeError("authority validator returned a non-object result"),
            )
        if not self._attempt_is_open(ports):
            indeterminate = self._with_fence(
                prepared,
                transaction,
                "indeterminate",
            )
            self._persist_best_effort(ports, indeterminate)
            return self._error(
                transaction,
                "COMMIT_OUTCOME_INDETERMINATE",
                "attempt closed after authority dispatch",
                fence_status="indeterminate",
                public_result=public,
            )
        raw_error = public.get("error")
        authority_code = (
            raw_error.get("code")
            if isinstance(raw_error, Mapping)
            else public.get("code")
        )
        if authority_code in _ATTEMPT_CLOSED_CODES:
            indeterminate = self._with_fence(
                prepared,
                transaction,
                "indeterminate",
            )
            self._persist_best_effort(ports, indeterminate)
            return self._error(
                transaction,
                "COMMIT_OUTCOME_INDETERMINATE",
                "authority reported that the commit attempt closed",
                fence_status="indeterminate",
                public_result=public,
            )
        if public.get("status") != "committed":
            if public.get("stateChanged") is not False:
                indeterminate = self._with_fence(
                    prepared,
                    transaction,
                    "indeterminate",
                )
                self._persist_best_effort(ports, indeterminate)
                return self._error(
                    transaction,
                    "COMMIT_OUTCOME_INDETERMINATE",
                    "validator rejection did not prove zero authority change",
                    fence_status="indeterminate",
                    public_result=public,
                )
            cleared = SemanticLifecycle(
                identity=prepared.identity,
                candidates=prepared.candidates,
                deferred_candidates=prepared.deferred_candidates,
                checked_candidates=prepared.checked_candidates,
                route_numbers=prepared.route_numbers,
            )
            self._persist_best_effort(ports, cleared)
            return self._error(
                transaction,
                "SEMANTIC_COMMIT_REJECTED",
                "authority validator rejected the transaction",
                public_result=public,
            )

        authority_committed = self._with_fence(
            prepared,
            transaction,
            "authority_committed",
        )
        if not self._persist(ports, authority_committed):
            return self._error(
                transaction,
                "COMMIT_OUTCOME_INDETERMINATE",
                "authority committed but its fence could not be persisted",
                fence_status="indeterminate",
                public_result=public,
            )
        try:
            ports.sink(result)
        except _CONTROL_EXCEPTIONS:
            indeterminate = self._with_fence(
                authority_committed,
                transaction,
                "indeterminate",
            )
            self._persist_best_effort(ports, indeterminate)
            raise
        except Exception as exc:
            sink_failed = self._with_fence(
                authority_committed,
                transaction,
                "sink_failed",
            )
            self._persist_best_effort(ports, sink_failed)
            return self._error(
                transaction,
                "COMMIT_CONFIRMATION_REQUIRED",
                str(exc),
                fence_status="sink_failed",
                public_result=public,
            )
        if not self._attempt_is_open(ports):
            indeterminate = self._with_fence(
                authority_committed,
                transaction,
                "indeterminate",
            )
            self._persist_best_effort(ports, indeterminate)
            return self._error(
                transaction,
                "COMMIT_OUTCOME_INDETERMINATE",
                "attempt closed after sink dispatch",
                fence_status="indeterminate",
                public_result=public,
            )
        sink_confirmed = self._with_fence(
            authority_committed,
            transaction,
            "sink_confirmed",
        )
        if not self._persist(ports, sink_confirmed):
            return self._error(
                transaction,
                "COMMIT_OUTCOME_INDETERMINATE",
                "sink confirmed but its fence could not be persisted",
                fence_status="indeterminate",
                public_result=public,
            )
        try:
            ports.mark_submitted(public)
        except _CONTROL_EXCEPTIONS:
            raise
        except Exception as exc:
            return self._error(
                transaction,
                "SUBMISSION_MARKER_FAILED",
                str(exc),
                fence_status="sink_confirmed",
                public_result=public,
            )
        return CommitOutcome(
            status="committed",
            delivery_fingerprint=transaction.delivery_fingerprint,
            fence_status="sink_confirmed",
            public_result=public,
        )

    @staticmethod
    def _attempt_is_open(ports: CommitPorts) -> bool:
        if not callable(ports.attempt_is_open):
            return True
        try:
            return bool(ports.attempt_is_open())
        except _CONTROL_EXCEPTIONS:
            raise
        except Exception:
            return False

    @staticmethod
    def _public_result(result: Any) -> dict[str, Any] | None:
        if hasattr(result, "to_public_dict"):
            value = result.to_public_dict()
        elif isinstance(result, Mapping):
            value = dict(result)
        else:
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _bind_transaction(
        current: SemanticLifecycle | None,
        transaction: BoundTransaction,
    ) -> SemanticLifecycle:
        candidates: Mapping[str, BoundCandidate]
        if (
            current is not None
            and current.candidates.get(transaction.bound_candidate.label)
            == transaction.bound_candidate
        ):
            candidates = current.candidates
        else:
            candidates = {
                transaction.bound_candidate.label: transaction.bound_candidate
            }
        return SemanticLifecycle(
            identity=transaction.identity,
            candidates=candidates,
            deferred_candidates=(
                current.deferred_candidates
                if current is not None
                else {}
            ),
            checked_candidates={
                semantic_delivery_fingerprint(transaction.identity, candidate): replace(candidate, label="C1")
                for candidate in all_checked_candidates(current)
            } if current is not None else {},
            route_numbers=current.route_numbers if current is not None else {},
        )

    @staticmethod
    def _with_fence(
        lifecycle: SemanticLifecycle,
        transaction: BoundTransaction,
        status: str,
    ) -> SemanticLifecycle:
        return SemanticLifecycle(
            identity=lifecycle.identity,
            candidates=lifecycle.candidates,
            deferred_candidates=lifecycle.deferred_candidates,
            checked_candidates=lifecycle.checked_candidates,
            route_numbers=lifecycle.route_numbers,
            commit_fence=CommitFence(
                candidate_label=transaction.bound_candidate.label,
                delivery_fingerprint=transaction.delivery_fingerprint,
                status=status,
            ),
        )

    @staticmethod
    def _persist(ports: CommitPorts, lifecycle: SemanticLifecycle) -> bool:
        try:
            ports.persist_lifecycle(lifecycle)  # type: ignore[misc]
        except _CONTROL_EXCEPTIONS:
            raise
        except Exception:
            return False
        return True

    @classmethod
    def _persist_best_effort(
        cls,
        ports: CommitPorts,
        lifecycle: SemanticLifecycle,
    ) -> None:
        cls._persist(ports, lifecycle)

    def _validator_exception(
        self,
        transaction: BoundTransaction,
        prepared: SemanticLifecycle,
        ports: CommitPorts,
        exc: Exception,
    ) -> CommitOutcome:
        indeterminate = self._with_fence(
            prepared,
            transaction,
            "indeterminate",
        )
        self._persist_best_effort(ports, indeterminate)
        return self._error(
            transaction,
            "COMMIT_OUTCOME_INDETERMINATE",
            str(exc),
            fence_status="indeterminate",
        )

    @staticmethod
    def _error(
        transaction: BoundTransaction,
        code: str,
        message: str,
        *,
        fence_status: str | None = None,
        public_result: Mapping[str, Any] | None = None,
    ) -> CommitOutcome:
        unresolved = code in {
            "COMMIT_OUTCOME_INDETERMINATE",
            "COMMIT_CONFIRMATION_REQUIRED",
        }
        return CommitOutcome(
            status="indeterminate" if unresolved else "rejected",
            delivery_fingerprint=transaction.delivery_fingerprint,
            fence_status=fence_status,
            public_result=public_result,
            error_code=code,
            error_message=message,
        )


__all__ = [
    "BoundTransaction",
    "CommitCoordinator",
    "CommitHandler",
    "CommitOutcome",
    "CommitPorts",
    "CommitProvenance",
    "CommitRequest",
    "CommitResolutionError",
]
