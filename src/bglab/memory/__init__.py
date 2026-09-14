"""Memory 系统 — 对齐 memdir/。

三部分注入:
  1. System prompt: load_memory_prompt() → 仅行为指南（不含索引）
  2. User context (messages[0]): MEMORY.md 索引 + CLAUDE.md 合并注入
  3. Attachment: get_relevant_memory_attachments() → Sonnet 选 5 个注入

持久化存储: ~/.bglab/memory/<sanitized_cwd>/
  - MEMORY.md (索引, 截断 200 行/25KB)
  - user_*, feedback_*, project_*, reference_*.md (memory 文件, frontmatter + 正文)
"""

from bglab.memory.memory_prompt import load_memory_prompt
from bglab.memory.find_relevant import get_relevant_memory_attachments
from bglab.memory.memdir import (
    get_memory_dir,
    get_game_memory_dir,
    scan_memory_files,
    write_memory_file,
    update_entrypoint,
)
from bglab.memory.memory_types import (
    MEMORY_TYPES,
    GAME_TYPE_SECTION,
    GAME_WHAT_NOT_TO_SAVE,
    GAME_SAVING_TWO_STEP,
    GAME_WHEN_TO_ACCESS,
    GAME_TRUSTING_RECALL,
)
from bglab.memory.consolidation_lock import (
    try_acquire_consolidation_lock,
    rollback_consolidation_lock,
    read_last_consolidated_at,
    release_consolidation_lock,
)

__all__ = [
    "load_memory_prompt",
    "get_relevant_memory_attachments",
    "get_memory_dir",
    "get_game_memory_dir",
    "scan_memory_files",
    "write_memory_file",
    "update_entrypoint",
    "MEMORY_TYPES",
    "GAME_TYPE_SECTION",
    "GAME_WHAT_NOT_TO_SAVE",
    "GAME_SAVING_TWO_STEP",
    "GAME_WHEN_TO_ACCESS",
    "GAME_TRUSTING_RECALL",
    "try_acquire_consolidation_lock",
    "rollback_consolidation_lock",
    "read_last_consolidated_at",
    "release_consolidation_lock",
]
