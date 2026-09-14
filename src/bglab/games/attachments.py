"""Capability-gated attachment assembly for persistent game players."""

from __future__ import annotations

import html
import hashlib
import math
from dataclasses import dataclass
from typing import Any, Callable

from bglab.engine.attachments import AttachmentValue, ProviderContext
from bglab.games.compaction_profile import build_game_post_compact
from bglab.games.model_recovery import model_recovery_enabled


GAME_TURN_ATTACHMENT_MAX_BYTES = 64 * 1024

GAME_STRATEGY_SCOPE = '以下是条件性的策略参考，按当前局面选用，不是每轮必须逐项完成的检查表。当前行动的费用和结算可以由 Frame 与 Check 确认；未来骰子、市场、工位及对手选择仍有不确定性。资源准备和持久发展按合理的未来机会评价，区分确定事实与预期，不需要证明整个剩余对局的最优路线。\n\n'


class GameAttachmentError(ValueError):
    """Raised before a provider can send ambiguous or oversized context."""


@dataclass(frozen=True)
class GameAttachmentProviderSpec:
    """One entry in the only game attachment registry.

    ``user_input`` builders consume ``(agent, game_context)`` and return
    attachment messages. ``thread`` builders consume ``(deps, state,
    tool_uses)`` and return reminder text or ``None``. The profile adapter is
    the only caller that knows those two implementation signatures.
    """

    provider_id: str
    slot: str
    capability: str
    timing: str
    max_bytes: int
    build: Callable[..., Any]
    interface: str = "agent"
    required: bool = False

    def __post_init__(self) -> None:
        if not self.provider_id:
            raise ValueError("game attachment provider id must be non-empty")
        if self.slot not in {
            "head", "user_input", "thread", "post_compact", "memory_prefetch",
        }:
            raise ValueError("unknown game attachment slot")
        if self.max_bytes <= 0:
            raise ValueError("game attachment provider max_bytes must be positive")
        if self.interface not in {"agent", "context", "thread"}:
            raise ValueError("unknown game attachment provider interface")


@dataclass(frozen=True)
class GameThreadAttachment:
    """Thread-slot content with a stable trigger-specific identity."""

    text: str
    trigger_id: str


def _context_attachment(attachment_id: str, text: str) -> dict[str, Any]:
    return {
        "role": "user",
        "type": "attachment",
        "attachment_type": "game_context",
        "content": [{"type": "text", "text": text}],
        "_is_meta": True,
        "_attachment_id": attachment_id,
    }


