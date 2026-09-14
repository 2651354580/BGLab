""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from dataclasses import dataclass, field

from bglab.engine.error_policy import AGENT_RUNTIME_FAILURE

logger = logging.getLogger("bglab.fork")


@dataclass
class ForkResult:
    text: str = ""
    usage: dict = field(default_factory=dict)
    requests: list[dict] = field(default_factory=list)


async def run_forked_agent(
    prompt: str,
    *,
    system_prompt: str = "",
    tools: list | None = None,
    handlers: dict | None = None,
    model: str = "deepseek-chat",
    max_turns: int = 5,
    cwd: str = "",
    permission_mode: str = "bypass",
) -> ForkResult:
    ""
    from bglab.engine.query import query_loop
    from bglab.engine.deps import QueryDeps
    from bglab.engine.query_profiles import CODE_BACKGROUND_QUERY_PROFILE
    from bglab.llm.client import call_model
    from bglab.compaction.autocompact import CompactTracker
    from bglab.loop_detector import LoopDetector
    from bglab.hooks.state import StopHooksState

    deps = QueryDeps(
        call_model=call_model,
        compact_tracker=CompactTracker(),
        loop_detector=LoopDetector(),
        stop_hooks_state=StopHooksState(),
        query_profile=CODE_BACKGROUND_QUERY_PROFILE,
    )

    messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]

    collected: list[str] = []
    total_usage: dict = {}
    request_count = 0

    try:
        async for event in query_loop(
            messages=messages,
            system_prompt=system_prompt,
            tools=tools or [],
            handlers=handlers or {},
            deps=deps,
            model=model,
            max_turns=max_turns,
            permission_mode=permission_mode,
            cwd=cwd,
        ):
            if event.text:
                collected.append(event.text)
            if event.type.value == "done" and event.terminal is None:
                request_count += 1
            if hasattr(event, "terminal") and event.terminal:
                if event.terminal.total_usage:
                    total_usage = dict(event.terminal.total_usage)
    except (asyncio.CancelledError, concurrent.futures.CancelledError):
        raise
    except Exception:
        logger.exception("Forked agent failed")
        collected = [AGENT_RUNTIME_FAILURE]

    if request_count:
        total_usage["requests"] = request_count

    return ForkResult(
        text="".join(collected).strip(),
        usage=total_usage,
        requests=list(getattr(deps, "_request_records", [])),
    )


def build_memory_extraction_fork(
    prompt: str,
    memory_dir: str,
    system_prompt: str = "",
    model: str = "deepseek-chat",
    max_turns: int = 5,
) -> tuple[list, dict]:
    ""
    from bglab.tools.base import Tool, ToolRegistry
    from bglab.tools import (
        ReadTool, WriteTool, EditTool, GrepTool, GlobTool,
    )

    reg = ToolRegistry()
    reg.register(ReadTool)
    reg.register(GrepTool)
    reg.register(GlobTool)

    # 受限 Write/Edit — 仅允许在 memory_dir 内
    _mem_path = memory_dir

    # 受限 Write — 保持原名称，只包装 call
    _orig_write_call = WriteTool.call
    reg.register(Tool(
        name=WriteTool.name,
        description=WriteTool.description + " (restricted to memory directory)",
        prompt=WriteTool.prompt,
        parameters=WriteTool.parameters,
        call=lambda args, _oc=_orig_write_call, _mp=_mem_path: (
            f"Write denied: file_path must be inside {_mp}"
            if not str(args.get("file_path", "")).startswith(_mp)
            else _oc(args)
        ),
    ))

    # 受限 Edit — 保持原名称，只包装 call
    _orig_edit_call = EditTool.call
    reg.register(Tool(
        name=EditTool.name,
        description=EditTool.description + " (restricted to memory directory)",
        prompt=EditTool.prompt,
        parameters=EditTool.parameters,
        call=lambda args, _oc=_orig_edit_call, _mp=_mem_path: (
            f"Edit denied: file_path must be inside {_mp}"
            if not str(args.get("file_path", "")).startswith(_mp)
            else _oc(args)
        ),
    ))

    return reg.to_definitions(), reg.to_handler_dict()
