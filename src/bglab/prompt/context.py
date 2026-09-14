"""User/System Context 组装 — 对齐 context.ts。

User context: CLAUDE.md + currentDate → <system-reminder> 注入第一条 user message
System context: git status → append 到 system prompt 末尾

对齐关系：
  context.ts:155 getUserContext()
  context.ts:116 getSystemContext()
  api.ts:437 appendSystemContext()
  api.ts:449 prependUserContext()

关键设计（与源码对齐）：
  - user context 不放 system prompt — 放第一条 user message（保持 cache key 稳定）
  - system context 放 system prompt 末尾
  - DECERRED tools 放第二条 user message（isMeta=true）
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path


def load_claude_md(cwd: str | None = None) -> str | None:
    """加载项目根目录的 CLAUDE.md — 对齐 claudemd.ts:790 getMemoryFiles()。

    查找顺序（对齐源码）：
      1. {cwd}/CLAUDE.md
      2. {cwd}/.claude/CLAUDE.md

    Returns:
        文件内容，不存在返回 None。
    """
    cwd = cwd or os.getcwd()
    paths = [
        os.path.join(cwd, "CLAUDE.md"),
        os.path.join(cwd, ".claude", "CLAUDE.md"),
    ]
    for p in paths:
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    return f.read().strip()
            except Exception:
                pass
    return None


def get_git_status(cwd: str | None = None) -> dict[str, str]:
    """获取 git 状态 — 对齐 context.ts:36 getGitStatus()。

    Returns:
        包含 branch, status 等字段的 dict。非 git 目录返回空。
    """
    cwd = cwd or os.getcwd()
    result: dict[str, str] = {}

    # branch
    try:
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        if branch.returncode == 0 and branch.stdout.strip():
            result["branch"] = branch.stdout.strip()
    except Exception:
        pass

    # status
    try:
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=cwd, capture_output=True, text=True, timeout=5,
        )
        if status.returncode == 0:
            lines = status.stdout.strip().split("\n") if status.stdout.strip() else []
            result["modified_files"] = str(len(lines))
            if lines:
                result["status_preview"] = "\n".join(lines[:10])
    except Exception:
        pass

    return result


def _get_memory_dir_for_display(cwd: str | None = None) -> Path:
    """获取 memory 目录用于显示在 user context 中。"""
    from bglab.memory.memdir import get_memory_dir
    return get_memory_dir(cwd)


def _load_memory_entrypoint(cwd: str | None = None) -> str | None:
    """加载 MEMORY.md 索引内容，用于注入 user context。

    对齐 getMemoryFiles() → AutoMem → getClaudeMds() 流程。
    MEMORY.md 索引在 messages[0] user context，不在 system prompt。
    """
    from bglab.memory.memdir import get_memory_dir, load_entrypoint
    mem_dir = get_memory_dir(cwd)
    if not mem_dir.exists():
        return None
    return load_entrypoint(mem_dir)


def build_user_context(
    cwd: str | None = None,
    claude_md_content: str | None = None,
    *,
    memory_enabled: bool = True,
) -> dict[str, str]:
    """构建 user context — 对齐 context.ts:155 getUserContext()。

    User context 会被 prepend 到 messages 第一条（<system-reminder>），不放 system prompt。
    包含: CLAUDE.md + MEMORY.md 索引 + currentDate。

    Returns:
        dict like {"claudeMd": "...", "memoryIndex": "...", "currentDate": "...", "envInfo": "..."}
    """
    cwd = cwd or os.getcwd()

    ctx: dict[str, str] = {}
    claude_md = claude_md_content
    if claude_md is None:
        claude_md = load_claude_md(cwd)

    parts: list[str] = []
    if claude_md:
        parts.append(f"Contents of CLAUDE.md (project instructions):\n\n{claude_md}")
    mem_index = _load_memory_entrypoint(cwd) if memory_enabled else None
    if mem_index:
        mem_dir = str(_get_memory_dir_for_display(cwd))
        parts.append(f"Contents of {mem_dir}/MEMORY.md (user's auto-memory, persists across conversations):\n\n{mem_index}")
    if parts:
        ctx["claudeMd"] = "\n\n".join(parts)

    ctx["currentDate"] = f"Today's date is {datetime.now().strftime('%Y-%m-%d')}."

    return ctx


def build_system_context(cwd: str | None = None) -> dict[str, str]:
    """构建 system context — 对齐 context.ts:116 getSystemContext()。

    System context 会被 append 到 system prompt 末尾。

    Returns:
        dict like {"gitStatus": "..."}
    """
    git = get_git_status(cwd)
    ctx: dict[str, str] = {}
    if git:
        parts = []
        if "branch" in git:
            parts.append(f"Branch: {git['branch']}")
        if "modified_files" in git:
            parts.append(f"Modified files: {git['modified_files']}")
        if "status_preview" in git:
            parts.append(git["status_preview"])
        if parts:
            ctx["gitStatus"] = "\n".join(parts)
    return ctx


def build_user_context_head_values(
    user_context: dict[str, str] | None,
    deferred_tool_names: list[str] | None = None,
) -> list[tuple[str, str]]:
    ""
    values: list[tuple[str, str]] = []
    context = user_context or {}
    if context:
        # ``build_user_context`` emits ``claudeMd`` first.  Keep that contract
        # explicit even for callers supplying an ordinary dict, and render
        
        ordered_items = sorted(
            context.items(),
            key=lambda item: (0 if item[0] == "claudeMd" else 1),
        )
        ctx_lines = [
            f"# {'CLAUDE.md' if key == 'claudeMd' else key}\n{value}"
            for key, value in ordered_items
        ]
        preamble = (
            "As you answer the user's questions, you can use the following context:\n"
        )
        values.append(
            (
                "session_head",
                (
                    "<system-reminder>\n"
                    + preamble
                    + "\n".join(ctx_lines)
                    + "\n\n"
                    "IMPORTANT: this context may or may not be relevant to your tasks. "
                    "You should not respond to this context unless it is highly relevant to your task.\n"
                    "</system-reminder>"
                ),
            )
        )
    if deferred_tool_names:
        values.append(
            (
                "deferred_tools_head",
                (
                    "<available-deferred-tools>\n"
                    + "\n".join(deferred_tool_names)
                    + "\n</available-deferred-tools>"
                ),
            )
        )
    return values


def render_user_context_head_messages(
    user_context: dict[str, str] | None,
    deferred_tool_names: list[str] | None = None,
) -> list[dict]:
    """Render request-local user-context values as model-visible messages."""
    return [
        {
            "role": "user",
            "content": [{"type": "text", "text": text}],
            "_is_meta": True,
        }
        for _kind, text in build_user_context_head_values(
            user_context,
            deferred_tool_names,
        )
    ]


def prepend_user_context(
    messages: list[dict],
    user_context: dict[str, str],
    deferred_tool_names: list[str] | None = None,
) -> list[dict]:
    """把 user context + deferred tools 注入到 messages 最前面。

    对齐 api.ts:449 prependUserContext()。

    注入顺序（对齐源码）：
      1. Deferred tools → <available-deferred-tools> → 第二条位置
      2. User context → <system-reminder> → 第一条位置

    这两条都标记 _is_meta=True，UI 不显示，但模型能看到。
    不放 state.messages，每次 call_model 前动态拼接。
    """
    return [
        *render_user_context_head_messages(
            user_context,
            deferred_tool_names,
        ),
        *messages,
    ]


def append_system_context(
    system_prompt: list[str],
    system_context: dict[str, str],
) -> list[str]:
    """把 system context 追加到 system prompt 末尾。

    对齐 api.ts:437 appendSystemContext()。
    """
    if not system_context:
        return system_prompt

    ctx_parts = []
    for key, value in system_context.items():
        ctx_parts.append(f"{key}: {value}")

    return system_prompt + ["\n".join(ctx_parts)]