def _skill_routing_provider(
    agent: Any, context: dict[str, Any],
) -> list[dict]:
    # Discovery metadata uses the existing attachment slot. Bodies still need
    # a model invocation, and a loaded method is no longer advertised.
    if not getattr(agent.profile, "skill_prefetch", False):
        agent._sync_game_skill_relink_bodies()
        from bglab.games.skills.loader import get_bundle_matching_skill_metadata

        pending = [guide for guide in get_bundle_matching_skill_metadata(
            agent.skill_bundle, context["state"], seat=agent.pid,
            limit=len(agent.skill_bundle.skills),
        ) if guide["name"] not in agent.loaded_skills]
        if not pending:
            return []
        catalog = "\n".join(
            f"- {guide['name']}：{guide['description']}" for guide in pending[:3]
        )
        return [_context_attachment(
            f"game_skills:{agent.game_id}:p{agent.pid}:turn:{context['turn']}",
            "<game-skills>\n以下专题方法可按需读取，当前只有目录，尚未加载正文：\n"
            + catalog
            + "\n若当前需要展开这类比较，可以先读取相应 Skill；已经确定的行动照常完成，"
            "无需为调用而调用。\n</game-skills>",
        )]
    from bglab.games.skills.loader import (
        get_bundle_matching_skill_metadata,
    )

    state = context["state"]
    phase = context["strategy_phase"]
    matching = (
        get_bundle_matching_skill_metadata(
            agent.skill_bundle, state, seat=agent.pid, limit=2,
        )
        if state else []
    )
    pending = [
        guide for guide in matching
        if agent.loaded_skill_phases.get(str(guide["name"])) != phase
    ]
    inactive = [
        name for name, loaded_phase in agent.loaded_skill_phases.items()
        if loaded_phase not in {"unknown", phase}
    ]
    agent._sync_game_skill_relink_bodies()
    if pending and getattr(agent.profile, "skill_prefetch", False):
        bodies: list[str] = []
        for guide in pending:
            name = str(guide["name"])
            body = agent._activate_game_skill(
                {"skill": name},
                name,
                event_type="skill_prefetched",
                phase=phase,
            )
            if not body.startswith("Error") and "not found" not in body.lower():
                bodies.append(body)
        if bodies:
            return [_context_attachment(
                f"game_skills:{agent.game_id}:p{agent.pid}:turn:{context['turn']}",
                "<game-skills>\n系统已按当前 Profile 预取已选静态攻略；"
                "攻略只提供策略参考，规则与合法性仍以 Authority 为准。\n"
                + "\n\n".join(bodies)
                + "\n</game-skills>",
            )]
    if not pending and not inactive:
        return []

    lines: list[str] = []
    if inactive:
        lines.append(
            "策略阶段已经变化；以下旧阶段攻略已失效，不得继续作为当前动作依据："
            + "、".join(sorted(inactive))
            + "。其正文可能仍在历史中，但当前局面必须按新阶段重新判断。"
        )
    if pending:
        lines.append(
            f"系统记录确认当前 {phase} 阶段尚未加载"
            "以下匹配攻略正文（这里只提供路由信息，不注入攻略正文）："
        )
        for guide in pending:
            lines.append(
                f"- {guide['name']}：{guide['description']}；"
                f"匹配条件={guide['when']}"
            )
        lines.append(
            "若本阶段尚未读取，请尽快调用 Skill 工具（Skill tool）"
            "加载需要的攻略；攻略是参考，动作仍由你判断。"
        )
    cue = "\n".join(lines)
    return [_context_attachment(
        f"game_skills:{agent.game_id}:p{agent.pid}:turn:{context['turn']}",
        "<game-skills>\n" + cue + "\n</game-skills>",
    )]


def _plan_provider(agent: Any, context: dict[str, Any]) -> list[dict]:
    from bglab.games.plans import render_plans, visible_plans

    snapshot = visible_plans(agent.tool_ctx)
    if not snapshot["revision"]:
        return []
    # The shared attachment lifecycle deduplicates this revision across turns.
    # A fresh compaction snapshot is supplied by the Game relink provider.
    return [_context_attachment(
        f"game_plan:{agent.game_id}:p{agent.pid}:revision:{snapshot['revision']}",
        render_plans(snapshot),
    )]


def _chat_provider(agent: Any, context: dict[str, Any]) -> list[dict]:
    from bglab.games.chat import CHAT_BATCH_SIZE, render_received_chat

    queued = list(agent.pending_chat[:CHAT_BATCH_SIZE])
    agent.tool_ctx['_chat_batch'] = [chat['id'] for chat in queued]
    attachments = []
    for chat in queued:
        attachments.append(_context_attachment(
            f"game_chat:{agent.game_id}:p{agent.pid}:{chat.get('id', 0)}",
            render_received_chat(chat, agent.pid),
        ))
    return attachments


def _recovery_provider(agent: Any, context: dict[str, Any]) -> list[dict]:
    from bglab.games.fallback import fallback_notice

    notice = fallback_notice(agent)
    return [_context_attachment(*notice)] if notice else []


