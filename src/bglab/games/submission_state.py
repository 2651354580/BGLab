"""Submission projections for the existing durable Commit lifecycle.

Coordinator owns write ordering; the host owns snapshot/replay evidence. This
adapter owns the context fields changed when a submission is accepted,
confirmed, or reopened by that host evidence. It holds no second history.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, MutableMapping
from dataclasses import replace
from typing import Any

from bglab.games.tools.semantic_lifecycle import (
    SemanticDecisionIdentity,
    restore_semantic_lifecycle,
    serialize_semantic_lifecycle,
)


_LIFECYCLE_SLOTS = ("_semantic_lifecycle_v2", "_restored_semantic_v2_lifecycle")


class SubmissionState:
    def __init__(self, context: MutableMapping[str, Any]) -> None:
        self.context = context

    def _stored(self) -> tuple[str, dict] | None:
        for slot in _LIFECYCLE_SLOTS:
            value = self.context.get(slot)
            if isinstance(value, dict):
                return slot, value
        return None

    def lifecycle_payload(self) -> dict | None:
        stored = self._stored()
        return stored[1] if stored is not None else None

    def mark_submitted(self, public: Mapping[str, Any], fallback_transaction: Mapping[str, Any]) -> None:
        """Project only after Coordinator has confirmed the durable sink."""
        self.context.update({
            "_act_submitted": True,
            "_committed_transaction": copy.deepcopy(public.get("transaction", fallback_transaction)),
            "_canonical_action": copy.deepcopy(public.get("canonicalAction")),
            "_semantic_lifecycle_pending_confirmation": True,
        })

    def confirm_snapshot(self, state_hash: str) -> bool:
        """A changed durable authority snapshot expires the previous binding."""
        if not self.context.get("_semantic_lifecycle_pending_confirmation"):
            return False
        payload = self.lifecycle_payload()
        identity = payload.get("identity") if payload is not None else None
        if not isinstance(identity, dict) or state_hash == identity.get("stateHash"):
            return False
        self.context.update({slot: None for slot in _LIFECYCLE_SLOTS})
        self.context.update({
            "_semantic_lifecycle_pending_confirmation": False,
            "_semantic_commit_fence": None,
        })
        return True

    def reopen_after_rollback(self, decision_id: str) -> bool:
        """Host proved the same decision remains and no durable replay is pending.

        Validate the original envelope before removing its fence, then serialize
        again so the retained candidates and fingerprint remain restorable.
        A corrupt envelope is not evidence that a write may safely be retried.
        """
        stored = self._stored()
        if stored is None:
            return False
        slot, payload = stored
        raw_identity = payload.get("identity")
        if not isinstance(raw_identity, dict) or raw_identity.get("decisionId") != decision_id:
            return False
        identity = SemanticDecisionIdentity.from_dict(raw_identity)
        lifecycle = restore_semantic_lifecycle(payload, identity) if identity is not None else None
        if lifecycle is None:
            raise ValueError("cannot reconcile an invalid semantic lifecycle envelope")
        if lifecycle.commit_fence is None:
            return False
        reopened = serialize_semantic_lifecycle(replace(lifecycle, commit_fence=None))
        self.context[slot] = reopened
        for other in _LIFECYCLE_SLOTS:
            if other != slot:
                self.context[other] = None
        self.context.update({
            "_semantic_commit_fence": None,
            "_semantic_lifecycle_pending_confirmation": False,
            "_act_submitted": False,
            "_committed_transaction": None,
            "_canonical_action": None,
        })
        return True
