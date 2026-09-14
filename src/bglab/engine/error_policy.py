"""Stable public failure policy shared by Provider, Tool, Agent, and TUI seams."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from bglab.permissions.types import ToolPermissionSpec


TOOL_HANDLER_FAILURE = "TOOL_HANDLER_FAILURE"
TOOL_OUTCOME_INDETERMINATE = "TOOL_OUTCOME_INDETERMINATE"
TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
PROVIDER_REQUEST_FAILED = "PROVIDER_REQUEST_FAILED"
AGENT_RUNTIME_FAILURE = "AGENT_RUNTIME_FAILURE"
RUNTIME_FAILURE = "RUNTIME_FAILURE"


@dataclass(frozen=True)
class PublicToolFailure:
    text: str
    metadata: Mapping[str, Any]


class DeferredTargetHandlerFailure(Exception):
    """Internal carrier preserving a deferred target's permission metadata."""

    def __init__(self, permission_spec: ToolPermissionSpec):
        super().__init__("deferred target handler failed")
        self.permission_spec = permission_spec


@dataclass(frozen=True)
class ToolErrorPolicy:
    """Classify an unexpected failure after a Tool handler has started."""

    def unexpected(self, spec: ToolPermissionSpec) -> PublicToolFailure:
        if spec.read_only:
            return PublicToolFailure(
                TOOL_HANDLER_FAILURE,
                {
                    "outcome_kind": "tool_handler_failure",
                    "public_code": TOOL_HANDLER_FAILURE,
                    "outcome_indeterminate": False,
                    "retryable": True,
                    "reconcile_required": False,
                },
            )
        return PublicToolFailure(
            TOOL_OUTCOME_INDETERMINATE,
            {
                "outcome_kind": "tool_outcome_indeterminate",
                "public_code": TOOL_OUTCOME_INDETERMINATE,
                "outcome_indeterminate": True,
                "retryable": False,
                "reconcile_required": True,
            },
        )


DEFAULT_TOOL_ERROR_POLICY = ToolErrorPolicy()


def stable_public_error(code: str) -> str:
    """Return a public code without incorporating untrusted exception text."""

    return str(code)


__all__ = [
    "AGENT_RUNTIME_FAILURE",
    "DEFAULT_TOOL_ERROR_POLICY",
    "DeferredTargetHandlerFailure",
    "PROVIDER_REQUEST_FAILED",
    "PublicToolFailure",
    "RUNTIME_FAILURE",
    "TOOL_HANDLER_FAILURE",
    "TOOL_OUTCOME_INDETERMINATE",
    "TOOL_UNAVAILABLE",
    "ToolErrorPolicy",
    "stable_public_error",
]