def build_game_session_head_text(
    *,
    definition: Any,
    rules: str | None = None,
    strategy: str | None = None,
    seat: int | None = None,
) -> str:
    if rules is None:
        rules_path = getattr(definition, "rules_path", None)
        if rules_path is None:
            raise GameAttachmentError("game definition has no complete rules path")
        rules = rules_path.read_text(encoding="utf-8")
    if strategy is None:
        strategy_path = getattr(definition, "session_head_path", None)
        strategy = (
            strategy_path.read_text(encoding="utf-8")
            if strategy_path is not None
            else ""
        )
    rules_text = str(rules or "").strip()
    strategy_text = str(strategy or "").strip()
    if not rules_text:
        raise GameAttachmentError("complete game rules are empty")
    rules_revision = hashlib.sha256(rules_text.encode("utf-8")).hexdigest()[:12]
    strategy_revision = (
        hashlib.sha256(strategy_text.encode("utf-8")).hexdigest()[:12]
        if strategy_text
        else "none"
    )
    capabilities = getattr(definition, "capabilities", None)
    information_model = getattr(capabilities, "information_model", "seat_private")
    seat_label = (
        f"P{seat}"
        if isinstance(seat, int) and not isinstance(seat, bool) and seat >= 0
        else "the current Player seat"
    )
    private_description = (
        "No seat-private game information is declared by this package."
        if information_model == "perfect"
        else (
            f"Only information explicitly authorized for {seat_label} may be used; "
            "another seat's private state and hidden future state remain unknown."
        )
    )
    strategy_section = (
        "<game-strategy>\n"
        + GAME_STRATEGY_SCOPE
        + strategy_text
        + "\n</game-strategy>\n"
        if strategy_text
        else ""
    )
    return (
            "<game-session-head>\n"
            "<game-identity>\n"
            f"game={definition.title}\n"
            f"rules version=packaged rules revision {rules_revision}\n"
            f"strategy version=packaged strategy revision {strategy_revision}\n"
            "</game-identity>\n"
            "<game-information-boundary>\n"
            f"authorized seat={seat_label}\n"
            "public information=Only facts explicitly marked public by the latest "
            "DecisionFrame and Tool results are shared across seats.\n"
            f"seat-private information={private_description}\n"
            "</game-information-boundary>\n"
            "<game-rules>\n"
            + rules_text
            + "\n</game-rules>\n"
            + strategy_section
            + "</game-session-head>"
    )


def _game_session_head_provider(context: ProviderContext) -> tuple[AttachmentValue, ...]:
    agent = getattr(context.deps, "_game_attachment_agent", None)
    if agent is None:
        return ()
    return (AttachmentValue(
        kind="game_session_head",
        text=build_game_session_head_text(
            definition=agent.definition,
            seat=int(agent.pid),
        ),
    ),)


async def _game_memory_provider(context: ProviderContext) -> tuple[AttachmentValue, ...]:
    agent = getattr(context.deps, "_game_attachment_agent", None)
    if agent is None:
        return ()
    prompt = str(context.local.get("turn_input") or "").strip()
    for message in reversed(context.messages):
        if prompt:
            break
        if message.get("role") != "user" or message.get("_is_meta"):
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            prompt = content
        elif isinstance(content, list):
            prompt = "".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        if prompt:
            break
    messages = await agent._game_memory_attachments(prompt)
    return tuple(
        AttachmentValue(
            kind=str(message.get("attachment_type", "game_context")),
            text="".join(
                str(block.get("text", ""))
                for block in message.get("content", [])
                if isinstance(block, dict)
            ),
            attachment_id=message.get("_attachment_id"),
        )
        for message in messages
    )


def _next_decision_waiting_attachment(deps: Any, _state: Any, _blocks: Any) -> str | None:
    notice = getattr(deps, "_game_pending_decision_notice", None)
    if not isinstance(notice, dict) or not notice.get("decisionId"):
        return None
    return (
        "<system-reminder>\n"
        "同一席位的新一轮权威决策已经到达并在邮箱中等待。"
        "请尽快完成当前轮次的收尾；不要在当前轮次读取、猜测或执行新一轮状态。\n"
        "</system-reminder>"
    )


