""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import os
import threading

from bglab.tools.base import Tool
from bglab.tools.read import ReadTool
from bglab.tools.grep import GrepTool
from bglab.tools.glob import GlobTool
from bglab.tools.bash import BashTool
from bglab.tools.write import WriteTool
from bglab.tools.edit import EditTool
from bglab.tools.web_fetch import WebFetchTool
from bglab.tools.web_search import WebSearchTool
from bglab.engine.error_policy import AGENT_RUNTIME_FAILURE


logger = logging.getLogger("bglab.tools.agent")


# ── Agent context tracking (fork recursion guard) ──

# In nano, use thread-local since each sub-agent runs in its own thread.

_agent_context = threading.local()


def _is_in_agent_context() -> bool:
    ""
    return getattr(_agent_context, "active", False)


def _set_agent_context(active: bool) -> None:
    _agent_context.active = active


def _bridge_parent_ask_callback(callback, parent_loop):
    """Run a child permission prompt on the parent TUI event loop."""
    if callback is None or parent_loop is None:
        return None

    async def ask(tool_name: str, tool_input: dict, reason: str):
        async def invoke():
            result = callback(tool_name, tool_input, reason)
            if inspect.isawaitable(result):
                result = await result
            return result

        future = asyncio.run_coroutine_threadsafe(invoke(), parent_loop)
        return await asyncio.wrap_future(future)

    return ask


# ── bilingual helper ──

def _bilingual(zh: str, en: str) -> str:
    """返回 '中文 (English)' 格式 — 测试用英文关键词，用户看中文。"""
    return f"{zh} ({en})"



# prompt 保留英文（LLM 指令语言），description/whenToUse 用中文

