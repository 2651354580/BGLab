"""Microcompact — 对齐 microCompact.ts。

两种路径:
  1. Time-based: 最后的 assistant message 距今超过阈值(60min) → 清除旧 tool result
  2. Cached: (skip, Ant-only CACHED_MICROCOMPACT feature)

清除逻辑(对齐 maybeTimeBasedMicrocompact):
  - 收集所有 compactable tool_result 的 tool_use_id
  - 保留最近 keepRecent 个(默认8), 其余替换为 "[Old tool result content cleared]"
  - 计算 tokens_saved
"""

from __future__ import annotations

import time

CLEARED_STUB = "[Old tool result content cleared]"
DEFAULT_GAP_MINUTES = 60
DEFAULT_KEEP_RECENT = 8


# 不可重建的工具 (Write/Edit) 不在内 — 防止误删导致模型失忆。
COMPACTABLE_TOOLS = frozenset({
    "Read", "Grep", "Glob",
    "Bash", "Cmd", "PowerShell",
    "WebSearch", "WebFetch",
})


def microcompact_messages(
    messages: list[dict],
    min_gap_minutes: int = DEFAULT_GAP_MINUTES,
    keep_recent: int = DEFAULT_KEEP_RECENT,
) -> tuple[list[dict], int]:
    """Microcompact — 时间基准清除旧 tool result。

    Args:
        messages: 完整对话历史
        min_gap_minutes: 触发间隔阈值(分钟)
        keep_recent: 保留最近 N 个 compactable tool results

    Returns:
        (cleaned_messages, tokens_saved)
    """

    # ① 检查触发条件: 最后一条 assistant message 距今是否超过阈值
    if not _should_trigger(messages, min_gap_minutes):
        return messages, 0

    # ② 收集所有 compactable tool_use_id (按时间顺序)
    compactable_ids = _collect_compactable_tool_ids(messages)
    if not compactable_ids:
        return messages, 0

    # ③ 分割: 保留最近 keep_recent 个, 清除其余
    keep_set = set(compactable_ids[-keep_recent:])
    clear_set = set(id for id in compactable_ids if id not in keep_set)

    if not clear_set:
        return messages, 0

    # ④ 替换清除
    tokens_saved = 0
    result = []
    for msg in messages:
        if msg.get("role") != "user":
            result.append(msg)
            continue

        if msg.get("type") == "tool_result":
            tool_use_id = str(msg.get("tool_use_id", ""))
            if tool_use_id in clear_set and msg.get("content") != CLEARED_STUB:
                old_len = len(str(msg.get("content", "")))
                tokens_saved += old_len // 4
                result.append({**msg, "content": CLEARED_STUB})
            else:
                result.append(msg)
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            result.append(msg)
            continue

        touched = False
        new_content = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id", "")
                if tid in clear_set and block.get("content") != CLEARED_STUB:
                    old_len = len(str(block.get("content", "")))
                    tokens_saved += old_len // 4
                    touched = True
                    new_content.append({**block, "content": CLEARED_STUB})
                else:
                    new_content.append(block)
            else:
                new_content.append(block)

        if touched:
            result.append({**msg, "content": new_content})
        else:
            result.append(msg)

    if tokens_saved > 0:
        print(f"[microcompact] time-based: cleared {len(clear_set)} tool results "
              f"~{tokens_saved} tokens, kept last {keep_recent}")

    return result, tokens_saved


def _should_trigger(messages: list[dict], min_gap_minutes: int) -> bool:
    """检查距离最后一次 assistant message 是否超过间隔阈值。"""
    # 从后往前找最后一条 assistant message
    last_assistant_ts = None
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            # 检查是否有 _timestamp 字段
            ts = msg.get("_timestamp")
            if ts is not None:
                last_assistant_ts = ts
                break

    if last_assistant_ts is None:
        return False

    gap = time.time() - last_assistant_ts
    return gap > min_gap_minutes * 60


def _collect_compactable_tool_ids(messages: list[dict]) -> list[str]:
    ""
    # 建 tool_use_id → tool_name 映射，避开重复扫 assistant 块
    id_to_name: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                bid = block.get("id", "")
                if bid:
                    id_to_name[bid] = block.get("name", "")

    ids = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        if msg.get("type") == "tool_result":
            tool_use_id = str(msg.get("tool_use_id", ""))
            tool_name = id_to_name.get(tool_use_id, "")
            if tool_use_id and tool_name in COMPACTABLE_TOOLS:
                ids.append(tool_use_id)
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id", "")
                if not tid:
                    continue
                tname = id_to_name.get(tid, "")
                # 未知 tool_name (无对应 tool_use 块) 默认不清理，保守
                if tname and tname in COMPACTABLE_TOOLS:
                    ids.append(tid)
    return ids
