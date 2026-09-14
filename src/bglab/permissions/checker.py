"""Metadata-authoritative Tool permission evaluation."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from bglab.permissions.types import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
    PermissionMode,
    PermissionRule,
    ToolPermissionSpec,
)


logger = logging.getLogger("bglab.permissions")


def evaluate_tool_permission(
    tool_name: str,
    tool_input: dict,
    spec: ToolPermissionSpec,
    context: PermissionContext | None,
    *,
    permission_mode: str = "default",
    project_cwd: str | None = None,
) -> PermissionDecision:
    """Apply deny, ask, checker, mode, allow, then confirmation order."""

    if context is None:
        return PermissionDecision(
            PermissionBehavior.ALLOW,
            "unmanaged permission context",
        )
    mode = _permission_mode(context, permission_mode)

    deny_rule = _matching_rule(context.always_deny, tool_name, tool_input)
    if deny_rule:
        return PermissionDecision(
            PermissionBehavior.DENY,
            f"denied by [{deny_rule.source}]: "
            f"{deny_rule.rule_content or tool_name}",
        )

    pending_ask: PermissionDecision | None = None
    ask_rule = _matching_rule(context.always_ask, tool_name, tool_input)
    if ask_rule:
        pending_ask = PermissionDecision(
            PermissionBehavior.ASK,
            f"always-ask by [{ask_rule.source}]: "
            f"{ask_rule.rule_content or tool_name}",
        )

    if spec.checker is not None:
        try:
            checker_decision = spec.checker(tool_input)
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            raise
        except Exception:
            logger.exception("Tool permission checker failed: %s", tool_name)
            return PermissionDecision(
                PermissionBehavior.DENY,
                "tool permission checker failed",
            )
        if checker_decision.behavior == PermissionBehavior.DENY:
            return checker_decision
        if (
            checker_decision.behavior == PermissionBehavior.ASK
            and pending_ask is None
        ):
            pending_ask = checker_decision

    if mode == PermissionMode.PLAN and not spec.plan_allowed:
        return PermissionDecision(
            PermissionBehavior.DENY,
            f"plan mode: {tool_name} denied",
        )

    from bglab.permissions.path_policy import is_within_project

    project_scope = is_within_project(tool_name, tool_input, project_cwd)
    if pending_ask is not None:
        if mode == PermissionMode.DONT_ASK:
            return PermissionDecision(
                PermissionBehavior.DENY,
                f"dont_ask mode: {pending_ask.reason}",
            )
        return pending_ask

    allow_rule = _matching_rule(context.always_allow, tool_name, tool_input)
    if project_scope is False:
        if mode == PermissionMode.BYPASS:
            return PermissionDecision(PermissionBehavior.ALLOW, "bypass mode")
        if allow_rule is not None:
            return _allowed_by_rule(allow_rule, tool_name)
        if mode == PermissionMode.DONT_ASK:
            return PermissionDecision(
                PermissionBehavior.DENY,
                f"dont_ask mode: {tool_name} outside project",
            )
        return PermissionDecision(
            PermissionBehavior.ASK,
            f"{tool_name} path is outside the project scope and needs confirmation",
        )

    if (
        mode == PermissionMode.ACCEPT_EDITS
        and project_scope is True
        and not spec.read_only
    ):
        return PermissionDecision(
            PermissionBehavior.ALLOW,
            "accept_edits mode: project-scoped edit",
        )
    if spec.auto_allow:
        return PermissionDecision(
            PermissionBehavior.ALLOW,
            "Tool permission spec: auto-allow",
        )
    if mode == PermissionMode.BYPASS:
        return PermissionDecision(PermissionBehavior.ALLOW, "bypass mode")
    if allow_rule is not None:
        return _allowed_by_rule(allow_rule, tool_name)
    if mode == PermissionMode.DONT_ASK:
        return PermissionDecision(
            PermissionBehavior.DENY,
            "dont_ask mode: confirmation unavailable",
        )
    return PermissionDecision(
        PermissionBehavior.ASK,
        "permission confirmation required",
    )


def has_permission_to_use_tool(
    tool_name: str,
    tool_input: dict,
    tool_check_permission,
    context: PermissionContext,
    *,
    is_cli: bool = True,
) -> PermissionDecision:
    """Compatibility wrapper; the typed evaluator remains the sole policy."""

    del is_cli
    return evaluate_tool_permission(
        tool_name,
        tool_input,
        ToolPermissionSpec(checker=tool_check_permission),
        context,
    )


def _permission_mode(
    context: PermissionContext,
    fallback: str,
) -> PermissionMode:
    raw = (
        context.mode.value
        if isinstance(context.mode, PermissionMode)
        else str(context.mode or fallback)
    )
    try:
        return PermissionMode(raw)
    except ValueError:
        return PermissionMode.DEFAULT


def _matching_rule(
    rules_by_tool: dict[str, list[PermissionRule]],
    tool_name: str,
    tool_input: dict,
) -> PermissionRule | None:
    return next((
        rule
        for rule in rules_by_tool.get(tool_name, [])
        if _rule_matches(rule, tool_input)
    ), None)


def _allowed_by_rule(
    rule: PermissionRule,
    tool_name: str,
) -> PermissionDecision:
    return PermissionDecision(
        PermissionBehavior.ALLOW,
        f"allowed by [{rule.source}]: {rule.rule_content or tool_name}",
    )


def _rule_matches(rule: PermissionRule, tool_input: dict | None) -> bool:
    if not rule.rule_content:
        return True
    if tool_input is None:
        return False
    content = rule.rule_content
    if content.startswith("contains:"):
        path = content[len("contains:"):]
        if "file_path" in tool_input:
            return path in str(tool_input.get("file_path", ""))
        return any(path in str(value) for value in tool_input.values())
    if content.startswith("prefix:"):
        prefix = content[len("prefix:"):]
        return str(tool_input.get("command", "")).strip().startswith(prefix)
    return False


__all__ = ["evaluate_tool_permission", "has_permission_to_use_tool"]
