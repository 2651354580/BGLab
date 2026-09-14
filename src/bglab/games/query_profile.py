"""Game content and triggers plugged into the shared query-loop positions."""

from __future__ import annotations

import inspect
from dataclasses import replace
from typing import Any

from bglab.engine.query_profiles import (
    AgentRuntimeProfile,
    BudgetThinkingProfile,
    DISABLED_MEMORY_PROFILE,
    ErrorRecoveryProfile,
    MemoryProfile,
    OutputProfile,
    ProviderRequestPolicy,
    QueryLoopProfile,
    RelinkProfile,
    StopHookProfile,
    ToolProfile,
    build_shared_context_shaping_profile,
    identity_cwd,
    identity_permission_mode,
)
from bglab.engine.tool_execution import execute_generic_tool
from bglab.engine.turn_input import TurnInput, normalize_turn_input
from bglab.games.attachment_profile import GAME_ATTACHMENT_PROFILE
from bglab.games.compaction_profile import GAME_COMPACTION_PROFILE, retire_superseded_game_frames
from bglab.games.convergence import apply_game_loop_intervention
from bglab.games.hooks import build_game_hooks, build_game_stop_hook_runner
from bglab.games.model_recovery import (
    RECOVERY_REQUEST_ALLOWANCE, prepare_game_request, project_game_recovery_messages,
)
from bglab.games.prompt.profile import GAME_PROMPT_PROFILE
from bglab.llm.providers import resolve_model


GAME_BUDGET_THINKING_PROFILE = BudgetThinkingProfile(
    # Shared output allowance includes thinking and complete tool arguments.
    # Recovery still handles generations that exhaust this larger allowance.
    initial_max_tokens=16_000,
    initial_thinking_effort="low",
    disable_thinking_after_turn=None,
)


GAME_CONTEXT_SHAPING_PROFILE = build_shared_context_shaping_profile(
    name="game",
    # Structured route facts must remain complete. Shared history compaction
    # still owns context pressure; cutting JSON by character count does not.
    max_tool_result_chars=None,
    transform_history=retire_superseded_game_frames,
)


def _game_max_output_recovery_message(attempt: int) -> str:
    return (
        "上一次生成没有交付完整可执行结果，失败草稿已丢弃。"
        "请从最新权威 Frame 和已有 Tool result 继续当前决定。"
        "已有符合意图的完整合法路线时可直接 Commit 其 id；否则用具体玩家选择 Check。"
        "把机械计算交给工具，依据返回的新事实修正决定。"
        f" [attempt {attempt}]"
    )


GAME_ERROR_RECOVERY_PROFILE = ErrorRecoveryProfile(
    name="game",
    max_output_recovery_message=_game_max_output_recovery_message,
    # Keep the recovery seam, but do not silently change the decision budget.
    escalated_max_tokens=16_000,
    max_output_recovery_attempts=1,
    compact_after_output_exhaustion=False,
    preserve_incomplete_assistant=False,
    provider_request=ProviderRequestPolicy(
        name="game-v4-flash",
        max_attempts=2,
        atomic_attempts=True,
        first_event_timeout_seconds=60.0,
        idle_timeout_seconds=60.0,
        attempt_timeout_seconds=180.0,
        parallel_tool_calls=False,
    ),
    deliberation_repetition_guard=True,
)


def _game_recovery_profile(model: str) -> ErrorRecoveryProfile:
    """Enable the tested fallback only for a declared official thinking toggle."""
    try:
        resolved = resolve_model(model)
    except ValueError:
        return GAME_ERROR_RECOVERY_PROFILE
    if resolved.provider.id == "deepseek" and resolved.model.explicit_thinking:
        return replace(
            GAME_ERROR_RECOVERY_PROFILE,
            output_recovery_disable_thinking=True,
            output_recovery_tool_name="BgAct",
        )
    return GAME_ERROR_RECOVERY_PROFILE


