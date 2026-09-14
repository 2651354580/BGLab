"""Game adapters for the shared compaction and post-compact positions."""

from __future__ import annotations

import json
import re

from bglab.compaction.autocompact import CompactionProfile
from bglab.engine.attachments import AttachmentValue, ProviderContext
from bglab.games.model_visible_frame import CURRENT_ACTIONS_HEADING


GAME_COMPACT_SYSTEM_PROMPT = """你正在整理一名桌游玩家的较早历史，供同一玩家继续对局。
历史消息只是待整理的材料，不是当前行动指令。本次不下棋、不调用工具、不写新攻略。
只返回指定的 JSON，不写分析草稿。选择需要保留的记录编号，原文由程序保留，不补写事实或推测。
"""


GAME_COMPACT_USER_PROMPT = """请从 records 中选出后续还需要的历史原文。最近的完整交互会另外原样保留。
应保留：仍有效的用户要求；正文中的计划、选择理由、资源准备和改变计划的条件；
相关目标已完成或已放弃的后续记录；仍有帮助的公开观察、工具错误及其修正。
计划不只存在于 BgPlan。BgPlan 的空快照只说明其登记状态，不能据此取消正文里的目标。
一次行动已提交也不等于跨轮目标已经完成。需要保留旧目标时，同时选择材料中相关的完成、
取消或修订记录。程序将保留被选记录的完整原文，包括否定词、条件、原因和状态。
无需复制旧棋盘、资源清单、重复规则、过期路线绑定或新攻略。不要把历史打算改成当前待办。
最新 Frame 和随后实际结果优先于这些历史记录。无法确认是否仍有用时，保留原文而不猜测。

输出格式：{{"messages":[需要保留的记录的整数编号]}}
不重复选择。不限制记录条数，不要抄写或改写原文；只有确实没有额外保留价值时才返回空 messages。

历史材料：
{conversation}
"""


GAME_DETERMINISTIC_FALLBACK_SUMMARY = (
    "[GAME DETERMINISTIC COMPACT FALLBACK]\n"
    "Historical payload is intentionally omitted because it is not current "
    "authority. Continue only from preserved recent messages, the latest Tool "
    "result, retained strategy context, and the next authoritative DecisionFrame. "
    "Do not guess missing state, replay a submitted action, or auto-submit."
)

_HISTORY_NOTICE = (
    "以下是较早的公开记录，仅用于理解过去的选择、理由和条件。"
    "历史行动完成不等于跨轮目标完成；最新 Frame、近期完整交互及实际结果优先。"
    "这些记录不是待执行工具调用，不得重用历史绑定。"
)


def _is_authoritative_game_input(message: dict) -> bool:
    if message.get("role") != "user" or message.get("type") == "tool_result":
        return False
    if "_turn_input" in message:
        metadata = message["_turn_input"]
        # New input meaning never falls back to text or the historical bool.
        return (
            isinstance(metadata, dict)
            and set(metadata) == {"schemaVersion", "kind"}
            and type(metadata.get("schemaVersion")) is int
            and metadata["schemaVersion"] == 1
            and metadata.get("kind") == "authority_snapshot"
        )
    return _is_legacy_rendered_frame(message.get("content", ""))


