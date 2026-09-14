""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
import time
import uuid
from typing import AsyncGenerator, Any

logger = logging.getLogger("bglab.engine.submit")

from bglab.llm.types import (
    LoopEvent,
    LoopEventType,
    StopReason,
    TerminalInfo,
    ToolDefinition,
)
from bglab.tools.base import ToolRegistry
from bglab.engine.query import query_loop
from bglab.engine.deps import QueryDeps
from bglab.engine.query_profiles import CODE_QUERY_PROFILE
from bglab.engine.prompt_profiles import resolve_prompt_bundle
from bglab.engine.trace import checkpoint
from bglab.compaction.autocompact import CompactTracker
from bglab.loop_detector import LoopDetector
from bglab.hooks.state import StopHooksState
from bglab.persistence import save_transcript


# ══════════════════════════════════════════════

# ══════════════════════════════════════════════

class QueryEngine:
    ""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        model: str = "deepseek-chat",
        max_turns: int = 50,
        permission_mode: str = "default",
        claude_md_path: str | None = None,
        system_prompt_append: str = "",
        deps: QueryDeps | None = None,
        session_id: str | None = None,
        ask_callback: Any = None,
        tool_registry: ToolRegistry | None = None,
        feature_flags: Any = None,
        language: str | None = None,
    ):
        self.cwd = cwd or os.getcwd()
        self.model = model
        self.max_turns = max_turns
        self.permission_mode = permission_mode
        self.claude_md_path = claude_md_path
        self.system_prompt_append = system_prompt_append
        self.deps = deps or _make_default_deps()
        self.session_id = session_id
        self.ask_callback = ask_callback or _default_ask_callback
        self.tool_registry = tool_registry
        self.feature_flags = feature_flags  # FeatureFlags instance
        self.language = language
        self.deps.feature_flags = feature_flags
        self.deps.current_cwd = self.cwd
        self._prompt_cache_namespace = session_id or uuid.uuid4().hex
        scratch_id = hashlib.sha256(
            self._prompt_cache_namespace.encode("utf-8"),
        ).hexdigest()[:16]
        self._scratchpad_dir = os.path.join(
            tempfile.gettempdir(),
            "bglab-scratchpad",
            scratch_id,
        )
        os.makedirs(self._scratchpad_dir, exist_ok=True)
        self._cached_user_context: dict[str, str] | None = None
        self._cached_system_context: dict[str, str] | None = None
        self.deps.invalidate_prompt_context = self.invalidate_prompt_context

        
        
        self.mutable_messages: list[dict[str, Any]] = []
        
        self._abort_event = asyncio.Event()
        
        self.total_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        
        
        self.discovered_skill_names: set[str] = set()
        
        self.loaded_nested_memory_paths: set[str] = set()

        # ── 懒加载的 tool 定义 (fetchSystemPromptParts 时需要) ──
        self._tools: list[ToolDefinition] = []
        self._handlers: dict[str, Any] = {}

    

    def interrupt(self) -> None:
        ""
        self._abort_event.set()
        logger.debug("interrupt() called — abort_event set")

    def reset_abort(self) -> None:
        """每轮开始时重置 abort 信号。"""
        self._abort_event.clear()

    def invalidate_prompt_context(self, _reason: str = "manual") -> None:
        """Invalidate only this conversation's cached Prompt/context state."""

        from bglab.prompt.system_prompt_sections import clear_system_prompt_sections

        clear_system_prompt_sections(self._prompt_cache_namespace)
        self._cached_user_context = None
        self._cached_system_context = None

    def set_model(self, model: str) -> None:
        if model == self.model:
            return
        self.model = model
        self.invalidate_prompt_context("model_changed")

    def set_permission_mode(self, mode: str) -> None:
        from bglab.session.state import session_state

        self.permission_mode = str(mode or "default")
        session_state.permission_mode = self.permission_mode

    def _refresh_session_cwd(self) -> None:
        from bglab.session.state import session_state

        current = str(getattr(session_state, "cwd", "") or self.cwd)
        if os.path.abspath(current) == os.path.abspath(self.cwd):
            return
        self.cwd = current
        self.deps.current_cwd = current
        self.invalidate_prompt_context("cwd_changed")

    

    def get_messages(self) -> list[dict[str, Any]]:
        return list(self.mutable_messages)

    # ── submit_message (async generator) ──

    async def submit_message(
        self,
        user_message: str,
        *,
        messages: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[LoopEvent, None]:
        ""
        self._refresh_session_cwd()
        self.discovered_skill_names.clear()
        self.reset_abort()
        start_time = time.time()

        # ── ① 加载 claude.md ──
        claude_md_content = None
        if self.claude_md_path and self._cached_user_context is None:
            try:
                with open(self.claude_md_path, encoding="utf-8") as f:
                    claude_md_content = f.read().strip()
            except Exception:
                pass

        # ── ② 组装 tool 定义 ──
        self._tools = []
        self._handlers = {}
        if self.tool_registry:
            self._tools = self.tool_registry.to_definitions()
            self._handlers = self.tool_registry.to_handler_dict()

        # ── ②.5 组装 PermissionContext ──
        # QueryEngine owns the initial mode; the trusted Code tool adapter
        # applies explicit EnterPlanMode/ExitPlanMode transitions to this
        # per-query context.
        from bglab.session.state import session_state
        from bglab.permissions.types import (
            PermissionContext,
            PermissionMode as PM,
            populate_permission_context_from_settings,
        )
        from bglab.session.settings import load_settings

        # ``self.permission_mode`` is the per-engine authority.  The process
        # singleton is only updated through explicit engine/Plan-tool
        # transitions and must not overwrite an explicitly configured BYPASS.
        effective_mode = self.permission_mode
        mode_map = {
            "default": PM.DEFAULT,
            "plan": PM.PLAN,
            "accept_edits": PM.ACCEPT_EDITS,
            "bypass": PM.BYPASS,
            "dont_ask": PM.DONT_ASK,
        }
        permission_context = PermissionContext(
            mode=mode_map.get(effective_mode, PM.DEFAULT),
        )
        populate_permission_context_from_settings(permission_context, load_settings(cwd=self.cwd))

        # ── ③ Profile request resolver — loop resolves Prompt + Tool together ──
        loop_profile = self.deps.query_profile or CODE_QUERY_PROFILE
        async def request_prompt_resolver(
            *,
            tools: list[ToolDefinition],
            model: str,
            permission_mode: str,
        ):
            self._refresh_session_cwd()
            if model != self.model:
                self.model = model
                self.invalidate_prompt_context("provider_model_changed")
            self.permission_mode = permission_mode
            bundle = await resolve_prompt_bundle(
                loop_profile.prompt,
                tools=tools,
                cwd=self.cwd,
                model=model,
                append_prompt=self.system_prompt_append,
                claude_md_content=claude_md_content,
                feature_flags=self.feature_flags,
                permission_mode=(
                    self.permission_mode
                ),
                language=self.language,
                scratchpad_dir=self._scratchpad_dir,
                token_budget=(
                    getattr(self.deps.budget_tracker, "budget_total", None)
                    if self.deps.budget_tracker is not None
                    else None
                ),
                cache_namespace=self._prompt_cache_namespace,
                cached_user_context=self._cached_user_context,
                cached_system_context=self._cached_system_context,
            )
            if self._cached_user_context is None:
                self._cached_user_context = dict(bundle.user_context)
            if self._cached_system_context is None:
                self._cached_system_context = dict(bundle.system_context)
            return bundle

        self.deps.request_prompt_resolver = request_prompt_resolver
        system_prompt = ""
        system_prompt_sections: list[str] = []
        user_context: dict[str, str] = {}
        system_context: dict[str, str] = {}

        # ── ③.3 prompt snapshot capture moved to query_loop (after assembly) ──

        # ── ④ processUserInput — 处理用户输入 ──
        messages_from_user_input, should_query, allowed_tools, model_from_user_input, result_text = \
            await process_user_input(
                user_message=user_message,
                existing_messages=messages or self.mutable_messages,
                cwd=self.cwd,
            )
        logger.debug(f"processUserInput: should_query={should_query}, "
                     f"model={model_from_user_input}, result_text_len={len(result_text)}")

        
        # 用户消息含 "+500k" / "use 2M tokens" 时设置本会话预算
        from bglab.engine.stubs import parse_token_budget
        if self.deps and self.deps.budget_tracker is not None:
            parsed_budget = parse_token_budget(user_message)
            if parsed_budget:
                self.deps.budget_tracker.budget_total = parsed_budget
                logger.debug(f"budget set from user message: {parsed_budget:,} tokens")

        # ── ⑤ 选择既有历史；共享 AgentRuntimeProfile 追加本轮输入 ──
        if messages is not None:
            self.mutable_messages = list(messages)

        # ── ⑥ shouldQuery? → 本地命令 / 进入 query_loop ──
        if not should_query:
            # Local mode commands deliberately update session state; adopt
            # that user-triggered transition for this persistent engine.
            command_parts = user_message.strip().lower().split()
            changes_mode = bool(
                command_parts
                and (
                    command_parts[0] in {"/plan", "/default", "/accept-edits"}
                    or (
                        command_parts[0] == "/permissions"
                        and len(command_parts) >= 2
                        and command_parts[1] == "mode"
                    )
                )
            )
            if changes_mode:
                self.permission_mode = str(
                    session_state.permission_mode or self.permission_mode
                )
            if result_text:
                yield LoopEvent(type=LoopEventType.TEXT, text=result_text)
            yield LoopEvent(
                type=LoopEventType.DONE,
                stop_reason=StopReason.END_TURN,
                terminal=TerminalInfo(
                    reason="completed",
                    turn_count=0,
                    elapsed_ms=(time.time() - start_time) * 1000,
                ),
            )
            return

        # ── ⑦ skills/plugins Promise.all 并行加载 ──
        logger.debug("loading skills + plugins (parallel)...")
        from bglab.engine.stubs import load_skills as _load_skills
        skills, _plugins = await asyncio.gather(
            _load_skills(self.cwd),
            _load_plugins_stub(),
        )
        if skills:
            logger.debug(f"skills loaded: {len(skills)}")
        if _plugins:
            logger.debug(f"plugins loaded: {len(_plugins)}")

        from bglab.permissions.types import merge_skill_allowed_tools
        merge_skill_allowed_tools(permission_context, skills)

        # ── ⑧ 进入 query_loop ──
        logger.debug(f"enter query_loop ({len(self._tools)} tools)")

        model = model_from_user_input or self.model

        terminal_info: TerminalInfo | None = None

        async for event in query_loop(
            messages=self.mutable_messages,
            system_prompt=system_prompt,
            system_prompt_sections=system_prompt_sections,
            tools=self._tools,
            handlers=self._handlers,
            deps=self.deps,
            user_context=user_context,
            system_context=system_context,
            model=model,
            max_turns=self.max_turns,
            permission_mode=effective_mode,
            permission_context=permission_context,
            ask_callback=self.ask_callback,
            cwd=self.cwd,
            session_id=self.session_id,
            request_session_id=self.session_id or self._prompt_cache_namespace,
            abort_event=self._abort_event,
            turn_input=user_message,
        ):
            if event.terminal:
                terminal_info = event.terminal
            yield event

        from bglab.engine.query_profiles import _code_permission_mode

        self.permission_mode = _code_permission_mode(
            permission_context,
            effective_mode,
        )

        # ── ⑨ query_loop 退出 → 持久化 ──
        elapsed = time.time() - start_time
        terminal_reason = terminal_info.reason if terminal_info else "unknown"
        logger.debug(f"done - terminal={terminal_reason} elapsed={elapsed:.1f}s")
        checkpoint("submit", terminal=terminal_reason, elapsed_ms=elapsed * 1000,
                   turns=terminal_info.turn_count if terminal_info else 0)

        has_boundary = any(m.get("_compact_boundary") for m in self.mutable_messages)
        sess_id = save_transcript(
            self.mutable_messages, cwd=self.cwd, session_id=self.session_id,
            session_meta={
                "model": self.model,
                "tools": list(
                    self.deps._last_tool_surface_names
                    or [tool.name for tool in self._tools]
                ),
                "permission_mode": self.permission_mode,
                "cwd": self.cwd,
            },
        )
        checkpoint("transcript_save", session_id=sess_id, msg_count=len(self.mutable_messages),
                   has_boundary=has_boundary)
        logger.debug(f"saved transcript: {sess_id}")


# ══════════════════════════════════════════════

# ══════════════════════════════════════════════

async def fetch_system_prompt_parts(
    *,
    tools: list[ToolDefinition],
    cwd: str,
    model: str = "deepseek-chat",
    append_prompt: str = "",
    claude_md_content: str | None = None,
    feature_flags: Any = None,
    language: str | None = None,
) -> tuple[str, list[str], dict[str, str], dict[str, str]]:
    """Backward-compatible Code Profile prompt assembly wrapper."""
    bundle = await resolve_prompt_bundle(
        CODE_QUERY_PROFILE.prompt,
        tools=tools,
        cwd=cwd,
        model=model,
        append_prompt=append_prompt,
        claude_md_content=claude_md_content,
        feature_flags=feature_flags,
        language=language,
    )
    return (
        bundle.system_prompt,
        list(bundle.sections),
        bundle.user_context,
        bundle.system_context,
    )


# ══════════════════════════════════════════════

# ══════════════════════════════════════════════

async def process_user_input(
    user_message: str,
    existing_messages: list[dict[str, Any]],
    cwd: str = "",
) -> tuple[list[dict[str, Any]], bool, list[str], str | None, str]:
    ""
    messages = list(existing_messages)

    if user_message.strip().startswith("/"):
        from bglab.slash_commands.registry import CommandRegistry
        registry = CommandRegistry()
        result = registry.execute(user_message.strip())

        if result["type"] == "local":
            text = result["data"]
            # Apply side effects for mode-changing commands
            _apply_slash_side_effects(user_message.strip(), result)
            result_msg = {
                "role": "user",
                "content": [{"type": "text", "text": f"<local-command-stdout>{text}</local-command-stdout>"}],
                "_is_meta": True,
            }
            messages.append(result_msg)
            return messages, False, [], None, text

        if result["type"] == "prompt":
            # Prompt command (skill) — forward expanded text to LLM
            new_message = result["data"]
            result_msg = {
                "role": "user",
                "content": [{"type": "text", "text": new_message}],
            }
            messages.append(result_msg)
            return messages, True, [], None, ""

        # result["type"] == "not_found" — forward raw /text to LLM

    return messages, True, [], None, ""


def _apply_slash_side_effects(raw_text: str, result: dict) -> None:
    """Apply side effects for slash commands that change session state.

    CommandRegistry.execute() returns display text; this function handles
    the state-mutating side effects that the registry doesn't know about.
    """
    from bglab.session.state import session_state

    cmd_name = raw_text.strip().split()[0].lower() if raw_text else ""
    parts = raw_text.strip().split(maxsplit=1)
    args = parts[1] if len(parts) > 1 else ""

    if cmd_name in ("/plan",):
        if session_state.permission_mode != "plan":
            session_state.pre_plan_mode = session_state.permission_mode
            session_state.permission_mode = "plan"
    elif cmd_name in ("/default",):
        session_state.pre_plan_mode = session_state.permission_mode
        session_state.permission_mode = "default"
    elif cmd_name in ("/accept-edits",):
        session_state.pre_plan_mode = session_state.permission_mode
        session_state.permission_mode = "accept_edits"
    elif cmd_name in ("/model",) and args.strip():
        session_state.model = args.strip()


async def _load_plugins_stub() -> list[dict]:
    ""
    return []


# ══════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════

def _make_default_deps() -> QueryDeps:
    from bglab.llm.client import call_model as _call_model
    return QueryDeps(
        call_model=_call_model,
        compact_tracker=CompactTracker(),
        loop_detector=LoopDetector(),
        stop_hooks_state=StopHooksState(),
    )


async def _default_ask_callback(tool_name: str, tool_input: dict, reason: str) -> bool:
    """Default: auto-deny in non-interactive mode. REPL overwrites this."""
    return False




# ══════════════════════════════════════════════
# Backward-compat: standalone submit_message (delegates to QueryEngine)
# ══════════════════════════════════════════════

async def submit_message(
    user_message: str,
    *,
    messages: list[dict[str, Any]] | None = None,
    tool_registry: ToolRegistry | None = None,
    system_prompt_append: str = "",
    cwd: str | None = None,
    model: str = "deepseek-chat",
    max_turns: int = 50,
    permission_mode: str = "default",
    claude_md_path: str | None = None,
    deps: QueryDeps | None = None,
    session_id: str | None = None,
    ask_callback: Any = None,
    feature_flags: Any = None,
) -> AsyncGenerator[LoopEvent, None]:
    ""
    from bglab.session.state import session_state
    # Seed the shared session state so tools (EnterPlanMode etc.) start from the right baseline
    if session_state.permission_mode == "default" or permission_mode != "default":
        session_state.permission_mode = permission_mode

    engine = QueryEngine(
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        permission_mode=permission_mode,
        claude_md_path=claude_md_path,
        system_prompt_append=system_prompt_append,
        deps=deps,
        session_id=session_id,
        ask_callback=ask_callback,
        tool_registry=tool_registry,
        feature_flags=feature_flags,
    )
    if messages:
        engine.mutable_messages = list(messages)

    async for event in engine.submit_message(user_message, messages=messages):
        yield event
