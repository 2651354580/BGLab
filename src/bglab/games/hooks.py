"""The three fixed game-mode Stop Hook positions.

Loop-time reminders belong to ``GAME_ATTACHMENT_PROVIDERS``. Stop Hooks run
only at the shared query loop's fixed end position and remain deliberately
protocol-agnostic:

1. continue an uncommitted action through the decision recovery policy;
2. reserved game-memory position;
3. reserved skill-evolution position.
"""

from __future__ import annotations

from typing import Any


UNCOMMITTED_ACTION_REMINDER = (
    "<system-reminder>\n"
    "当前真实游戏决策尚未成功提交。请依据最新 DecisionFrame 和已有 Tool result（如有）完成当前决策；"
    "不要只回复文字，也不要开始处理下一轮。\n"
    "</system-reminder>"
)


def _action_committed(deps: Any) -> bool:
    tool_ctx = getattr(deps, "_game_tool_ctx", None)
    if isinstance(tool_ctx, dict):
        return bool(tool_ctx.get("_act_submitted", False))
    return bool(getattr(deps, "_game_bg_act_committed", False))


async def hook_bg_act_enforcement(extra: dict) -> dict:
    """Continue in the same Query; its request policy owns the recovery limit."""
    from bglab.games.model_recovery import activate_recovery, model_recovery_enabled
    deps = extra.get("deps")
    if deps is None:
        return {}
    if _action_committed(deps):
        tool_ctx = getattr(deps, '_game_tool_ctx', {})
        if not tool_ctx.get('_chat_enabled'):
            return {}
        from bglab.games.chat import CHAT_REPLY_REQUEST_ALLOWANCE, pending_reply_ids, finish_unanswered

        pending = pending_reply_ids(tool_ctx)
        if pending:
            if tool_ctx.get('_chat_reply_requests', 0) >= CHAT_REPLY_REQUEST_ALLOWANCE:
                finish_unanswered(tool_ctx)
                return {}
            return {'blocking_error': (
                '<system-reminder>当前行动已提交，不要再次行动。请用 BgChat 回复尚未处理的玩家消息，'
                f'reply_to 分别为 {pending}，随后结束。</system-reminder>'
            )}
        return {}
    tool_ctx = getattr(deps, "_game_tool_ctx", None)
    if (
        isinstance(tool_ctx, dict)
        and tool_ctx.get("_semantic_reconciliation_required", False)
    ):
        return {"prevent_continuation": True}

    reminder_count = int(getattr(deps, "_game_missing_act_this_turn", 0) or 0)
    if reminder_count >= 1 and not model_recovery_enabled(deps):
        # One DecisionFrame gets exactly one profile-owned extra round.  A
        # second stop cannot start another loop or another runner submission.
        return {"prevent_continuation": True}
    if reminder_count >= 1:
        activate_recovery(deps, "missing_action")
    deps._game_missing_act_this_turn = reminder_count + 1
    recovery_state = getattr(deps, "_game_recovery_state", None)
    if recovery_state is None:
        recovery_state = {"total": getattr(deps, "_game_missing_act_total", 0)}
        deps._game_recovery_state = recovery_state
    recovery_state["total"] = int(recovery_state.get("total", 0)) + 1
    deps._game_missing_act_total = recovery_state["total"]
    persist = getattr(deps, "_game_recovery_persist", None)
    if persist:
        persist(recovery_state["total"])
    return {"blocking_error": UNCOMMITTED_ACTION_REMINDER}


async def hook_game_memory(extra: dict) -> dict:
    """Run the Game Profile memory adapter after action enforcement passes."""
    schedule = extra.get("schedule_memory")
    if callable(schedule):
        schedule()
    return {}


async def hook_skill_evolution(extra: dict) -> dict:
    """Run an optional Game Profile evolution adapter at the third position."""
    schedule = extra.get("schedule_evolution")
    if callable(schedule):
        schedule()
    return {}


def build_game_hooks(
    *,
    memory_extraction: bool = False,
    skill_evolution: bool = False,
) -> list:
    """Select ordered Game adapters without making the executor read flags."""

    hooks = [hook_bg_act_enforcement]
    if memory_extraction:
        hooks.append(hook_game_memory)
    if skill_evolution:
        hooks.append(hook_skill_evolution)
    return hooks


def build_game_stop_hook_runner(
    *,
    hooks: tuple[Any, ...],
    extra: dict[str, Any],
):
    """Build the complete Game adapter for the shared Stop Hook seam."""

    async def run(**context: Any):
        from bglab.hooks import handle_stop_hooks

        return await handle_stop_hooks(
            context["messages_for_query"],
            context["assistant_messages"],
            context["tool_use_context"],
            state=context["deps"].stop_hooks_state,
            system_prompt=context["system_prompt"],
            user_context=context.get("user_context"),
            system_context=context.get("system_context"),
            cwd=context["cwd"],
            model=context["model"],
            skip_background=True,
            memory_extraction=None,
            callable_failure_policy="raise",
            hooks=list(hooks),
            extra={**extra, "deps": context["deps"]},
        )

    return run


__all__ = [
    "UNCOMMITTED_ACTION_REMINDER",
    "build_game_hooks",
    "build_game_stop_hook_runner",
    "hook_bg_act_enforcement",
    "hook_game_memory",
    "hook_skill_evolution",
]