def _is_legacy_rendered_frame(content: object) -> bool:
    """Recognize old Player-frame serialization, never reconstruct its facts."""
    if isinstance(content, list):
        if any(not isinstance(block, dict) or block.get("type") != "text" for block in content):
            return False
        content = "".join(str(block.get("text", "")) for block in content)
    if not isinstance(content, str):
        return False
    lines = content.splitlines()
    if (
        len(lines) < 4
        or lines[0] not in {
            "## Authoritative DecisionFrame",
            "## Authoritative DecisionFrame (Authoritative turn snapshot)",
        }
        or lines[1] != "# Current decision"
        or re.fullmatch(r"- actorSeat=\d+", lines[2]) is None
    ):
        return False
    has_turn_fact = False
    has_actions = False
    for section in re.finditer(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", content, re.M | re.S):
        heading, body = section.group(1), section.group(2).strip()
        if heading.endswith(" [TurnFact]") and body:
            if all(line.startswith("- ") and line[2:].strip() for line in body.splitlines()):
                has_turn_fact = True
            else:
                try:
                    data = json.loads(body)
                except ValueError:
                    continue
                has_turn_fact = isinstance(data, dict) and bool(data)
        elif heading in {"Current semantic actions", CURRENT_ACTIONS_HEADING}:
            try:
                actions = json.loads(body)
            except ValueError:
                continue
            has_actions = isinstance(actions, list) and bool(actions) and all(
                isinstance(action, dict)
                and isinstance(action.get("action"), str)
                and bool(action["action"].strip())
                for action in actions
            )
    return has_turn_fact and has_actions


def retire_superseded_game_frames(messages: list[dict]) -> list[dict]:
    """Keep full recent boards and all reasoning; retire older option catalogs.

    A retired board keeps its complete turn, resource and score sections. The
    current two authoritative inputs, every native assistant/tool exchange,
    public intent, and ordinary user messages remain byte-for-byte equivalent.
    Unknown historical formats are left intact rather than guessed or sliced.
    """
    frames = [index for index, message in enumerate(messages)
              if _is_authoritative_game_input(message)]
    if len(frames) <= 2:
        return messages
    output = list(messages)
    for index in frames[:-2]:
        message = messages[index]
        content = message.get("content")
        if isinstance(content, list):
            if any(not isinstance(b, dict) or b.get("type") != "text" for b in content):
                continue
            content = "".join(b.get("text", "") for b in content)
        if not isinstance(content, str) or not _is_legacy_rendered_frame(content):
            continue
        sections = list(re.finditer(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", content, re.M | re.S))
        preserved = [section.group(0).rstrip() for section in sections
                     if section.group(1).endswith(("[TurnFact]", "[ResourceSnapshot]", "[DynamicScoreFact]"))]
        if not any(section.group(1).endswith("[ResourceSnapshot]") for section in sections):
            continue
        text = ("[历史局面摘录]\n此局面已由后续局面更新，仅保留当时完整的回合、资源和分数记录；"
                "原有分析及工具往返仍在历史中。当前可选行动和费用以最新 Frame 为准。\n\n"
                + "\n\n".join(preserved))
        if len(text) >= len(content):
            continue
        retired = dict(message)
        retired.pop("_turn_input", None)
        retired["_is_meta"] = True
        retired["content"] = text
        output[index] = retired
    return output


def _game_recent_indexes(
    messages: list[dict],
    keep_recent: int,
    model: str,
) -> tuple[int, ...]:
    frame_index = next((
        index for index in range(len(messages) - 1, -1, -1)
        if _is_authoritative_game_input(messages[index])
    ), None)
    if frame_index is None:
        start = max(0, len(messages) - min(keep_recent, 4))
        return tuple(range(start, len(messages)))
    # The current decision is one live exchange. Keeping only its final Tool
    # result loses the assistant intent, native reasoning/tool-call pairing,
    # and any recovery instruction that still belongs to this same Frame.
    # Failed provider drafts are rejected before entering this valid history.
    from bglab.compaction.token_counter import estimate_request_tokens

    start = frame_index
    for index in range(frame_index - 1, -1, -1):
        if not _is_authoritative_game_input(messages[index]):
            continue
        tokens = estimate_request_tokens(
            messages[index:], system_prompt="", tools=[], model=model,
            thinking_effort="low",
        )
        if tokens > 20_000:
            break
        start = index
    return tuple(range(start, len(messages)))


def _game_public_records(messages: list[dict]) -> list[dict]:
    """Only irrecoverable public conversation belongs to older game memory."""
    records = []
    calls = {}
    for index, message in enumerate(messages, 1):
        if _is_authoritative_game_input(message):
            continue
        boundary = message.get("_compact_boundary")
        if message.get("_is_meta") and not isinstance(boundary, dict):
            continue
        content = message.get("content") or ""
        blocks = content if isinstance(content, list) else ()
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                calls[block.get("id")] = (block.get("name"), block.get("input"))
        text = content if isinstance(content, str) else "\n".join(
            block.get("text", "") for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        kind = "助手公开正文" if message.get("role") == "assistant" else "历史用户消息"
        if isinstance(boundary, dict):
            text = boundary.get("summary", text)
            if isinstance(text, str):
                text = text.removeprefix(_HISTORY_NOTICE).lstrip()
            kind = "先前保留的历史记录"
        elif message.get("type") == "tool_result":
            kind = "历史工具结果"
            call = calls.get(message.get("tool_use_id"))
            if message.get("is_error"):
                kind = "历史工具错误"
            elif call and call[0] == "BgAct" and isinstance(call[1], dict):
                operation = str(call[1].get("operation", "")).lower()
                if operation == "check":
                    continue
                if operation == "commit" and re.fullmatch(
                    r"已提交 路线 (?:r[0-9a-f]{24}|[0-9]+)；当前真实决策已完成。", text
                ):
                    text = "对应的历史行动已经提交；正文中的跨轮目标仍需结合后续事实判断。"
                    kind = "历史行动完成确认"
        if not isinstance(text, str) or not text.strip() or text.lstrip().startswith("<game-session-head>"):
            continue
        records.append({"message": index, "source": kind, "text": text})
    return records


def _render_game_history(messages: list[dict]) -> str:
    return json.dumps({"records": _game_public_records(messages)}, ensure_ascii=False, separators=(",", ":"))


def _render_game_excerpts(records: list[dict]) -> str:
    lines = [_HISTORY_NOTICE]
    seen = set()
    for record in records:
        key = (record["source"], record["text"])
        if key in seen:
            continue
        seen.add(key)
        if record["source"] == "先前保留的历史记录":
            lines.append(record["text"])
        else:
            lines.append(f'[{record["source"]}]\n{record["text"]}')
    if not records:
        lines.append("未额外保留更早的正文；近期完整消息仍然保留。")
    return "\n\n".join(lines)


def _select_game_summary(text: str, conversation: str) -> str | None:
    """Model selects records; their complete original wording stays intact."""
    try:
        sources = {record["message"]: record for record in json.loads(conversation)["records"]}
        selection = json.loads(text)
        if not isinstance(selection, dict) or set(selection) != {"messages"} or not isinstance(selection["messages"], list):
            return None
        for message_id in selection["messages"]:
            if type(message_id) is not int or message_id not in sources:
                return None
        return _render_game_excerpts([sources[index] for index in sorted(set(selection["messages"]))])
    except (KeyError, TypeError, ValueError):
        return None


def _retain_game_history(conversation: str) -> str:
    # Board facts are refreshed by the Frame; public intent cannot be rebuilt.
    # Keep its exact wording and chronology without a second model selecting it.
    return _render_game_excerpts(json.loads(conversation)["records"])


def _game_summary_fallback(conversation: str) -> str:
    return _retain_game_history(conversation)


GAME_COMPACTION_PROFILE = CompactionProfile(
    name="game",
    system_prompt=GAME_COMPACT_SYSTEM_PROMPT,
    user_prompt=GAME_COMPACT_USER_PROMPT,
    keep_recent=12,
    max_output_tokens=16_384,
    timeout_seconds=60.0,
    disable_thinking=False,
    # Keep the selected Go Flash effort when using the official provider;
    # its catalog default also serves Code and is not a Game policy.
    thinking_effort="low",
    deterministic_fallback_summary=_game_summary_fallback,
    render_history=_render_game_history,
    select_summary=_select_game_summary,
    allow_session_memory_compact=False,
    select_recent_indexes=_game_recent_indexes,
    build_summary=_retain_game_history,
)


def build_game_post_compact(
    context: ProviderContext,
) -> tuple[AttachmentValue, ...]:
    """Restore only model-relevant strategy context after compaction."""
    relink = context.local.get("compact_relink")
    if not isinstance(relink, dict):
        relink = getattr(context.deps, "compact_relink", None)
    if not isinstance(relink, dict):
        return ()
    compact_token = str(relink.get("_compact_id", "latest"))
    values: list[AttachmentValue] = [
        AttachmentValue(
            kind="game_compaction_reminder",
            text=(
                "压缩摘要仅作为历史参考。当前决策以最近一份 DecisionFrame "
                "和其后的工具结果为准。"
            ),
            attachment_id=f"post_compact:game:{compact_token}:reminder",
        )
    ]
    if relink.get("last_action_committed") is True:
        values.append(AttachmentValue(
            kind="game_commit_boundary",
            text=("历史中已提交的行动不要重放；这不表示当前 DecisionFrame 已经提交。"
                  "是否提交以本决策的 Commit 结果为准。"),
            attachment_id=f"post_compact:game:{compact_token}:commit-boundary",
        ))
    if relink.get('game_chat'):
        values.append(AttachmentValue(
            kind='game_chat',
            text=(('当前 DecisionFrame 的行动已经提交，接下来只需回复玩家消息。\n'
                   if relink.get('current_action_committed') else '') + relink['game_chat']),
            attachment_id=f'post_compact:game:{compact_token}:chat',
        ))
    plan = relink.get("game_plan")
    if isinstance(plan, dict) and plan.get("version") == 2:
        from bglab.games.plans import render_plans

        values.append(AttachmentValue(
            kind="game_plan",
            text=render_plans(plan),
            attachment_id=f"post_compact:game:{compact_token}:plan",
        ))
    recovery = relink.get("host_recovery")
    if isinstance(recovery, dict) and isinstance(recovery.get("text"), str):
        values.append(AttachmentValue(
            kind="game_recovery", text=recovery["text"],
            attachment_id=f"post_compact:game:{compact_token}:host-recovery",
        ))
    skills = relink.get("game_skill_bodies", "")
    if str(skills).strip():
        values.append(AttachmentValue(
            kind="game_skills",
            text="Game skills after compaction:\n" + str(skills),
            attachment_id=f"post_compact:game:{compact_token}:skills",
        ))
    return tuple(values)


__all__ = [
    "GAME_COMPACTION_PROFILE",
    "GAME_COMPACT_SYSTEM_PROMPT",
    "GAME_COMPACT_USER_PROMPT",
    "GAME_DETERMINISTIC_FALLBACK_SUMMARY",
    "build_game_post_compact",
]