BUILT_IN_AGENTS = {
    "general-purpose": {
        "tools": ["*"],
        "whenToUse": "通用子 Agent：用于研究复杂问题、搜索代码和执行多步骤任务。当你不确定第一次搜索能找到正确结果时，用它来帮你搜索。",
        "system_prompt": """You are a general-purpose sub-agent. The main agent has delegated a specific task to you.

You have access to ALL tools: Read, Write, Edit, Bash, Grep, Glob, WebSearch, WebFetch. Solve the task autonomously.

Your response will be returned to the main agent as a tool result. Be concise and direct — state your findings clearly, no conversational filler.

DO NOT use the Agent tool yourself. DO NOT call TaskCreate/TaskUpdate to track your own work. Just solve the task and report back.""",
    },
    "Explore": {
        "tools": ["Read", "Grep", "Glob", "WebFetch", "WebSearch", "Bash"],
        "disallowedTools": ["Agent", "ExitPlanMode", "Edit", "Write", "NotebookEdit"],
        "whenToUse": "快速只读搜索 Agent：用于定位代码。按 pattern 查找文件(如 \"src/components/**/*.tsx\")、grep 搜索符号或关键词(如 \"API endpoints\")、回答\"X 定义在哪里 / 哪些文件引用了 Y\"。不要用于代码审查、设计文档审核、跨文件一致性检查或开放性分析——它只读取摘要而非完整文件。调用时指定搜索广度: \"quick\" 快速定位, \"medium\" 中等探索, \"very thorough\" 全面搜索。",
        "system_prompt": """You are a code-exploration sub-agent. You can Read files, Grep for patterns, Glob for file lists, and run read-only Bash commands.

=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===
This is a READ-ONLY exploration task. You are STRICTLY PROHIBITED from:
- Creating new files (no Write, touch, or file creation of any kind)
- Modifying existing files (no Edit operations)
- Deleting files (no rm or deletion)
- Moving or copying files (no mv or cp)
- Creating temporary files anywhere, including /tmp
- Using redirect operators (>, >>, |) or heredocs to write to files
- Running ANY commands that change system state

Your role is EXCLUSIVELY to search and analyze existing code. You do NOT have access to file editing tools - attempting to edit files will fail.

Your strengths:
- Rapidly finding files using glob patterns
- Searching code and text with powerful regex patterns
- Reading and analyzing file contents

Guidelines:
- Use Glob for broad file pattern matching
- Use Grep for searching file contents with regex
- Use Read when you know the specific file path
- Use Bash ONLY for read-only operations (ls, git status, git log, git diff, find, cat, head, tail)
- NEVER use Bash for: mkdir, touch, rm, cp, mv, git add, git commit, npm install, pip install, or any file creation/modification
- Adapt your search approach based on the thoroughness level specified by the caller
- Communicate your final report directly as a regular message - do NOT attempt to create files

NOTE: You are meant to be a fast agent that returns output as quickly as possible.
Make efficient use of the tools you have: spawn multiple parallel tool calls for grepping and reading files.

Complete the user's search request efficiently and report your findings clearly.""",
    },
    "Plan": {
        "tools": ["Read", "Grep", "Glob", "WebFetch", "WebSearch", "Bash"],
        "disallowedTools": ["Agent", "ExitPlanMode", "Edit", "Write", "NotebookEdit"],
        "whenToUse": "软件架构 Agent：用于设计实现方案。当需要规划任务的实现策略时使用。返回步骤式计划，标识关键文件，考虑架构权衡。",
        "system_prompt": """You are a software architect and planning specialist for BGLab. Your role is to explore the codebase and design implementation plans.

=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===
This is a READ-ONLY planning task. You are STRICTLY PROHIBITED from:
- Creating new files (no Write, touch, or file creation of any kind)
- Modifying existing files (no Edit operations)
- Deleting files (no rm or deletion)
- Moving or copying files (no mv or cp)
- Creating temporary files anywhere, including /tmp
- Using redirect operators (>, >>, |) or heredocs to write to files
- Running ANY commands that change system state

Your role is EXCLUSIVELY to explore the codebase and design implementation plans.

## Your Process

1. **Understand Requirements**: Focus on the requirements provided and apply your assigned perspective throughout the design process.
2. **Explore Thoroughly**: Read files, find existing patterns and conventions, understand current architecture.
3. **Design Solution**: Create implementation approach, consider trade-offs and architectural decisions.
4. **Detail the Plan**: Provide step-by-step implementation strategy, identify dependencies and sequencing.

## Required Output

End your response with:

### Critical Files for Implementation
List 3-5 files most critical for implementing this plan:
- path/to/file1.ts
- path/to/file2.ts
- path/to/file3.ts

REMEMBER: You can ONLY explore and plan. You CANNOT and MUST NOT write, edit, or modify any files.""",
    },
    "bglab-guide": {
        "tools": ["Read", "Grep", "Glob", "WebFetch", "WebSearch"],
        "whenToUse": "当用户询问 BGLab 的功能、命令、模型配置、工具权限、对话或游戏存档用法时使用。",
        "system_prompt": """You are a BGLab documentation agent. Answer questions about BGLab features, configuration, and usage.

Read the available BGLab README and relevant implementation before describing a feature. For model-provider questions, consult that provider's official documentation. Do not assume that features from another application exist in BGLab.

Be accurate and cite specific configuration options. If you're unsure, say so rather than guessing.""",
    },
    "statusline-setup": {
        "tools": ["Read", "Edit"],
        "whenToUse": "用户要求修改项目中已有的状态栏配置时使用；先确认该项目确实支持对应设置。",
        "system_prompt": """You are a status-line configuration agent. Locate the application's existing status-line configuration and verify that the requested setting is supported before editing it. If no such configuration exists, explain that instead of inventing a setting.

Read the current settings file, then update the status line configuration as requested. Report what was changed.""",
    },
}


