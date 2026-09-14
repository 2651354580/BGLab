"""Memory prompt 构建 — 对齐 memdir/memdir.ts loadMemoryPrompt() + buildMemoryLines()。

两部分:
  1. System prompt: 注入行为指南 + MEMORY.md 索引（15 个 section）
  2. Extraction prompt: 对齐 services/extractMemories/prompts.ts buildExtractAutoOnlyPrompt()
"""

from __future__ import annotations

from pathlib import Path

from bglab.memory.memdir import get_memory_dir, load_entrypoint
from bglab.memory.memory_types import (
    TYPES_SECTION,
    WHAT_NOT_TO_SAVE,
    WHEN_TO_ACCESS,
    TRUSTING_RECALL,
    SAVING_TWO_STEP,
    FRONTMATTER_EXAMPLE,
)

DIR_EXISTS_GUIDANCE = (
    "This directory already exists and you can write to it directly with the Write tool "
    "(do not run mkdir or check for its existence)."
)


# ═══════════════════════════════════════════════════════════
# Part 1: System Prompt — 对齐 loadMemoryPrompt + buildMemoryLines
# ═══════════════════════════════════════════════════════════

async def load_memory_prompt(cwd: str | None = None) -> str | None:
    """加载 auto memory 的 system prompt 部分（仅行为指南，不含索引）。对齐 loadMemoryPrompt()。"""
    mem_dir = get_memory_dir(cwd)
    if not mem_dir.exists():
        mem_dir.mkdir(parents=True, exist_ok=True)

    return build_memory_lines(mem_dir)


def build_memory_lines(memory_dir: Path) -> str:
    ""
    parts = [
        "# auto memory",
        "",
        f"You have a persistent, file-based memory system at `{memory_dir}`. {DIR_EXISTS_GUIDANCE}",
        "",
        "You should build up this memory system over time so that future conversations can have a complete picture of who the user is, how they'd like to collaborate with you, what behaviors to avoid or repeat, and the context behind the work the user gives you.",
        "",
        "If the user explicitly asks you to remember something, save it immediately as whichever type fits best. If they ask you to forget something, find and remove the relevant entry.",
        "",
        TYPES_SECTION,
        "",
        WHAT_NOT_TO_SAVE,
        "",
        SAVING_TWO_STEP,
        "",
        WHEN_TO_ACCESS,
        "",
        TRUSTING_RECALL,
        "",
        "## Memory and other forms of persistence",
        "Memory is one of several persistence mechanisms available to you as you assist the user in a given conversation. The distinction is often that memory can be recalled in future conversations and should not be used for persisting information that is only useful within the scope of the current conversation.",
        "- When to use or update a plan instead of memory: If you are about to start a non-trivial implementation task and would like to reach alignment with the user on your approach you should use a Plan rather than saving this information to memory. Similarly, if you already have a plan within the conversation and you have changed your approach persist that change by updating the plan rather than saving a memory.",
        "- When to use or update tasks instead of memory: When you need to break your work in current conversation into discrete steps or keep track of your progress use tasks instead of saving to memory. Tasks are great for persisting information about the work that needs to be done in the current conversation, but memory should be reserved for information that will be useful in future conversations.",
    ]

    return "\n".join(parts)


# ═══════════════════════════════════════════════════════════
# Part 2: Extraction Prompt — 对齐 buildExtractAutoOnlyPrompt
# ═══════════════════════════════════════════════════════════

def build_extraction_prompt(
    new_message_count: int,
    existing_memories: str,
    skip_index: bool = False,
) -> str:
    """构建记忆提取的 prompt — 对齐 buildExtractAutoOnlyPrompt()。

    Args:
        new_message_count: 自上次提取后的新消息数
        existing_memories: 现有 memory 文件清单 (filename: description)
        skip_index: 是否跳过 MEMORY.md 索引更新步骤
    """
    how_to_save = _make_how_to_save(skip_index)

    return "\n".join([
        _extraction_opener(new_message_count, existing_memories),
        "",
        "If the user explicitly asked you to remember something, save it immediately as whichever type fits best. If they asked you to forget something, find and remove the relevant entry.",
        "",
        TYPES_SECTION,
        WHAT_NOT_TO_SAVE,
        "",
        how_to_save,
    ])


def _extraction_opener(new_message_count: int, existing_memories: str) -> str:
    """对齐 prompts.ts opener()。"""
    manifest = ""
    if existing_memories:
        manifest = (
            f"\n\n## Existing memory files\n\n{existing_memories}"
            "\n\nCheck this list before writing — update an existing file "
            "rather than creating a duplicate."
        )

    return "\n".join([
        f"You are now acting as the memory extraction subagent. Analyze the most recent "
        f"~{new_message_count} messages above and use them to update your persistent memory systems.",
        "",
        "Available tools: Read, Grep, Glob, read-only Bash (ls/find/cat/stat/wc/head/tail), "
        "and Write/Edit for paths inside the memory directory only. Bash rm is not permitted. "
        "All other tools will be denied.",
        "",
        "You have a limited turn budget. Edit requires a prior Read of the same file, so the "
        "efficient strategy is: turn 1 — issue all Read calls in parallel for every file you "
        "might update; turn 2 — issue all Write/Edit calls in parallel. Do not interleave reads "
        "and writes across multiple turns.",
        "",
        f"You MUST only use content from the last ~{new_message_count} messages to update your "
        "persistent memories. Do not waste any turns attempting to investigate or verify that "
        "content further — no grepping source files, no reading code to confirm a pattern "
        "exists, no git commands.",
        manifest,
    ])


def _make_how_to_save(skip_index: bool) -> str:
    """对齐 prompts.ts how_to_save 分支。"""
    if skip_index:
        return "\n".join([
            "## How to save memories",
            "",
            "Write each memory to its own file (e.g., `user_role.md`, `feedback_testing.md`) "
            "using this frontmatter format:",
            "",
            FRONTMATTER_EXAMPLE,
            "",
            "- Organize memory semantically by topic, not chronologically",
            "- Update or remove memories that turn out to be wrong or outdated",
            "- Do not write duplicate memories. First check if there is an existing memory "
            "you can update before writing a new one.",
        ])
    else:
        return "\n".join([
            "## How to save memories",
            "",
            "Saving a memory is a two-step process:",
            "",
            "**Step 1** — write the memory to its own file (e.g., `user_role.md`, "
            "`feedback_testing.md`) using this frontmatter format:",
            "",
            FRONTMATTER_EXAMPLE,
            "",
            "**Step 2** — add a pointer to that file in `MEMORY.md`. `MEMORY.md` is an index, "
            "not a memory — each entry should be one line, under ~150 characters: "
            "`- [Title](file.md) — one-line hook`. It has no frontmatter. Never write memory "
            "content directly into `MEMORY.md`.",
            "",
            "- `MEMORY.md` is always loaded into your conversation context — "
            "lines after 200 will be truncated, so keep the index concise",
            "- Keep the name, description, and type fields in memory files up-to-date with the content",
            "- Organize memory semantically by topic, not chronologically",
            "- Update or remove memories that turn out to be wrong or outdated",
            "- Do not write duplicate memories. First check if there is an existing memory "
            "you can update before writing a new one.",
        ])
