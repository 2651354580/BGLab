"""Snip — 对齐 snipCompactIfNeeded()。

删掉旧的空结果或被拒绝的工具交流，保留用户输入。

规则:
  - tool_result 为空或全部是 error → 可删除旧工具交流
  - 保留最近 2 个 turn（即使满足删除条件）
  - 始终保留最近一次工具结果所在 turn，后续恢复文字不使它过期
"""

from __future__ import annotations

from bglab.compaction.token_counter import estimate_tokens


def snip_if_needed(
    messages: list[dict],
    min_keep_turns: int = 2,
) -> tuple[list[dict], int]:
    """Snip — 删除空结果/被拒绝的 turn。

    不删除 user message（用户输入）。
    只删除 assistant + 对应的 user(tool_result) pair。

    Args:
        messages: 完整对话历史
        min_keep_turns: 最少保留最近的几个 turn

    Returns:
        (new_messages, tokens_freed)
    """
    tokens_before = estimate_tokens(messages)

    # 按 turn 分组: user(+tool_results) → assistant
    turns = _group_into_turns(messages)
    latest_tool_turn = next((
        index for index in range(len(turns) - 1, -1, -1)
        if any(_has_tool_result(message) for message in turns[index])
    ), None)

    if len(turns) <= min_keep_turns:
        return list(messages), 0

    result_turns = []
    for i, turn in enumerate(turns):
        # 最近的 min_keep_turns 个 turn 保留
        if i >= len(turns) - min_keep_turns or i == latest_tool_turn:
            result_turns.append(turn)
            continue

        # 检查是否可删除
        if _is_empty_or_rejected(turn):
            # A mixed text/result message cannot be kept without its call.
            if any(_has_user_text(message) and _has_tool_result(message) for message in turn):
                result_turns.append(turn)
            else:
                result_turns.append([
                    message for message in turn
                    if message.get("role") == "user" and not _has_tool_result(message)
                ])
            continue

        result_turns.append(turn)

    new_messages = _flatten_turns(result_turns)
    tokens_after = estimate_tokens(new_messages)
    tokens_freed = max(0, tokens_before - tokens_after)

    return new_messages, tokens_freed


def _group_into_turns(messages: list[dict]) -> list[list[dict]]:
    """按 turn 分组 messages。

    一个 turn: user_text → assistant(+tool_use) → user(tool_result) → assistant → ...
    关键区分: role="user" 的 msg:
      - 有 text block → 新用户输入 → 新 turn
      - 只有 tool_result block → 属于当前 turn
    """
    turns = []
    current_turn = []

    for msg in messages:
        role = msg.get("role", "")

        if role == "user":
            if msg.get("_is_meta"):
                current_turn.append(msg)
                continue

            # 检查是否是新用户输入（有 text block）还是 tool_result
            if _has_user_text(msg):
                # 新用户输入 → 新 turn
                if current_turn:
                    turns.append(current_turn)
                current_turn = [msg]
            else:
                # tool_result → 属于当前 turn
                current_turn.append(msg)
            continue

        if role == "assistant":
            current_turn.append(msg)
            continue

    if current_turn:
        turns.append(current_turn)

    return turns


def _has_user_text(msg: dict) -> bool:
    """判断 user message 是否是真实用户输入（有 text block）。"""
    if msg.get("type") == "tool_result":
        return False
    content = msg.get("content", [])
    if isinstance(content, str):
        return bool(content)
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return True
    return False


def _has_tool_result(msg: dict) -> bool:
    if msg.get("role") != "user":
        return False
    if msg.get("type") == "tool_result":
        return True
    content = msg.get("content", [])
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )

def _is_empty_or_rejected(turn: list[dict]) -> bool:
    """检查 turn 是否可以 snip。

    条件:
      - assistant 没有 text（只调了工具，没有正常发言）
      - 所有 tool_result 要么为空，要么是 error
    """
    has_assistant_text = False
    all_tool_results_empty_or_error = True
    has_tool_results = False

    for msg in turn:
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        if block.get("text", "").strip():
                            has_assistant_text = True

        if msg.get("role") == "user":
            if msg.get("type") == "tool_result":
                has_tool_results = True
                text = str(msg.get("content", ""))
                if (
                    text.strip()
                    and not bool(msg.get("is_error", False))
                    and "error" not in text.lower()
                ):
                    all_tool_results_empty_or_error = False
                continue
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        has_tool_results = True
                        text = block.get("content", "")
                        if text.strip() and "error" not in str(text).lower():
                            all_tool_results_empty_or_error = False

    # 没有 tool_results → 不是工具 turn，不删
    if not has_tool_results:
        return False

    return not has_assistant_text and all_tool_results_empty_or_error


def _flatten_turns(turns: list[list[dict]]) -> list[dict]:
    """把 turns 展开回 messages list。"""
    result = []
    for turn in turns:
        result.extend(turn)
    return result