def _agent_registry(
    subagent_type: str,
    *,
    allow_destructive: bool = True,
):
    from bglab.tools.base import ToolRegistry
    from bglab.tools.base import Tool

    agent_def = BUILT_IN_AGENTS.get(subagent_type, BUILT_IN_AGENTS["general-purpose"])
    allowed = agent_def["tools"]
    denied = set(agent_def.get("disallowedTools", []))

    # All candidate tools
    all_tools = {
        "Read": ReadTool, "Grep": GrepTool, "Glob": GlobTool,
        "Bash": BashTool, "Write": WriteTool, "Edit": EditTool,
        "WebFetch": WebFetchTool, "WebSearch": WebSearchTool,
        "Agent": None,  # blocked by design
        "ExitPlanMode": None,
        "NotebookEdit": None,
    }

    def _include(tool: Tool | None, name: str) -> bool:
        if tool is None:
            return False
        if not allow_destructive and not tool.permission_spec.read_only:
            return False
        if name in denied:
            return False
        if "*" in allowed:
            return True
        return name in allowed

    reg = ToolRegistry()
    for name, tool in all_tools.items():
        if _include(tool, name):
            reg.register(tool)

    return reg


async def _run_fresh_agent(
    prompt: str,
    subagent_type: str,
    model: str,
    max_turns: int,
    cwd: str,
    *,
    permission_context=None,
    ask_callback=None,
    permission_mode: str = "default",
) -> str:
    """Fresh coding subagent: zero context, agent-specific system prompt."""
    from bglab.engine.query import query_loop

    agent_def = BUILT_IN_AGENTS.get(subagent_type, BUILT_IN_AGENTS["general-purpose"])
    reg = _agent_registry(
        subagent_type,
        allow_destructive=permission_context is not None,
    )
    from bglab.engine.deps import QueryDeps
    from bglab.llm.client import call_model as _call_model
    from bglab.compaction.autocompact import CompactTracker
    from bglab.loop_detector import LoopDetector
    from bglab.hooks.state import StopHooksState

    deps = QueryDeps(
        call_model=_call_model,
        compact_tracker=CompactTracker(),
        loop_detector=LoopDetector(),
        stop_hooks_state=StopHooksState(),
    )
    system_prompt = agent_def["system_prompt"]
    mt = max_turns

    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
    collected = []
    try:
        async for event in query_loop(
            messages=messages,
            system_prompt=system_prompt,
            tools=reg.to_definitions(),
            handlers=reg.to_handler_dict(),
            deps=deps,
            model=model,
            max_turns=mt,
            permission_mode=permission_mode,
            permission_context=permission_context,
            ask_callback=ask_callback,
            cwd=cwd,
        ):
            if event.text:
                collected.append(event.text)
            if event.type.value == "error":
                logger.warning("Child Agent returned an internal error event")
                collected.append(f"\n[Error: {AGENT_RUNTIME_FAILURE}]\n")
    except (asyncio.CancelledError, concurrent.futures.CancelledError):
        raise
    except Exception:
        logger.exception("Fresh Agent failed")
        raise

    return "".join(collected).strip()


async def _run_fork_agent(
    prompt: str,
    parent_messages: list[dict],
    parent_system_prompt: str,
    model: str,
    max_turns: int,
    cwd: str,
    *,
    permission_context=None,
    ask_callback=None,
    permission_mode: str = "default",
) -> str:
    ""
    from bglab.engine.query import query_loop
    from bglab.tools.base import ToolRegistry
    from bglab.tools import (
        ReadTool, WriteTool, EditTool, BashTool, GrepTool, GlobTool,
    )
    from bglab.engine.deps import QueryDeps
    from bglab.llm.client import call_model as _call_model
    from bglab.compaction.autocompact import CompactTracker
    from bglab.loop_detector import LoopDetector
    from bglab.hooks.state import StopHooksState

    reg = ToolRegistry()
    child_tools = [ReadTool, WriteTool, EditTool, BashTool, GrepTool, GlobTool]
    if permission_context is None:
        child_tools = [
            tool for tool in child_tools
            if tool.permission_spec.read_only
        ]
    for t in child_tools:
        reg.register(t)

    deps = QueryDeps(
        call_model=_call_model,
        compact_tracker=CompactTracker(),
        loop_detector=LoopDetector(),
        stop_hooks_state=StopHooksState(),
    )

    # Fork inherits parent messages, skipping _is_meta (they're parent's injections)
    messages = [m for m in parent_messages if not m.get("_is_meta")]
    messages.append({"role": "user", "content": [{"type": "text", "text": prompt}]})

    collected = []
    try:
        async for event in query_loop(
            messages=messages,
            system_prompt=parent_system_prompt,
            tools=reg.to_definitions(),
            handlers=reg.to_handler_dict(),
            deps=deps,
            model=model,
            max_turns=max_turns,
            permission_mode=permission_mode,
            permission_context=permission_context,
            ask_callback=ask_callback,
            cwd=cwd,
        ):
            if event.text:
                collected.append(event.text)
            if event.type.value == "error":
                logger.warning("Forked Agent returned an internal error event")
                collected.append(f"\n[Error: {AGENT_RUNTIME_FAILURE}]\n")
    except (asyncio.CancelledError, concurrent.futures.CancelledError):
        raise
    except Exception:
        logger.exception("Forked Agent failed")
        raise

    return "".join(collected).strip()


