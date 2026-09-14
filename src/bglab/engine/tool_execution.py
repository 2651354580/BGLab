"""Profile-selected Tool execution adapters for the shared query loop."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from bglab.engine.error_policy import (
    DEFAULT_TOOL_ERROR_POLICY,
    DeferredTargetHandlerFailure,
    TOOL_UNAVAILABLE,
    ToolErrorPolicy,
)
from bglab.permissions.types import ToolPermissionSpec
from bglab.tools.base import ToolCallResult


logger = logging.getLogger("bglab.engine.tools")


@dataclass(frozen=True)
class ToolInvocationContext:
    name: str
    arguments: Mapping[str, Any]
    handlers: Mapping[str, Callable]
    messages: list[dict[str, Any]]
    system_prompt: str
    cwd: str
    permission_context: Any
    ask_callback: Any
    permission_mode: str
    model: str
    permission_spec: ToolPermissionSpec = field(
        default_factory=ToolPermissionSpec,
    )
    error_policy: ToolErrorPolicy = DEFAULT_TOOL_ERROR_POLICY


@dataclass(frozen=True)
class ToolExecutionOutcome:
    value: Any
    is_error: bool
    metadata: Mapping[str, Any] = field(default_factory=dict)


def _unexpected_failure(
    context: ToolInvocationContext,
    permission_spec: ToolPermissionSpec | None = None,
) -> ToolExecutionOutcome:
    failure = context.error_policy.unexpected(
        permission_spec or context.permission_spec,
    )
    result = ToolCallResult(
        failure.text,
        is_error=True,
        metadata=failure.metadata,
    )
    return ToolExecutionOutcome(
        value=result,
        is_error=True,
        metadata=dict(failure.metadata),
    )


def tool_unavailable_outcome() -> ToolExecutionOutcome:
    metadata = {
        "outcome_kind": "tool_unavailable",
        "public_code": TOOL_UNAVAILABLE,
        "outcome_indeterminate": False,
        "retryable": False,
        "reconcile_required": False,
    }
    result = ToolCallResult(
        TOOL_UNAVAILABLE,
        is_error=True,
        metadata=metadata,
    )
    return ToolExecutionOutcome(result, True, metadata)


async def execute_generic_tool(
    context: ToolInvocationContext,
) -> ToolExecutionOutcome:
    handler = context.handlers.get(context.name)
    if handler is None:
        return tool_unavailable_outcome()
    try:
        result = handler(dict(context.arguments))
        if asyncio.iscoroutine(result):
            result = await result
        return ToolExecutionOutcome(
            value=result,
            is_error=bool(getattr(result, "is_error", False)),
            metadata=dict(getattr(result, "metadata", {}) or {}),
        )
    except (asyncio.CancelledError, concurrent.futures.CancelledError):
        raise
    except DeferredTargetHandlerFailure as failure:
        logger.exception("Unexpected deferred Tool handler failure: %s", context.name)
        return _unexpected_failure(context, failure.permission_spec)
    except Exception:
        logger.exception("Unexpected Tool handler failure: %s", context.name)
        return _unexpected_failure(context)


def _agent_arguments(context: ToolInvocationContext) -> dict[str, Any]:
    enriched = {
        key: value
        for key, value in context.arguments.items()
        if not str(key).startswith("_")
    }
    enriched.update({
        "_parent_messages": context.messages,
        "_parent_system_prompt": context.system_prompt,
        "_cwd": context.cwd,
        "_permission_context": context.permission_context,
        "_ask_callback": context.ask_callback,
        "_permission_mode": context.permission_mode,
        "_parent_model": context.model,
        "_parent_event_loop": asyncio.get_running_loop(),
    })
    return enriched


def _sync_plan_mode_transition(context: ToolInvocationContext) -> None:
    """Apply a trusted Plan-mode tool transition to the typed authority.

    Plan tools currently own their transition through ``session_state``.  The
    execution adapter is the narrow trusted boundary that copies that explicit
    transition into the per-query ``PermissionContext``.  Ordinary global
    state changes never overwrite the typed context.
    """

    if context.name not in {"EnterPlanMode", "ExitPlanMode"}:
        return
    from bglab.permissions.types import PermissionContext, PermissionMode

    if not isinstance(context.permission_context, PermissionContext):
        return
    from bglab.session.state import session_state

    try:
        context.permission_context.mode = PermissionMode(
            session_state.permission_mode,
        )
    except ValueError:
        return


def _prepare_plan_mode_transition(context: ToolInvocationContext) -> None:
    """Seed the legacy Plan handler from the typed per-query authority."""

    if context.name not in {"EnterPlanMode", "ExitPlanMode"}:
        return
    from bglab.permissions.types import PermissionContext, PermissionMode

    if not isinstance(context.permission_context, PermissionContext):
        return
    mode = context.permission_context.mode
    value = mode.value if isinstance(mode, PermissionMode) else str(mode)
    from bglab.session.state import session_state

    session_state.permission_mode = value


def _mcp_handler(full_name: str):
    def call(arguments: dict[str, Any]) -> str:
        async def invoke():
            from bglab.mcp.manager import get_mcp_manager

            return await get_mcp_manager().call_tool(full_name, arguments)

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            try:
                return pool.submit(
                    lambda: asyncio.run(invoke()),
                ).result(timeout=120)
            except concurrent.futures.TimeoutError:
                raise TimeoutError("MCP Tool call timed out")
            except concurrent.futures.CancelledError:
                raise

    return call


async def execute_code_tool(
    context: ToolInvocationContext,
) -> ToolExecutionOutcome:
    handler = context.handlers.get(context.name)
    if handler is None and context.name.startswith("mcp__"):
        handler = _mcp_handler(context.name)
    if handler is None:
        return await execute_generic_tool(context)
    try:
        _prepare_plan_mode_transition(context)
        if context.name == "Agent":
            result = await asyncio.wait_for(
                asyncio.to_thread(handler, _agent_arguments(context)),
                timeout=300,
            )
        else:
            result = handler(dict(context.arguments))
            if asyncio.iscoroutine(result):
                result = await result
        _sync_plan_mode_transition(context)
        return ToolExecutionOutcome(
            value=result,
            is_error=bool(getattr(result, "is_error", False)),
            metadata=dict(getattr(result, "metadata", {}) or {}),
        )
    except (asyncio.CancelledError, concurrent.futures.CancelledError):
        raise
    except DeferredTargetHandlerFailure as failure:
        logger.exception("Unexpected deferred Code Tool handler failure: %s", context.name)
        return _unexpected_failure(context, failure.permission_spec)
    except Exception:
        logger.exception("Unexpected Code Tool handler failure: %s", context.name)
        return _unexpected_failure(context)


__all__ = [
    "ToolExecutionOutcome",
    "ToolInvocationContext",
    "execute_code_tool",
    "execute_generic_tool",
    "tool_unavailable_outcome",
]
