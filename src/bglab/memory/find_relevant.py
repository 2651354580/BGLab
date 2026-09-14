"""Flash 模型选记忆 — 对齐 memdir/findRelevantMemories.ts。

用 sideQuery (小模型) 从 memory 目录中选最多 5 个最相关的 memory 文件,
注入为 attachment。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from bglab.memory.memdir import (
    get_memory_dir,
    scan_memory_files,
    read_memories_for_surfacing,
)


SELECT_MEMORIES_SYSTEM_PROMPT = """You are selecting memories that will be useful to the current BGLab Game Player as it processes the current turn. You will be given the current-turn query and a list of available memory files with their filenames and descriptions.

Return a list of filenames for the memories that will clearly be useful to the current BGLab Game Player (up to 5). Only include memories that you are certain will be helpful based on their name and description.
- If you are unsure if a memory will be useful for the current turn, then do not include it in your list. Be selective and discerning.
- If there are no memories in the list that would clearly be useful, feel free to return an empty list."""


async def get_relevant_memory_attachments(
    user_input: str,
    cwd: str | None = None,
    already_surfaced: set[str] | None = None,
    *,
    mem_dir: Path | None = None,
    max_selected: int = 5,
    max_bytes_per_memory: int = 4_000,
    max_lines_per_memory: int = 200,
    allowed_statuses: set[str] | None = None,
    model: str = "deepseek-chat",
) -> list[dict]:
    """获取相关的 memory 文件作为 attachment。

    对齐 getRelevantMemoryAttachments() → findRelevantMemories() 流程:
      1. 扫描 memory 目录
      2. Flash 模型选 5 个
      3. 读取全文
      4. 返回 attachment 格式

    mem_dir 不为 None 时直接使用该目录 (game mode 用 get_game_memory_dir)。
    Returns: [{type: 'relevant_memories', memories: [...]}] 或 []
    """
    if mem_dir is None:
        mem_dir = get_memory_dir(cwd)

    # 扫描
    memories = scan_memory_files(mem_dir)
    if allowed_statuses is not None:
        memories = [m for m in memories if m.get("status") in allowed_statuses]
    if not memories:
        return []

    # 过滤已展示过的
    if already_surfaced:
        memories = [m for m in memories if m["filePath"] not in already_surfaced]
        if not memories:
            return []

    # Flash 模型选 5 个
    selected = await _select_relevant(
        user_input,
        memories,
        max_selected=max_selected,
        model=model,
    )
    if not selected:
        return []

    # 读取全文
    full = read_memories_for_surfacing(
        selected,
        max_bytes=max_bytes_per_memory,
        max_lines=max_lines_per_memory,
    )
    if not full:
        return []

    return [{
        "type": "memory_attachment",
        "memories": full,
    }]


async def _select_relevant(
    user_input: str,
    memories: list[dict],
    *,
    max_selected: int = 5,
    model: str = "deepseek-chat",
) -> list[dict]:
    """Flash 模型从候选 memory 中选 5 个。对齐 selectRelevantMemories()。

    用 LLM 做 relevance scoring: 给每个 memory 的名字+描述, 选最多 5 个最相关的。
    """
    valid_names = {m["filename"] for m in memories}

    # 构建 manifest
    manifest_lines = []
    for m in memories:
        manifest_lines.append(f"- {m['filename']}: {m['description'] or '(no description)'}")
    manifest = "\n".join(manifest_lines)

    try:
        from bglab.llm.client import complete_text
        text = await complete_text(
            model=model,
            system_prompt=SELECT_MEMORIES_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": (
                    f"Query: {user_input[:500]}\n\n"
                    f"Available memories:\n{manifest}\n\n"
                    f"Return a JSON object with key 'selected_memories' containing an array of filenames."
                )},
            ],
            max_tokens=256,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        data = json.loads(text)
        if not isinstance(data, dict):
            logger.warning(f"[memory/select] unexpected response type: {type(data).__name__}")
            return []
        selected_names = data.get("selected_memories", [])
        if not isinstance(selected_names, list):
            return []

        # 过滤合法名称
        selected = [
            m for m in memories if m["filename"] in selected_names
        ][:max(0, max_selected)]
        return selected

    except Exception as e:
        print(f"[memory/select] failed: {e}")
        return []