def agent(args: dict) -> str:
    """Launch a sub-agent as defined by the main agent.

    Fork mode (no subagent_type): inherits full parent context (meta-filtered).
    Fresh mode (subagent_type): zero-context, agent-specific tools.
    Background: daemon thread, fire-and-forget, result as notification next turn.
    """
    
    if _is_in_agent_context():
        return _bilingual(
            "Agent 上下文中不允许递归创建子 Agent。请直接用你的工具完成任务。",
            "Agent is not available inside a sub-agent. Complete your task directly using your tools.",
        )

    description = args.get("description", "No description")
    prompt = args.get("prompt", "")
    subagent_type = args.get("subagent_type", "")
    run_in_background = args.get("run_in_background", False)
    max_turns = args.get("max_turns", 25)
    model = args.get("_parent_model") or "deepseek-chat"
    cwd = args.get("_cwd") or os.getcwd()

    # Parent context for fork mode — injected by query.py before handler call
    parent_messages = args.get("_parent_messages") or []
    parent_system_prompt = args.get("_parent_system_prompt") or ""
    permission_context = args.get("_permission_context")
    ask_callback = args.get("_ask_callback")
    parent_event_loop = args.get("_parent_event_loop")
    child_ask_callback = _bridge_parent_ask_callback(
        ask_callback,
        parent_event_loop,
    )
    permission_mode = args.get("_permission_mode") or "default"

    if not prompt:
        return _bilingual("需要提供 prompt 参数", "prompt is required")

    
    _type_map = {
        "claude": "general-purpose",  
    }
    effective_type = _type_map.get(subagent_type, subagent_type)

    mode = "fork" if not effective_type else f"fresh({effective_type})"
    TAG = f"[agent:{mode}]"

    def _run_sync():
        """Run agent coroutine in a dedicated event loop with proper cleanup.

        Uses explicit loop management instead of asyncio.run() so httpx
        connection pools get a chance to drain pending cleanup tasks before
        the loop is destroyed. This prevents 'Event loop is closed' warnings.
        """
        if effective_type:
            coro = _run_fresh_agent(
                prompt,
                effective_type,
                model,
                max_turns,
                cwd,
                permission_context=permission_context,
                ask_callback=child_ask_callback,
                permission_mode=permission_mode,
            )
        else:
            coro = _run_fork_agent(
                prompt,
                parent_messages,
                parent_system_prompt,
                model,
                max_turns,
                cwd,
                permission_context=permission_context,
                ask_callback=child_ask_callback,
                permission_mode=permission_mode,
            )

        _set_agent_context(True)
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(coro)
        finally:
            _set_agent_context(False)
            # Drain pending cleanup tasks (httpx connection pool close, etc.)
            pending = asyncio.all_tasks(loop)
            if pending:
                try:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception:
                    pass
            loop.close()
        return result

    if run_in_background:
        def _bg():
            from bglab.tools.bg_agent import enqueue_notification
            try:
                result = _run_sync()
            except BaseException as error:
                if isinstance(error, (
                    asyncio.CancelledError,
                    concurrent.futures.CancelledError,
                    SystemExit,
                    KeyboardInterrupt,
                    GeneratorExit,
                )):
                    raise
                logger.exception("Background Agent failed")
                result = AGENT_RUNTIME_FAILURE
            status = "failed" if "Error:" in result or "<subagent-error>" in result or "<fork-error>" in result else "completed"
            if result == AGENT_RUNTIME_FAILURE:
                status = "failed"
            enqueue_notification(mode, status, result)

        t = threading.Thread(target=_bg, daemon=True)
        t.start()
        return (
            f"{TAG} {_bilingual('已在后台启动', 'Launched in background')}\n"
            f"{_bilingual('模式', 'Mode')}: {mode}\n"
            f"{_bilingual('任务', 'Task')}: {description}\n"
            f"maxTurns: {max_turns}\n"
            f"{_bilingual('下轮对话开始时将收到通知', 'Result will be delivered as a notification at the start of your next turn')}."
        )

    # Foreground: called from asyncio.to_thread in query.py — already in a worker thread.
    # Run _run_sync() directly (no nested ThreadPoolExecutor).
    try:
        result = _run_sync()
    except (asyncio.CancelledError, concurrent.futures.CancelledError):
        raise
    except Exception as error:
        logger.exception("Foreground Agent failed")
        raise RuntimeError(AGENT_RUNTIME_FAILURE) from error

    return f"{TAG} {result}"