def _build_game_compact_relink(agent: Any):
    def build(
        _deps: Any,
        _permission_mode: str,
        _messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        relink: dict[str, Any] = {
            "game_skill_bodies": getattr(agent, "_game_skill_bodies", ""),
        }
        if getattr(agent.profile, 'chat', False):
            from bglab.games.chat import pending_reply_ids, render_received_chat

            pending = set(pending_reply_ids(agent.tool_ctx))
            relink['game_chat'] = '\n'.join(
                render_received_chat(chat, agent.pid)
                for chat in agent.pending_chat if chat['id'] in pending
            )
            relink['current_action_committed'] = bool(agent.tool_ctx.get('_act_submitted'))
        if getattr(agent.profile, "plan", False):
            from bglab.games.plans import visible_plans

            snapshot = visible_plans(agent.tool_ctx)
            if snapshot["revision"]:
                relink["game_plan"] = snapshot
        if getattr(agent, "last_committed_turn_id", None):
            relink["last_action_committed"] = True
        from bglab.games.fallback import fallback_notice

        recovery = fallback_notice(agent)
        if recovery:
            relink["host_recovery"] = {"attachmentId": recovery[0], "text": recovery[1]}
        return relink

    return build


def _observe_game_attachment_append(agent: Any):
    async def observe(_deps: Any, messages: list[dict[str, Any]]) -> None:
        decision_id = getattr(agent.deps, "_game_active_decision_id", None)
        if not isinstance(decision_id, str) or not decision_id:
            return
        for message in messages:
            metadata = message.get("_attachment_metadata", {})
            stage = metadata.get("deliveryStage")
            attachment_id = message.get("_attachment_id")
            if stage != "thread_attachment" or not attachment_id:
                continue
            try:
                result = agent._consume_convergence_delivery({
                    "id": attachment_id,
                    "decision_id": decision_id,
                    "stage": stage,
                })
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # Delivery telemetry is not part of model-visible context and
                # cannot invalidate an otherwise successful attachment append.
                continue

    return observe


async def _collect_game_initial_memory(turn: Any, context: Any):
    return await turn.collect_initial_memory(context)


def _game_turn_message(value: TurnInput | str) -> dict[str, Any]:
    turn_input = normalize_turn_input(value)
    return {
        "role": "user",
        "content": turn_input.text,
        "_turn_input": {"schemaVersion": 1, "kind": turn_input.kind},
        **({"_is_meta": True} if turn_input.is_meta else {}),
    }


def build_game_query_profile(
    agent: Any,
    *,
    stop_after_action: bool,
) -> QueryLoopProfile:
    """Create the per-seat Game profile without changing query-loop shape."""
    allowed_tools = frozenset(tool.name for tool in agent.tools)
    game_hooks = tuple(build_game_hooks(
        memory_extraction=agent._memory_extraction_enabled,
        # Skill evolution is a post-game adapter today; do not advertise a
        # per-turn Stop Hook until a real scheduler is supplied here.
        skill_evolution=False,
    ))
    stop_hook_extra = {
        "engine": agent.engine,
        "game_id": agent.game_id,
        "schedule_memory": agent._schedule_game_memory_extraction,
    }

    def game_tool_surface(
        tools: list[Any],
        _permission_context: Any,
        _permission_mode: str,
    ) -> list[Any]:
        unexpected = {
            tool.name for tool in tools
            if tool.name not in allowed_tools
        }
        if unexpected:
            raise ValueError(
                "game tool surface contains tools outside its Profile: "
                + ", ".join(sorted(unexpected)),
            )
        visible = [tool for tool in tools if tool.name in allowed_tools]
        if agent.profile.chat and agent.tool_ctx.get('_act_submitted'):
            # A reply after Commit cannot open another game action, even when
            # a model ignores the requested tool choice.
            visible = [tool for tool in visible if tool.name == 'BgChat']
        agent.tool_ctx["_semantic_commit_surface_active"] = False
        agent.tool_ctx["_semantic_repair_surface_active"] = False
        return visible

    recovery = _game_recovery_profile(getattr(agent, "model", ""))
    if agent.profile.chat:
        action_recovery_message = recovery.max_output_recovery_message
        recovery = replace(recovery, max_output_recovery_message=lambda attempt: (
            '刚才的回复未完整生成，失败草稿已丢弃。当前游戏行动已提交；只需用 BgChat '
            '回复尚未处理的 game-chat，并填写对应 reply_to。不要再次行动。'
            if agent.tool_ctx.get('_act_submitted') else action_recovery_message(attempt)
        ))
    if not getattr(agent, "host_fallback_enabled", False):
        recovery = replace(
            recovery,
            max_output_recovery_attempts=RECOVERY_REQUEST_ALLOWANCE,
            prepare_request=lambda deps, state: prepare_game_request(
                deps, state, disable_thinking=recovery.output_recovery_disable_thinking,
            ),
            project_request_messages=project_game_recovery_messages,
        )

    return QueryLoopProfile(
        name="game",
        prompt=GAME_PROMPT_PROFILE,
        tools=ToolProfile(
            name="game",
            resolve_surface=game_tool_surface,
            execute=execute_generic_tool,
            resolve_permission_mode=identity_permission_mode,
            resolve_cwd=identity_cwd,
            loop_block_exempt_tools=frozenset({"BgAct"}),
            apply_loop_intervention=apply_game_loop_intervention,
            stop_after_result=lambda: bool(
                stop_after_action
                and (
                    agent.tool_ctx.get("_act_submitted", False)
                    or agent.tool_ctx.get(
                        "_semantic_reconciliation_required", False,
                    )
                )
            ),
            # Game follows the same ordinary Tool loop as Code.  The model may
            # emit a concise conclusion, a Tool call, or both; only the shared
            # would-stop Hook handles a missing durable commit.
            resolve_request_tool_choice=None,
        ),
        attachments=GAME_ATTACHMENT_PROFILE,
        compaction=GAME_COMPACTION_PROFILE,
        context_shaping=GAME_CONTEXT_SHAPING_PROFILE,
        memory=(
            MemoryProfile(
                name="game",
                collect_initial=_collect_game_initial_memory,
            )
            if agent._memory_selection_enabled
            else DISABLED_MEMORY_PROFILE
        ),
        agent_runtime=AgentRuntimeProfile(
            name="game",
            build_turn_message=_game_turn_message,
        ),
        # A plain assistant stop is a would-be DecisionFrame end.  Game also
        # checks the same hook slot at the final max-turn boundary so one
        # protocol-agnostic continuation can be granted there.
        stop_hook=StopHookProfile(
            name="game",
            run=build_game_stop_hook_runner(
                hooks=game_hooks,
                extra=stop_hook_extra,
            ),
            boundaries=frozenset({
                "assistant_stop", "tool_result_stop", "max_turn",
            }),
        ),
        budget_thinking=GAME_BUDGET_THINKING_PROFILE,
        recovery=recovery,
        output=OutputProfile(
            enable_tool_use_summary=False,
            on_attachments_appended=_observe_game_attachment_append(agent),
        ),
        relink=RelinkProfile(build=_build_game_compact_relink(agent)),
    )


__all__ = [
    "GAME_BUDGET_THINKING_PROFILE",
    "GAME_CONTEXT_SHAPING_PROFILE",
    "GAME_ERROR_RECOVERY_PROFILE",
    "build_game_query_profile",
]
