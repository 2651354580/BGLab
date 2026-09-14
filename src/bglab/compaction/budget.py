"""Budget Reduction — 对齐 toolResultStorage.ts applyToolResultBudget()。

单条 tool_result 如果有多个 chunk，合并它们并在超限时截断。
和源码的区别：源码还做了磁盘持久化（ContentReplacementState），我们只做截断。

默认预算:
  - 单条 tool_result: 2000 chars
  - 多工具总预算: 50000 chars
"""

from __future__ import annotations

# 对齐源码 MAX_TOOL_RESULTS_PER_MESSAGE_CHARS
MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 50_000


def apply_tool_result_budget(
    messages: list[dict],
    max_per_result: int = 2000,
    on_replace: callable | None = None,
) -> list[dict]:
    ""
    result = list(messages)
    for i, msg in enumerate(result):
        if msg.get("role") != "user":
            continue

        if msg.get("type") == "tool_result":
            text = str(msg.get("content", ""))
            if len(text) > max_per_result:
                tool_use_id = str(msg.get("tool_use_id", ""))
                if on_replace:
                    on_replace(tool_use_id, text)
                result[i] = {
                    **msg,
                    "content": (
                        text[:max_per_result]
                        + f"\n... [truncated {len(text) - max_per_result} chars]"
                    ),
                }
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            continue

        tool_texts = []
        other_blocks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                text = block.get("content", "")
                tool_texts.append((block.get("tool_use_id", ""), text))
            else:
                other_blocks.append(block)

        if not tool_texts:
            continue

        truncated = []
        for tool_use_id, text in tool_texts:
            if len(text) > max_per_result:
                if on_replace:
                    on_replace(tool_use_id, text)
                text = (
                    text[:max_per_result]
                    + f"\n... [truncated {len(text) - max_per_result} chars]"
                )
            truncated.append((tool_use_id, text))

        new_content = list(other_blocks)
        for tool_use_id, text in truncated:
            new_content.append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": text,
            })

        result[i] = {**msg, "content": new_content}

    return result