# 中文适配版：保留工具名称和代码示例的英文，说明性文字翻译为中文

AGENT_PROMPT = """启动一个新的子 Agent 来自主处理复杂的多步骤任务。

Agent 工具启动专门化的子 Agent（子进程），它们能自主处理复杂任务。每种 Agent 类型拥有特定的能力和可用工具。

可用 Agent 类型及其工具权限：
- general-purpose: 通用子 Agent，用于研究复杂问题、搜索代码和执行多步骤任务。当你不确定第一次搜索能否找到正确结果时，用它来帮你搜索。(Tools: All tools)
- Explore: 快速只读搜索 Agent，用于定位代码。按 pattern 查找文件、grep 搜索符号或关键词、回答"X在哪里定义/哪些文件引用了Y"。不要用于代码审查、设计审核或开放性分析。调用时指定搜索广度: "quick" 快速, "medium" 中等, "very thorough" 全面。(Tools: Read, Grep, Glob, Bash)
- Plan: 软件架构 Agent，用于设计实现方案。当需要规划任务实现策略时使用。返回步骤式计划，标识关键文件，考虑架构权衡。(Tools: Read, Grep, Glob, Bash)
- bglab-guide: 回答 BGLab 功能、命令、模型配置、权限和存档用法；以当前文档和代码为依据。(Tools: Read, Grep, Glob, WebFetch, WebSearch)
- statusline-setup: 修改项目中实际存在且支持的状态栏配置。(Tools: Read, Edit)

不要使用 Agent 工具的时机:
- 如果要读取某个文件的路径，请使用 Read 工具
- 如果要搜索某个类定义或关键词，请使用 Grep 工具
- 与以上 Agent 描述无关的其他任务

使用说明:
- 尽可能同时启动多个 Agent，以最大化性能；使用单条消息中的多个工具调用来实现
- Agent 完成后会返回一条消息。Agent 返回的结果用户不可见。要向用户展示结果，应发送一条文本消息给用户，包含结果的简洁摘要
- 每次 Agent 调用是独立的。当前实现不支持向运行中的 Agent 继续发送消息，因此 prompt 应包含完成任务所需的上下文，并明确最终报告内容
- 有"访问当前上下文"能力的 Agent 可以看到工具调用前的全部对话历史。使用这些 Agent 时，可以编写简洁的 prompt 引用之前的上下文（例如："调查上面 README 测试运行中的错误"），而无需重复信息
- Agent 的输出通常可以信任；使用输出来指导后续操作，而不是双重检查 Agent 的结果（例如，不要重新读取 Agent 已经读过的文件）
- 如果 Agent 描述提到它应该被主动使用，那么你应该尽量在用户要求之前使用它
- 如果用户指定要"并行"运行 Agent，你必须在一条消息中发送多个 Agent 工具调用

## Fork 模式

省略 `subagent_type` 来 fork 自己 — fork 会继承你的对话上下文与系统提示，并获得受当前权限约束的 coding 工具子集。适用于中间工具输出不值得保留在主上下文中的任务。Fork 继承当前模型并独立运行。

## Fresh Agent 模式

指定 `subagent_type` 来启动一个零上下文的全新 Agent。Agent 一无所知 — 必须在 prompt 中包含所有相关上下文。

## 编写 prompt

像对刚进门的聪明同事一样 briefing — 它没看过这段对话，不知道你尝试过什么，不理解为什么这个任务重要。
- 解释你想完成什么以及为什么
- 描述你已经学到或排除的内容
- 提供足够的上下文让 Agent 能做判断
- 如果需要简短回复，说明（"200 字以内报告"）
- 查找类任务：提供具体命令。调查类任务：提供问题

简洁的命令式 prompt 会产生浅层、泛泛的工作。

**永远不要委托理解。** 不要写"基于你的发现修复这个 bug"或"基于研究来实现它"。写能证明你已经理解的 prompt：包含文件路径、行号、具体要改什么。

## 示例

<example>
user: "What's left on this branch before we can ship?"
assistant: <commentary>A survey question across git state, tests, and config. I'll delegate it and ask for a short report so the raw command output stays out of my context.</commentary>
Agent({description: "Branch ship-readiness audit",
  prompt: "Audit what's left before this branch can ship. Check: uncommitted changes, commits ahead of main, whether tests exist, whether the GrowthBook gate is wired up, whether CI-relevant files changed. Report a punch list — done vs. missing. Under 200 words."})
</example>

<example>
user: "Can you get a second opinion on whether this migration is safe?"
assistant: <commentary>I'll ask the code-reviewer agent — it won't see my analysis, so it can give an independent read.</commentary>
Agent({description: "Independent migration review",
  subagent_type: "Explore",
  prompt: "Review migration 0042_user_schema.sql for safety. Context: we're adding a NOT NULL column to a 50M-row table. Existing rows get a backfill default. I want a second opinion on whether the backfill approach is safe under concurrent writes — I've checked locking behavior but want independent verification. Report: is this safe, and if not, what specifically breaks?"})
</example>

<example>
user: "Can you get a second opinion on whether this migration is safe?"
assistant: <commentary>The agent starts with no context from this conversation, so the prompt briefs it: what to assess, the relevant background, and what form the answer should take.</commentary>
Agent({description: "Independent migration review",
  subagent_type: "Explore",
  prompt: "Review migration 0042_user_schema.sql for safety..."})
</example>"""


AgentTool = Tool(
    name="Agent",
    searchHint="launch sub-agent for multi-step tasks",
    description="启动子 Agent 来自主处理复杂的多步骤任务。",
    prompt=AGENT_PROMPT,
    parameters={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "任务的简短描述（3-5 个词）",
            },
            "prompt": {
                "type": "string",
                "description": "要 Agent 执行的任务描述",
            },
            "subagent_type": {
                "type": "string",
                "enum": ["general-purpose", "Explore", "Plan", "bglab-guide", "statusline-setup"],
                "description": "要使用的专用 Agent 类型。省略则以 fork 模式运行（继承当前上下文）。",
            },
            "run_in_background": {
                "type": "boolean",
                "description": "设为 true 在后台运行此 Agent。完成后会收到通知。",
                "default": False,
            },
        },
        "required": ["description", "prompt"],
        "additionalProperties": False,
    },
    call=agent,
    is_read_only=False,
    auto_allow=True,
    plan_allowed=True,
)
