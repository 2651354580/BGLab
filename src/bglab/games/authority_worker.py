"""Read-only, DecisionFrame-scoped authority clone worker."""

from __future__ import annotations

import copy
from typing import Any

from bglab.games.adapter_process import AdapterProcess
from bglab.games.registry import GameDefinition
from bglab.games.replay import authority_hash
from bglab.games.semantic_validation.fingerprint import canonical_steps_fingerprint


__all__ = [
    "AuthorityWorker",
    "canonical_steps_fingerprint",
    "snapshot_fingerprint",
]


def snapshot_fingerprint(snapshot: dict) -> str:
    return authority_hash(snapshot)


class AuthorityWorker:
    """A restored authority clone exposing read-only validation operations."""

    def __init__(
        self,
        definition: GameDefinition,
        snapshot: dict,
        *,
        decision_id: str | None = None,
        node_heap_mb: int = 256,
        request_timeout_s: float = 120.0,
    ) -> None:
        self._snapshot = copy.deepcopy(snapshot)
        self._snapshot_fingerprint = authority_hash(self._snapshot)
        self._definition = definition
        self.supports_complete_prefix_subgraph = (
            getattr(definition.capabilities, "complete_prefix_subgraph", False) is True
        )
        self._request_timeout_s = request_timeout_s
        self._node_heap_mb = node_heap_mb
        self._decision_id = str(
            decision_id
            if decision_id is not None
            else self._snapshot.get("decisionId", "")
        )
        self._adapter = self._start_restored_adapter()

    def _start_restored_adapter(self) -> AdapterProcess:
        try:
            adapter = AdapterProcess(
                self._definition,
                node_heap_mb=self._node_heap_mb,
                request_timeout_s=self._request_timeout_s,
            )
        except Exception:
            raise RuntimeError("AUTHORITY_WORKER_START_FAILED") from None
        try:
            adapter.restore(self._snapshot)
        except BaseException as exc:
            try:
                adapter.close()
            except BaseException:
                pass
            if isinstance(exc, Exception):
                raise RuntimeError("AUTHORITY_WORKER_RESTORE_FAILED") from None
            raise
        return adapter

    def _restart_restored_adapter(self) -> None:
        try:
            self._adapter.close()
        except BaseException:
            pass
        self._adapter = self._start_restored_adapter()

    def model_state(self, seat: int) -> dict:
        """Return confirmed authority plus a host-derived public seat view."""
        if isinstance(seat, bool) or not isinstance(seat, int) or seat < 0:
            raise RuntimeError("invalid authority seat")
        wrappers = (self._snapshot.get("wrapper"), self._snapshot.get("st"))
        current_players = [
            container.get("currentPlayer")
            for container in wrappers
            if isinstance(container, dict) and "currentPlayer" in container
        ]
        if not current_players or any(
            isinstance(current, bool) or current != seat
            for current in current_players
        ):
            raise RuntimeError("stale authority seat")
        if not self._decision_id:
            raise RuntimeError("confirmed authority decision is missing")
        snapshot_decision_id = self._snapshot.get("decisionId")
        if (
            snapshot_decision_id is not None
            and snapshot_decision_id != self._decision_id
        ):
            raise RuntimeError("confirmed authority decision mismatch")
        view = self._read_only(self._adapter.view, seat)
        if not isinstance(view, dict):
            raise RuntimeError("authority adapter view is malformed")
        if isinstance(view.get("seat"), bool) or view.get("seat") != seat:
            raise RuntimeError("authority adapter view seat mismatch")
        if view.get("decisionId") != self._decision_id:
            raise RuntimeError("authority adapter view decision mismatch")
        model_state = copy.deepcopy(self._snapshot)
        model_state["adapterView"] = copy.deepcopy(view)
        return model_state

    def _validate_identity(self, request: dict | None = None) -> None:
        if request and request.get("decisionId") not in {None, self._decision_id}:
            raise RuntimeError("stale authority decision")
        if request and request.get("snapshotFingerprint") not in {
            None,
            self._snapshot_fingerprint,
        }:
            raise RuntimeError("stale authority snapshot")
        frame = request.get("decisionFrame") if isinstance(request, dict) else None
        if frame is not None:
            if (
                not isinstance(frame, dict)
                or frame.get("decisionId") != self._decision_id
            ):
                raise RuntimeError("stale authority decision")
            if frame.get("stateHash") != self._snapshot_fingerprint:
                raise RuntimeError("stale authority snapshot")

    def coverage_catalog(self) -> dict:
        return self._read_only(self._adapter.coverage_catalog)

    def strategic_opportunity_catalog(self) -> list[dict]:
        return self._read_only(self._adapter.strategic_opportunity_catalog)

    def outcome_index(self, seat: int, request: dict | None = None) -> dict:
        return self._read_only(
            self._adapter.outcome_index,
            seat,
            copy.deepcopy(request or {}),
        )

    def enumerate_routes(
        self,
        request: dict,
        *,
        game_id: str | None = None,
        seat: int | None = None,
    ) -> dict:
        """Enumerate read-only engine routes for authority matching."""
        request = copy.deepcopy(request)
        self._validate_identity(request)
        expected_frame = None
        if request.get("proposal") is not None:
            if not isinstance(game_id, str) or not game_id:
                raise RuntimeError("authority game id is required")
            if isinstance(seat, bool) or not isinstance(seat, int) or seat < 0:
                raise RuntimeError("authority seat is required")
            request["decisionId"] = self._decision_id
            request["snapshotFingerprint"] = self._snapshot_fingerprint
            expected_frame = {
                "gameId": game_id,
                "decisionId": self._decision_id,
                "seat": seat,
                "stateHash": self._snapshot_fingerprint,
            }
            request["decisionFrame"] = copy.deepcopy(expected_frame)
        if expected_frame is None:
            return self._read_only(self._adapter.enumerate_routes, request)
        return self._read_only(
            self._adapter.enumerate_routes,
            request,
            expected_decision_frame=expected_frame,
        )

    def validate_transaction(
        self,
        transaction: dict,
        request: dict | None = None,
    ) -> dict:
        """Validate one engine transaction without mutating the source snapshot."""
        request = copy.deepcopy(request or {})
        self._validate_identity(request)
        return self._read_only(
            self._adapter.validate_transaction,
            self._decision_id,
            copy.deepcopy(transaction),
        )

    def _read_only(self, operation: Any, *args: Any, **kwargs: Any) -> Any:
        def assert_source_unchanged() -> None:
            snapshot = getattr(self._adapter, "authority_snapshot", None)
            if not callable(snapshot):
                snapshot = getattr(self._adapter, "snapshot", None)
            if not callable(snapshot):
                return
            try:
                current = snapshot()
                current_fingerprint = authority_hash(current)
            except Exception:
                raise RuntimeError("AUTHORITY_WORKER_SNAPSHOT_FAILED") from None
            if current_fingerprint != self._snapshot_fingerprint:
                raise RuntimeError(
                    "AUTHORITY_WORKER_MUTATION: read-only operation changed "
                    "the restored snapshot"
                ) from None

        method_name = str(getattr(operation, "__name__", ""))
        for attempt in range(2):
            try:
                result = operation(*args, **kwargs)
                break
            except Exception:
                if attempt == 0 and method_name and getattr(
                    operation, "__self__", None,
                ) is self._adapter:
                    self._restart_restored_adapter()
                    operation = getattr(self._adapter, method_name)
                    continue
                raise RuntimeError("AUTHORITY_WORKER_OPERATION_FAILED") from None
        assert_source_unchanged()
        return result

    def close(self) -> None:
        self._adapter.close()