def _turn_budget_reminder_attachment(
    deps: Any,
    state: Any,
    _blocks: Any,
) -> GameThreadAttachment | None:
    tool_ctx = getattr(deps, "_game_tool_ctx", None)
    if isinstance(tool_ctx, dict) and tool_ctx.get("_act_submitted", False):
        return None
    recovery = getattr(deps, "_game_model_recovery", {})
    if recovery.get("reason"):
        return GameThreadAttachment(
            text=("<system-reminder>\n"
                  "当前决策已自动进入行动恢复，权威局面没有改变。"
                  "请给出一个完整 BgAct 调用：已有符合意图的完整合法路线时 Commit 其 id；"
                  "否则根据已有反馈修正具体玩家选择后 Check。不要重复无变化的查询。\n"
                  "</system-reminder>"),
            trigger_id="action_recovery",
        )
    alert = getattr(deps, "_game_loop_alert", None)
    if alert and alert.get("severity") == "warning":
        return GameThreadAttachment(
            text=("<system-reminder>\n"
                  "相同工具参数已经返回相同结果多次，没有得到新事实。BgAct 仍然可用。"
                  "已有符合意图的完整合法路线时可直接 Commit 其 id；否则针对返回的具体问题"
                  "修改玩家选择后再 Check。继续比较应带来新的行动或新的事实。\n"
                  "</system-reminder>"),
            trigger_id="no_progress",
        )
    if model_recovery_enabled(deps):
        return None
    turn_count = int(getattr(state, "turn_count", 0) or 0)
    max_turns = max(1, int(getattr(state, "max_turns", 0) or 0))
    threshold_60 = max(1, math.ceil(max_turns * 0.60))
    threshold_80 = max(threshold_60 + 1, math.ceil(max_turns * 0.80))
    if turn_count >= threshold_80:
        return GameThreadAttachment(
            text=(
                "<system-reminder>\n"
                "当前 DecisionFrame 的可用轮次已经很少。请停止无必要的重复比较，"
                "尽快提交当前合法行动；只有仍存在不确定选择时才先校验。\n"
                "</system-reminder>"
            ),
            trigger_id="80pct",
        )
    if turn_count >= threshold_60:
        return GameThreadAttachment(
            text=(
                "<system-reminder>\n"
                "当前 DecisionFrame 的轮次预算正在收紧。请开始收敛路线，并为及时提交"
                "保留足够轮次。\n"
                "</system-reminder>"
            ),
            trigger_id="60pct",
        )
    return None


# One ordered registry; each entry owns its slot, trigger, budget, and content.
GAME_ATTACHMENT_PROVIDERS: tuple[GameAttachmentProviderSpec, ...] = (
    GameAttachmentProviderSpec(
        "game_session_head", "head", "always", "every_request", 64 * 1024,
        _game_session_head_provider, "context", True,
    ),
    GameAttachmentProviderSpec(
        "skill_routing", "user_input", "skills", "every_turn", 16 * 1024,
        _skill_routing_provider,
    ),
    GameAttachmentProviderSpec(
        "plan", "user_input", "plan", "every_turn", 16 * 1024, _plan_provider,
    ),
    GameAttachmentProviderSpec(
        "host_recovery", "user_input", "always", "every_turn", 4 * 1024, _recovery_provider,
    ),
    GameAttachmentProviderSpec(
        "chat", "user_input", "chat", "every_turn", 8 * 1024, _chat_provider,
    ),
    GameAttachmentProviderSpec(
        "game_memory", "memory_prefetch", "memory_selection", "inter_turn", 8 * 1024,
        _game_memory_provider, "context",
    ),
    GameAttachmentProviderSpec(
        "next_decision_waiting", "thread", "always", "post_tool", 16 * 1024,
        _next_decision_waiting_attachment, "thread",
    ),
    GameAttachmentProviderSpec(
        "turn_budget_reminder", "thread", "always", "post_tool", 16 * 1024,
        _turn_budget_reminder_attachment, "thread",
    ),
    GameAttachmentProviderSpec(
        "game_post_compact", "post_compact", "always", "post_compact", 64 * 1024,
        build_game_post_compact, "context", True,
    ),
)


def _capability_enabled(agent: Any, capability: str) -> bool:
    if capability == "always":
        return True
    if capability == "skills":
        return bool(agent.skills_enabled)
    if capability in {"plan", "chat", "memory_selection"}:
        return bool(getattr(agent.profile, capability, False))
    raise GameAttachmentError(
        f"unknown attachment provider capability: {capability}"
    )


def _provider_context(agent: Any) -> dict[str, Any]:
    state = agent.tool_ctx.get("_state", {})
    phase = "unknown"
    if agent.skills_enabled:
        from bglab.games.skills.loader import strategy_phase

        phase = strategy_phase(state)
        agent.tool_ctx["_strategy_phase"] = phase
    return {
        "turn": agent.game_turn_count,
        "state": state,
        "strategy_phase": phase,
    }
