""

from __future__ import annotations

import asyncio
import copy
import concurrent.futures
import inspect
import logging
import time
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any, AsyncGenerator, Callable
from uuid import uuid4

from bglab.llm.client import FallbackTriggeredError
from bglab.llm.providers import get_model_context_window
from bglab.llm.types import (
    CompletionObservation,
    LoopEvent,
    LoopEventType,
    ProviderFailureInfo,
    StopReason,
    TerminalInfo,
    ToolDefinition,
    ToolResultBlock,
    ToolUseBlock,
)
from bglab.llm.usage import empty_usage_totals, record_request_usage
from bglab.prompt.context import append_system_context
from bglab.prompt import viewer

# Compaction imports — at module level, not buried in the loop
from bglab.compaction.autocompact import (
    auto_compact_if_needed,
    CompactionProviderStopped,
    CompactTracker,
)
from bglab.compaction.token_counter import estimate_request_tokens
from bglab.loop_detector import LoopDetector
from bglab.engine.deps import QueryDeps
from bglab.engine.error_policy import PROVIDER_REQUEST_FAILED, RUNTIME_FAILURE
from bglab.engine.attachments import AttachmentTurn, ProviderContext
from bglab.engine.query_profiles import CODE_QUERY_PROFILE, QueryLoopProfile
from bglab.engine.prompt_profiles import PromptBundle
from bglab.engine.turn_input import TurnInput, normalize_turn_input
from bglab.engine.tool_execution import (
    ToolExecutionOutcome,
    ToolInvocationContext,
    tool_unavailable_outcome,
)
from bglab.engine.trace import (
    Tracker, checkpoint,
    llm_call_start, llm_call_end, loop_detector_result, compact_boundary,
)
from bglab.hooks.state import StopHooksState
from bglab.permissions.types import ToolPermissionSpec

logger = logging.getLogger("bglab.engine.query")


def _validate_outbound_tool_schemas(tools: list[ToolDefinition]) -> None:
    """Reject invalid provider-visible tool schemas before an API request."""
    from jsonschema import Draft202012Validator, SchemaError

    for tool in tools:
        try:
            Draft202012Validator.check_schema(tool.parameters)
        except SchemaError as error:
            raise ValueError(
                f"OUTBOUND_TOOL_SCHEMA_INVALID:{tool.name}: {error.message}",
            ) from error


# ══════════════════════════════════════════════
# Types
# ══════════════════════════════════════════════

class TransitionReason(str, Enum):
    ""
    NEXT_TURN = "next_turn"
    REACTIVE_COMPACT_RETRY = "reactive_compact_retry"
    COLLAPSE_DRAIN_RETRY = "collapse_drain_retry"
    MAX_OUTPUT_TOKENS_ESCALATE = "max_output_tokens_escalate"
    MAX_OUTPUT_TOKENS_RECOVERY = "max_output_tokens_recovery"
    VISIBLE_DELIBERATION_RECOVERY = "visible_deliberation_recovery"
    STOP_HOOK_BLOCKING = "stop_hook_blocking"
    TOKEN_BUDGET_CONTINUATION = "token_budget_continuation"
    MODEL_ERROR_RECOVERY = "model_error_recovery"


class TerminalReason(str, Enum):
    ""
    COMPLETED = "completed"
    BLOCKING_LIMIT = "blocking_limit"
    HOOK_STOPPED = "hook_stopped"
    STOP_HOOK_PREVENTED = "stop_hook_prevented"
    MAX_TURNS = "max_turns"
    MODEL_ERROR = "model_error"
    PROMPT_TOO_LONG = "prompt_too_long"
    ABORTED_STREAMING = "aborted_streaming"
    ABORTED_TOOLS = "aborted_tools"
    USER_ABORT = "user_abort"
    IMAGE_ERROR = "image_error"
    TOOL_OUTCOME_INDETERMINATE = "tool_outcome_indeterminate"


@dataclass
class Transition:
    reason: TransitionReason
    detail: Any = None


@dataclass
class QueryTracking:
    ""
    chain_id: str = ""
    depth: int = 0


@dataclass
class ToolUseContext:
    ""
    agent_depth: int = 0
    agent_id: str | None = None
    permission_mode: str = "default"
    modified_files: list[str] = field(default_factory=list)
    read_file_state: dict[str, Any] = field(default_factory=dict)
    query_tracking: QueryTracking = field(default_factory=QueryTracking)


@dataclass
class QueryState:
    ""
    messages: list[dict[str, Any]]
    tool_use_context: ToolUseContext = field(default_factory=ToolUseContext)
    turn_count: int = 1
    max_turns: int = 50
    transition: Transition | None = None
    max_output_tokens_recovery_count: int = 0
    output_budget_recovery: bool = False
    invalid_tool_arguments_recovery: bool = False
    visible_deliberation_recovery_count: int = 0
    has_attempted_reactive_compact: bool = False
    auto_compact_tracking: dict[str, Any] = field(default_factory=dict)
    pending_tool_use_summary: Any = None
    stop_hook_active: bool = False
    current_message_usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0})
    last_stop_reason: str = ""
    budget_global_turn_tokens: int = 0  # 累计本会话已用 output tokens，每次 final_usage 到时更新


class ImageSizeError(Exception):
    ""
    pass


class AbortEvent:
    ""
    def __init__(self, reason: str = ""):
        self._event = asyncio.Event()
        self.reason = reason

    def set(self, reason: str = "interrupt") -> None:
        self.reason = reason
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()


# ══════════════════════════════════════════════
# queryLoop — the while True loop
# ══════════════════════════════════════════════

ToolHandler = Callable[[dict[str, Any]], str]


async def _resolve_request_prompt_bundle(
    deps: QueryDeps,
    *,
    tools: list[ToolDefinition],
    model: str,
    permission_mode: str,
    fallback: PromptBundle,
) -> PromptBundle:
    resolver = getattr(deps, "request_prompt_resolver", None)
    if not callable(resolver):
        return fallback
    bundle = resolver(
        tools=tools,
        model=model,
        permission_mode=permission_mode,
    )
    if inspect.isawaitable(bundle):
        bundle = await bundle
    if not isinstance(bundle, PromptBundle):
        raise TypeError("request prompt resolver returned invalid PromptBundle")
    return bundle


async def query_loop(
    messages: list[dict[str, Any]],
    system_prompt: str,
    tools: list[ToolDefinition],
    handlers: dict[str, ToolHandler],
    *,
    deps: QueryDeps | None = None,
    user_context: dict[str, str] | None = None,
    system_context: dict[str, str] | None = None,
    system_prompt_sections: list[str] | None = None,
    model: str = "deepseek-chat",
    provider_slot: Any = None,
    fallback_model: str | None = None,
    max_turns: int = 50,
    permission_mode: str = "default",
    permission_context: Any = None,
    ask_callback: Callable[[str, dict, str], Any] | None = None,
    cwd: str = "",
    session_id: str | None = None,
    abort_event: Any = None,
    turn_input: TurnInput | str | None = None,
    turn_input_appended: Callable[[], Any] | None = None,
    request_session_id: str | None = None,
) -> AsyncGenerator[LoopEvent, None]:
    """Public query generator with one owned attachment turn lifecycle."""

    if turn_input is not None:
        turn_input = normalize_turn_input(turn_input)
    resolved = deps if deps is not None else _default_deps()
    if deps is not None:
        defaults = _default_deps()
        if resolved.call_model is None:
            resolved.call_model = defaults.call_model
        if resolved.compact_tracker is None:
            resolved.compact_tracker = defaults.compact_tracker
        if resolved.loop_detector is None:
            resolved.loop_detector = defaults.loop_detector
        if resolved.stop_hooks_state is None:
            resolved.stop_hooks_state = defaults.stop_hooks_state

    loop_profile = resolved.query_profile or CODE_QUERY_PROFILE
    effective_permission_mode = loop_profile.tools.resolve_permission_mode(
        permission_context,
        permission_mode,
    )
    state = QueryState(
        messages=messages,
        tool_use_context=ToolUseContext(
            permission_mode=effective_permission_mode,
        ),
        turn_count=1,
        max_turns=max_turns,
    )
    initial_tools = loop_profile.tools.resolve_surface(
        list(tools),
        permission_context,
        effective_permission_mode,
    )
    initial_bundle = await _resolve_request_prompt_bundle(
        resolved,
        tools=initial_tools,
        model=model,
        permission_mode=effective_permission_mode,
        fallback=PromptBundle(
            system_prompt=system_prompt,
            sections=tuple(system_prompt_sections or ()),
            user_context=dict(user_context or {}),
            system_context=dict(system_context or {}),
        ),
    )
    system_prompt = initial_bundle.system_prompt
    system_prompt_sections = list(initial_bundle.sections)
    user_context = dict(initial_bundle.user_context)
    system_context = dict(initial_bundle.system_context)
    cwd = str(getattr(resolved, "current_cwd", None) or cwd)
    profile = loop_profile.attachments
    attachment_turn = AttachmentTurn.start(
        profile,
        ProviderContext(
            profile=profile,
            messages=messages,
            deps=resolved,
            state=state,
            tool_use_context=state.tool_use_context,
            cwd=cwd,
            model=model,
            permission_mode=effective_permission_mode,
            local={
                "user_context": user_context or {},
                "deferred_tool_names": [],
                "turn_input": turn_input.text if turn_input is not None else None,
            },
        ),
    )
    try:
        input_attachments = await attachment_turn.collect_user_input()
        messages.extend(input_attachments)
        initial_memory = loop_profile.memory.collect_initial(
            attachment_turn,
            attachment_turn.context,
        )
        if inspect.isawaitable(initial_memory):
            initial_memory = await initial_memory
        messages.extend(initial_memory)
        current_turn_messages = [*input_attachments, *initial_memory]
        if turn_input is not None:
            messages.append(
                loop_profile.agent_runtime.build_turn_message(turn_input),
            )
            if turn_input_appended is not None:
                delivered = turn_input_appended()
                if inspect.isawaitable(delivered):
                    await delivered
        resolved._last_attachment_messages = current_turn_messages
        # The public boundary accounts each request once. Raw DONE is
        # consumed by runners; terminal totals serve Code UI/fork callers.
        request_usage = empty_usage_totals()
        async for event in _query_loop_core(
            messages,
            system_prompt,
            tools,
            handlers,
            deps=resolved,
            user_context=user_context,
            system_context=system_context,
            system_prompt_sections=system_prompt_sections,
            model=model,
            provider_slot=provider_slot,
            fallback_model=fallback_model,
            max_turns=max_turns,
            permission_mode=effective_permission_mode,
            permission_context=permission_context,
            ask_callback=ask_callback,
            cwd=cwd,
            session_id=session_id,
            request_session_id=request_session_id,
            abort_event=abort_event,
            _state=state,
            _attachment_turn=attachment_turn,
            _loop_profile=loop_profile,
        ):
            if event.type == LoopEventType.DONE:
                if event.terminal is None:
                    record_request_usage(request_usage, event.usage, purpose=event.request_purpose)
                else:
                    event = replace(event, usage=dict(request_usage),
                        terminal=replace(event.terminal, total_usage=dict(request_usage)))
            yield event
    finally:
        await attachment_turn.aclose()


async def _query_loop_core(
    messages: list[dict[str, Any]],
    system_prompt: str,
    tools: list[ToolDefinition],
    handlers: dict[str, ToolHandler],
    *,
    deps: QueryDeps | None = None,
    user_context: dict[str, str] | None = None,
    system_context: dict[str, str] | None = None,
    system_prompt_sections: list[str] | None = None,
    model: str = "deepseek-chat",
    provider_slot: Any = None,
    fallback_model: str | None = None,
    max_turns: int = 50,
    permission_mode: str = "default",
    permission_context: Any = None,
    ask_callback: Callable[[str, dict, str], Any] | None = None,
    cwd: str = "",
    session_id: str | None = None,
    abort_event: Any = None,
    _state: QueryState | None = None,
    _attachment_turn: AttachmentTurn | None = None,
    _loop_profile: QueryLoopProfile | None = None,
    request_session_id: str | None = None,
) -> AsyncGenerator[LoopEvent, None]:
    ""

    if deps is None:
        deps = _default_deps()
    else:
        # Merge: user-provided deps fills in individual None fields
        defaults = _default_deps()
        if deps.call_model is None:
            deps.call_model = defaults.call_model
        if deps.complete_text is None:
            deps.complete_text = defaults.complete_text
        if deps.compact_tracker is None:
            deps.compact_tracker = defaults.compact_tracker
        if deps.loop_detector is None:
            deps.loop_detector = defaults.loop_detector
        if deps.stop_hooks_state is None:
            deps.stop_hooks_state = defaults.stop_hooks_state

    # A logical conversation identity spans decisions, retries and summaries.
    # An unpersisted caller gets one identity for this loop, never a global one.
    request_session_id = request_session_id or session_id or uuid4().hex

    loop_profile = _loop_profile or deps.query_profile or CODE_QUERY_PROFILE
    current_max_tokens = loop_profile.budget_thinking.initial_max_tokens
    max_tokens_escalated = loop_profile.recovery.escalated_max_tokens
    max_output_tokens_recovery_limit = (
        loop_profile.recovery.max_output_recovery_attempts
    )
    consecutive_errors = 0

    state = _state or QueryState(
        messages=messages,  # NOT a copy — caller's reference, modified in place for transcript save
        tool_use_context=ToolUseContext(permission_mode=permission_mode),
        turn_count=1,
        max_turns=max_turns,
    )
    state.max_turns = max_turns

    logger.debug(f" start model={model} tools={[t.name for t in tools]}")
    compaction_events: list[LoopEvent] = []

    def observe_compaction(observation: CompletionObservation) -> None:
        # Record synchronously, including cancellation, before the next
        # yield. Keep auxiliary accounting separate; a provider-requested
        # long wait stops the query through compaction control flow below.
        record, event = _compaction_observation(observation, provider_slot)
        if not hasattr(deps, "_request_records"):
            deps._request_records = []
        deps._request_records.append(record)
        compaction_events.append(event)

    async def render_request_head(history):
        return await _attachment_turn.render_head(
            ProviderContext(
                profile=_attachment_turn.profile,
                messages=history,
                deps=deps,
                state=state,
                tool_use_context=state.tool_use_context,
                cwd=cwd,
                model=current_model,
                permission_mode=effective_permission_mode,
                phase="head",
                local={
                    "user_context": user_context or {},
                    "deferred_tool_names": deferred_tool_names,
                    "compact_relink": getattr(deps, "compact_relink", None),
                },
            )
        )

    current_model = model  # 可能因 fallback 而切换
    while True:
        final_usage: dict | None = None
        compact_result = None
        tracker = deps.compact_tracker
        effective_permission_mode = (
            loop_profile.tools.resolve_permission_mode(
                permission_context,
                permission_mode,
            )
        )
        state.tool_use_context.permission_mode = effective_permission_mode
        iteration_tools = loop_profile.tools.resolve_surface(
            list(tools),
            permission_context,
            effective_permission_mode,
        )
        tools_for_api = list(iteration_tools)
        deferred_tool_names: list[str] = []
        if deps.deferred_registry is not None:
            should_defer_names = {
                tool.name for tool in tools_for_api
                if (
                    getattr(tool, "should_defer", False)
                    and not getattr(tool, "always_load", False)
                )
            }
            assembly = deps.deferred_registry.classify(
                tools_for_api,
                should_defer_names=should_defer_names,
            )
            if assembly.activated:
                tools_for_api = list(assembly.tool_defs)
            deps._last_assembly = assembly
            if assembly.activated:
                deferred_tool_names = [
                    str(entry.get("name", ""))
                    for entry in deps.deferred_registry._catalog
                    if str(entry.get("name", "")).strip()
                ]
        logger.debug(" deferred_tools=%s", deferred_tool_names)
        permission_specs = {
            tool.name: tool.permission_spec for tool in tools_for_api
        }
        deps._last_tool_surface_names = [tool.name for tool in tools_for_api]
        prompt_bundle = await _resolve_request_prompt_bundle(
            deps,
            tools=tools_for_api,
            model=current_model,
            permission_mode=effective_permission_mode,
            fallback=PromptBundle(
                system_prompt=system_prompt,
                sections=tuple(system_prompt_sections or ()),
                user_context=dict(user_context or {}),
                system_context=dict(system_context or {}),
            ),
        )
        system_prompt = prompt_bundle.system_prompt
        system_prompt_sections = list(prompt_bundle.sections)
        user_context = dict(prompt_bundle.user_context)
        system_context = dict(prompt_bundle.system_context)
        cwd = str(getattr(deps, "current_cwd", None) or cwd)
        final_surface_names = {tool.name for tool in tools_for_api}
        iteration_handlers = {
            name: handler
            for name, handler in handlers.items()
            if name in final_surface_names
        }
        if (
            deps.deferred_registry is not None
            and getattr(deps._last_assembly, "activated", False)
        ):
            async def authorize_deferred_tool(
                name: str,
                arguments: dict[str, Any],
                permission_spec: ToolPermissionSpec,
            ) -> tuple[bool, str]:
                allowed, reason, needs_ask = _check_tool_permission(
                    name,
                    arguments,
                    permission_spec,
                    permission_context,
                    effective_permission_mode,
                    cwd,
                )
                if not needs_ask:
                    return allowed, reason
                if ask_callback is None:
                    return False, reason
                try:
                    decision = await ask_callback(name, arguments, reason)
                    if isinstance(decision, bool):
                        allowed = decision
                    elif hasattr(decision, "behavior"):
                        allowed = decision.behavior == "allow"
                    else:
                        allowed = str(decision).lower().startswith("y")
                except Exception:
                    allowed = False
                return (
                    allowed,
                    reason if allowed else f"denied by user: {reason}",
                )

            iteration_handlers.update(
                deps.deferred_registry.bridge_handlers(
                    handlers,
                    authorize=authorize_deferred_tool,
                ),
            )
        full_system_prompt_parts = [system_prompt]
        if system_context:
            full_system_prompt_parts = append_system_context(
                [system_prompt],
                system_context,
            )
        full_system_prompt = "\n\n".join(full_system_prompt_parts)
        logger.debug(f"\n{'=' * 40}")
        logger.debug(f" [1] turn={state.turn_count} "
              f"transition={state.transition.reason.value if state.transition else 'none'} "
              f"max_tokens={current_max_tokens}")

        # ── Yield pending tool use summary from previous turn ──
        
        pending_summary = state.pending_tool_use_summary
        if pending_summary is not None:
            try:
                summary_text = await pending_summary
                if summary_text:
                    yield LoopEvent(
                        type=LoopEventType.TEXT,
                        text=f"\n[{summary_text}]\n",
                    )
            except Exception:
                pass
            state.pending_tool_use_summary = None

        attachment_iteration = None
        # A same-request recovery starts a fresh iteration. Close the prior
        # skill handle before replacing it; turn-level memory remains owned by
        # AttachmentTurn and is never restarted here.
        await _attachment_turn.close_iterations()
        attachment_iteration = _attachment_turn.start_iteration(
            ProviderContext(
                profile=_attachment_turn.profile,
                messages=state.messages,
                deps=deps,
                state=state,
                tool_use_context=state.tool_use_context,
                cwd=cwd,
                model=current_model,
                permission_mode=effective_permission_mode,
                local={
                    "user_context": user_context or {},
                    "deferred_tool_names": deferred_tool_names,
                    "turn_input": _attachment_turn.context.local.get(
                        "turn_input",
                    ),
                },
            ),
        )

        # ═══ ② 上下文整形（5层压缩流水线） ═══
        ctx_tracker = Tracker("ctx_shaping", turn=state.turn_count).start()

        # 2a: getMessagesAfterCompactBoundary — 从最后 compact 边界开始切片
        pre_boundary = len(state.messages)
        messages_for_query = _get_messages_after_compact_boundary(state.messages)
        relink_source_messages = list(messages_for_query)
        request_tool_choice = (
            loop_profile.tools.resolve_request_tool_choice(
                messages_for_query,
                tools_for_api,
            )
            if loop_profile.tools.resolve_request_tool_choice is not None
            else None
        )
        if len(messages_for_query) < pre_boundary:
            compact_boundary("slice", pre_tokens=pre_boundary - len(messages_for_query),
                            messages_kept=len(messages_for_query))

        shaping = loop_profile.context_shaping.shape(
            messages_for_query,
            current_model,
        )
        messages_for_query = shaping.messages
        snip_freed = shaping.snip_tokens_freed
        if snip_freed:
            checkpoint("snip", tokens_freed=snip_freed)
        mc_saved = shaping.microcompact_tokens_freed
        if mc_saved:
            checkpoint("microcompact", freed=mc_saved)
        collapse_saved = shaping.collapse_tokens_saved
        if collapse_saved:
            checkpoint("context_collapse", saved=collapse_saved)

        head_messages = await render_request_head(messages_for_query)
        request_tokens = estimate_request_tokens(
            [*head_messages, *messages_for_query], system_prompt=full_system_prompt,
            tools=tools_for_api, model=current_model,
            thinking_effort=loop_profile.budget_thinking.initial_thinking_effort,
        )

        # 2f: AutoCompact — use tracker from deps, not function-attached
        
        # suppresses autocompact because collapse "owns the headroom" at
        # 90% commit / 95% blocking. Same logic here: if collapse freed
        # enough tokens to get below threshold, skip autocompact.
        if not shaping.collapse_owns_headroom:
            try:
                messages_for_query, tracker, compact_result = await auto_compact_if_needed(
                    messages_for_query,
                    deps.compact_tracker,
                    model=current_model,
                    cwd=cwd or None,
                    turn_count=state.turn_count,
                    profile=loop_profile.compaction,
                    provider_slot=provider_slot, completion=deps.complete_text,
                    observation_callback=observe_compaction,
                    request_session_id=request_session_id,
                    request_tokens=request_tokens, max_output_tokens=current_max_tokens,
                )
            except CompactionProviderStopped as stopped:
                ctx_tracker.stop(msg_count=len(messages_for_query))
                for event in compaction_events:
                    yield event
                compaction_events.clear()
                yield LoopEvent(
                    type=LoopEventType.ERROR, error=PROVIDER_REQUEST_FAILED,
                    provider_failure=stopped.provider_failure,
                )
                yield _make_terminal_event(
                    TerminalReason.MODEL_ERROR, state.turn_count, final_usage,
                )
                return
        deps.compact_tracker = tracker
        if compact_result:
            # Progress: only show during actual compaction
            if deps.compaction_tick:
                deps.compaction_tick("Compacting", f"{compact_result.pre_compact_tokens} tokens -> summary")
            checkpoint("compact", pre_tokens=compact_result.pre_compact_tokens,
                      messages_compacted=getattr(compact_result.boundary_marker, 'messages_compacted', 0)
                      if compact_result.boundary_marker else 0,
                      messages_kept=0, has_relink=True)
            _compact_boundary_relink(
                compact_result, deps, user_context or {},
                system_context or {}, effective_permission_mode,
                [], messages_for_query,
                relink_source_messages=relink_source_messages,
                cwd=cwd,
                session_id=session_id,
                _attachment_turn=_attachment_turn,
                _loop_profile=loop_profile,
            )
            # Compaction is a durable context transition, not a tentative
            # model response. Persist it before the provider call so a retry
            # cannot summarize the same oversized history repeatedly.
            state.messages[:] = messages_for_query
            if deps.compaction_tick:
                deps.compaction_tick("Compacted", "done")

        # Preserve the durable compact transition before exposing its
        # accounting event to consumers that may cancel at a yield.
        for event in compaction_events:
            yield event
        compaction_events.clear()

        ctx_tracker.stop(msg_count=len(messages_for_query))

        # ── BLOCKING_LIMIT check: 无 compact 且 circuit broken → 阻止继续 ──
        
        if not compact_result and deps.compact_tracker.is_circuit_broken():
            estimated = request_tokens
            if estimated > (
                (loop_profile.budget_thinking.context_window_tokens
                 or get_model_context_window(current_model))
                * loop_profile.budget_thinking.context_blocking_ratio
            ):
                logger.debug(f" BLOCKING_LIMIT: {estimated} tokens, circuit broken")
                yield _make_terminal_event(
                    TerminalReason.BLOCKING_LIMIT, state.turn_count, final_usage,
                )
                return

        # ═══ ③ prompt 拼接 ═══
        # One profile-owned session head is rendered immediately before every
        # provider request.  It is not persisted in ``state.messages`` and
        # therefore remains dynamic across retries and compaction boundaries.
        if compact_result:
            head_messages = await render_request_head(messages_for_query)
            request_tokens = estimate_request_tokens(
                [*head_messages, *messages_for_query], system_prompt=full_system_prompt,
                tools=tools_for_api, model=current_model,
                thinking_effort=loop_profile.budget_thinking.initial_thinking_effort,
            )
        msgs_with_context = [*head_messages, *messages_for_query]

        # Compaction may have no older material to replace, or a retained
        # current decision may still be too large. Never repeatedly send the
        # known oversized request; Game owns authority-safe action recovery.
        if request_tokens + current_max_tokens > get_model_context_window(current_model):
            yield _make_terminal_event(
                TerminalReason.PROMPT_TOO_LONG, state.turn_count, final_usage,
            )
            return

        _validate_outbound_tool_schemas(tools_for_api)
        estimated_tokens = request_tokens
        logger.debug(f" [3] context ~{len(msgs_with_context)} msgs ~{estimated_tokens} tokens")

        
        assistant_messages: list[dict] = []
        tool_use_blocks: list[tuple[str, str, dict]] = []
        needs_follow_up = False
        current_text = ""
        current_reasoning_content: str | None = None
        visible_deliberation_interrupted = False
        final_usage = None
        is_withheld_max_output = False
        is_withheld_provider_draft = False
        is_withheld_output_budget = False
        provider_failure_seen = False
        authoritative_terminal_seen = False

        llm_attempt = True
        # The first request uses the head assembled for prompt capture below;
        # every fallback/retry request gets a fresh profile render as well.
        pending_request_head = head_messages
        while llm_attempt:
            llm_attempt = False
            if pending_request_head is None:
                pending_request_head = await _attachment_turn.render_head(
                    ProviderContext(
                        profile=_attachment_turn.profile,
                        messages=messages_for_query,
                        deps=deps,
                        state=state,
                        tool_use_context=state.tool_use_context,
                        cwd=cwd,
                        model=current_model,
                        permission_mode=effective_permission_mode,
                        phase="head",
                        local={
                            "user_context": user_context or {},
                            "deferred_tool_names": deferred_tool_names,
                            "compact_relink": getattr(deps, "compact_relink", None),
                        },
                    )
                )
            msgs_with_context = [*pending_request_head, *messages_for_query]
            pending_request_head = None
            request_recovery = None
            if loop_profile.recovery.prepare_request is not None:
                request_recovery = loop_profile.recovery.prepare_request(deps, state)
                if request_recovery.stop_reason:
                    state.messages[:] = messages_for_query
                    yield LoopEvent(
                        type=LoopEventType.ERROR, error=PROVIDER_REQUEST_FAILED,
                        provider_failure=ProviderFailureInfo(
                            reason=request_recovery.stop_reason,
                            retryable_same_slot=False, switch_slot=False,
                        ),
                    )
                    yield _make_terminal_event(
                        TerminalReason.MODEL_ERROR, state.turn_count, final_usage,
                    )
                    return
            project_request = loop_profile.recovery.project_request_messages
            if project_request is not None:
                msgs_with_context = project_request(deps, msgs_with_context)
            # Capture the actual request projection, including retries, without
            # replacing the canonical conversation with a recovery-only view.
            _capture_for_show(
                system_prompt=full_system_prompt,
                system_prompt_sections=system_prompt_sections or [],
                user_context=user_context or {},
                system_context=system_context or {},
                messages=msgs_with_context,
                tools=tools_for_api,
                model=current_model,
                turn_count=state.turn_count,
            )
            logger.debug(f" [4] LLM call (model={current_model})...")
            llm_tracker = llm_call_start(current_model, current_max_tokens, len(msgs_with_context))
            request_started = time.monotonic()
            request_dispatched = False
            request_recorded = False
            observed_request_usage = None

            try:
                call_kwargs = {
                    "messages": msgs_with_context,
                    "system_prompt": full_system_prompt,
                    "tools": tools_for_api,
                    "model": current_model,
                    "fallback_model": fallback_model,
                    "max_tokens": current_max_tokens,
                    "request_session_id": request_session_id,
                }
                provider_request = loop_profile.recovery.provider_request
                if provider_request is not None:
                    call_kwargs.update({
                        "max_retries": provider_request.max_attempts,
                        "atomic_attempts": provider_request.atomic_attempts,
                        "first_event_timeout_seconds": (
                            provider_request.first_event_timeout_seconds
                        ),
                        "idle_timeout_seconds": (
                            provider_request.idle_timeout_seconds
                        ),
                        "attempt_timeout_seconds": (
                            provider_request.attempt_timeout_seconds
                        ),
                        "parallel_tool_calls": (
                            provider_request.parallel_tool_calls
                        ),
                    })
                if loop_profile.recovery.deliberation_repetition_guard:
                    call_kwargs["deliberation_repetition_guard"] = True
                if loop_profile.budget_thinking.initial_thinking_effort is not None:
                    call_kwargs["thinking_effort"] = (
                        loop_profile.budget_thinking.initial_thinking_effort
                    )
                if (
                    loop_profile.budget_thinking.disable_thinking_after_turn is not None
                    and state.turn_count
                    > loop_profile.budget_thinking.disable_thinking_after_turn
                ):
                    call_kwargs["disable_thinking"] = True
                if provider_slot is not None:
                    call_kwargs["provider_slot"] = provider_slot
                if request_tool_choice is not None:
                    call_kwargs["tool_choice"] = request_tool_choice
                if state.max_output_tokens_recovery_count > 0:
                    recovery = loop_profile.recovery
                    if recovery.output_recovery_disable_thinking:
                        call_kwargs["disable_thinking"] = True
                    if recovery.output_recovery_tool_name in final_surface_names:
                        # A profile cannot reintroduce a tool excluded by the
                        # current surface (or force a tool in a text-only use).
                        call_kwargs["tool_choice"] = {
                            "type": "function",
                            "function": {"name": recovery.output_recovery_tool_name},
                        }
                if request_recovery is not None:
                    if request_recovery.temperature is not None:
                        call_kwargs["temperature"] = request_recovery.temperature
                    if request_recovery.disable_thinking:
                        call_kwargs["disable_thinking"] = True
                    if request_recovery.tool_name in final_surface_names:
                        call_kwargs["tool_choice"] = {
                            "type": "function",
                            "function": {"name": request_recovery.tool_name},
                        }
                model_stream = deps.call_model(**call_kwargs)
                request_dispatched = True
                async for event in model_stream:
                    if (
                        event.type == LoopEventType.TOOL_USE_END
                        and event.tool_use is not None
                        and event.tool_use.name not in final_surface_names
                    ):
                        event = LoopEvent(
                            type=LoopEventType.TOOL_USE_END,
                            tool_use=ToolUseBlock(
                                id=event.tool_use.id,
                                name="TOOL_UNAVAILABLE",
                                input={},
                            ),
                        )
                    if event.type == LoopEventType.DONE and event.terminal is None:
                        # Keep the report before yielding: a caller can cancel
                        # while consuming this event, before the body resumes.
                        observed_request_usage = copy.deepcopy(event.usage)
                    yield event
                    if event.provider_failure is not None:
                        provider_failure_seen = True

                    
                    if abort_event is not None and abort_event.is_set():
                        abort_reason = getattr(abort_event, 'reason', '')
                        if abort_reason == 'interrupt':
                            logger.debug(" USER_ABORT during streaming")
                            state.last_stop_reason = "user_abort"
                            yield _make_terminal_event(
                                TerminalReason.USER_ABORT, state.turn_count, final_usage,
                            )
                        else:
                            logger.debug(f" ABORTED during streaming event {event.type}")
                            state.last_stop_reason = "aborted_streaming"
                            yield _make_terminal_event(
                                TerminalReason.ABORTED_STREAMING, state.turn_count, final_usage,
                            )
                        return

                    match event.type:
                        case LoopEventType.TEXT:
                            if event.text:
                                current_text += event.text
                            limit = (
                                loop_profile.recovery
                                .visible_deliberation_limit_chars
                            )
                            if (
                                limit is not None
                                and not tool_use_blocks
                                and state.visible_deliberation_recovery_count
                                < loop_profile.recovery
                                .max_visible_deliberation_recoveries
                                and len(current_text) > limit
                            ):
                                visible_deliberation_interrupted = True
                                break
                        case LoopEventType.TOOL_USE_END:
                            if event.tool_use:
                                tool_use_blocks.append((
                                    event.tool_use.id,
                                    event.tool_use.name,
                                    event.tool_use.input,
                                ))
                                needs_follow_up = True
                        case LoopEventType.ERROR:
                            consecutive_errors += 1
                        case LoopEventType.DONE:
                            if event.terminal is not None:
                                authoritative_terminal_seen = True
                            final_usage = event.usage
                            reclassified_visible_deliberation = bool(
                                (final_usage or {}).get(
                                    "provider_visible_deliberation_reclassified",
                                    0,
                                )
                            )
                            repetition_guard_interrupted = bool(
                                (final_usage or {}).get("provider_repetition_guard_interrupted", 0)
                            )
                            is_withheld_output_budget = bool(
                                (final_usage or {}).get("provider_output_budget_interrupted", 0)
                            )
                            state.output_budget_recovery |= is_withheld_output_budget
                            state.invalid_tool_arguments_recovery |= bool(
                                (final_usage or {}).get("provider_invalid_tool_arguments", 0)
                            )
                            # A Go/V4 response can put its private draft in
                            # ``content`` despite a non-thinking request.  The
                            # Provider adapter exposes that draft on this DONE
                            # event for diagnostics, but it must not become
                            # persistent history or a later model input.
                            current_reasoning_content = (
                                None
                                if reclassified_visible_deliberation or repetition_guard_interrupted
                                else event.reasoning_content
                            )
                            request_records = getattr(deps, "_request_records", None)
                            if request_records is None:
                                request_records = []
                                deps._request_records = request_records
                            request_records.append({
                                "model": current_model,
                                "requestUsage": copy.deepcopy(observed_request_usage),
                                "latencyMs": int((time.monotonic() - request_started) * 1000),
                                "stopReason": (
                                    event.stop_reason.value
                                    if hasattr(event.stop_reason, "value")
                                    else str(event.stop_reason or "")
                                ),
                                "toolUseCount": len(tool_use_blocks),
                                "visibleTextChars": len(current_text),
                                "inputTokens": int((final_usage or {}).get("input_tokens", 0) or 0),
                                "outputTokens": int((final_usage or {}).get("output_tokens", 0) or 0),
                                "reasoningTokens": int((final_usage or {}).get("reasoning_tokens", 0) or 0),
                                "providerAttempts": int((final_usage or {}).get("provider_attempts", 0) or 0),
                                "providerRetries": int((final_usage or {}).get("provider_retries", 0) or 0),
                                "providerTimeouts": int((final_usage or {}).get("provider_timeouts", 0) or 0),
                                "providerAttemptUsage": (final_usage or {}).get("provider_attempt_usage", []),
                                "usageMissingAttempts": int((final_usage or {}).get("usage_missing_attempts", 0) or 0),
                                "usagePartialAttempts": int((final_usage or {}).get("usage_partial_attempts", 0) or 0),
                                "reclassifiedVisibleDeliberationChars": int(
                                    (final_usage or {}).get(
                                        "provider_visible_deliberation_chars",
                                        0,
                                    )
                                    or 0
                                ),
                                "repeatedOutputAtDeadline": bool(
                                    (final_usage or {}).get("provider_visible_deliberation_at_deadline")
                                ),
                                "repetitionGuardInterrupted": repetition_guard_interrupted,
                                "outputBudgetInterrupted": is_withheld_output_budget,
                                "cacheReadTokens": int((final_usage or {}).get("cache_hit_tokens", 0) or 0),
                                "cacheMissTokens": int((final_usage or {}).get("cache_miss_tokens", 0) or 0),
                                "cacheDetailsSupported": bool((final_usage or {}).get("cache_details_supported", False)),
                            })
                            request_recorded = True
                            if final_usage:
                                # Request accounting includes discarded retries;
                                # this message's usage describes only its last attempt.
                                reports = final_usage.get("provider_attempt_usage")
                                message_usage = reports[-1]["usage"] if reports else final_usage
                                # A server length finish with an exhausted output
                                # budget has the same recovery origin as our local
                                # stop. Use the final attempt, not summed retries.
                                state.output_budget_recovery |= (
                                    event.stop_reason == StopReason.MAX_TOKENS
                                    and message_usage.get("output_tokens", 0) >= current_max_tokens
                                )
                                state.current_message_usage = {
                                    "input_tokens": message_usage.get("input_tokens", 0),
                                    "output_tokens": message_usage.get("output_tokens", 0),
                                    "cache_hit_tokens": message_usage.get("cache_hit_tokens", 0),
                                    "cache_miss_tokens": message_usage.get("cache_miss_tokens", 0),
                                }
                                state.last_stop_reason = str(event.stop_reason.value if hasattr(event.stop_reason, 'value') else event.stop_reason)
                                llm_call_end(final_usage, state.last_stop_reason)
                            else:
                                llm_tracker.stop(completed="ok")
                            if event.stop_reason == StopReason.MAX_TOKENS:
                                is_withheld_max_output = True
                                is_withheld_provider_draft = bool(
                                    (repetition_guard_interrupted or (
                                        reclassified_visible_deliberation
                                        and not current_text.strip()
                                    ))
                                    and not tool_use_blocks
                                )
                                if (
                                    not loop_profile.recovery.compact_after_output_exhaustion
                                    and not is_withheld_provider_draft
                                    and not state.invalid_tool_arguments_recovery
                                ):
                                    # A server length finish remains an output
                                    # failure when usage is absent or rounded.
                                    # Token accounting cannot decide its cause.
                                    state.output_budget_recovery = True

                if visible_deliberation_interrupted:
                    close_stream = getattr(model_stream, "aclose", None)
                    if callable(close_stream):
                        await close_stream()

            except FallbackTriggeredError as fbe:
                if fallback_model and current_model != fallback_model:
                    current_model = fallback_model
                    fallback_bundle = await _resolve_request_prompt_bundle(
                        deps,
                        tools=tools_for_api,
                        model=current_model,
                        permission_mode=effective_permission_mode,
                        fallback=PromptBundle(
                            system_prompt=system_prompt,
                            sections=tuple(system_prompt_sections or ()),
                            user_context=dict(user_context or {}),
                            system_context=dict(system_context or {}),
                        ),
                    )
                    system_prompt = fallback_bundle.system_prompt
                    system_prompt_sections = list(fallback_bundle.sections)
                    user_context = dict(fallback_bundle.user_context)
                    system_context = dict(fallback_bundle.system_context)
                    cwd = str(getattr(deps, "current_cwd", None) or cwd)
                    prompt_parts = [system_prompt]
                    if system_context:
                        prompt_parts = append_system_context(
                            prompt_parts,
                            system_context,
                        )
                    full_system_prompt = "\n\n".join(prompt_parts)
                    pending_request_head = None
                    llm_attempt = True
                    logger.debug(f" fallback triggered: {fbe.original_model} -> {fbe.fallback_model}")
                    yield LoopEvent(
                        type=LoopEventType.TEXT,
                        text=f"\n[Switched to {fallback_model} due to high demand for {fbe.original_model}]\n",
                    )
                    # Reset accumulated state for retry
                    assistant_messages = []
                    tool_use_blocks = []
                    current_text = ""
                    current_reasoning_content = None
                    needs_follow_up = False
                    consecutive_errors = 0
                    provider_failure_seen = False
                    authoritative_terminal_seen = False
                    continue
                raise

            except ImageSizeError:
                
                logger.debug(" IMAGE_ERROR: image processing failed")
                yield _make_terminal_event(
                    TerminalReason.IMAGE_ERROR, state.turn_count, final_usage,
                )
                return

            except (asyncio.CancelledError, concurrent.futures.CancelledError, GeneratorExit):
                if request_dispatched and not request_recorded:
                    # No terminal result will reach the runner. Preserve the
                    # client call in the existing observation ledger; absent
                    # usage stays unknown, including the API attempt count.
                    request_records = getattr(deps, "_request_records", None)
                    if request_records is None:
                        request_records = []
                        deps._request_records = request_records
                    request_records.append({
                        "model": current_model,
                        "latencyMs": int((time.monotonic() - request_started) * 1000),
                        "outcome": "cancelled",
                        "requestUsage": copy.deepcopy(observed_request_usage),
                    })
                raise
            except Exception:
                logger.exception("Unexpected Provider-loop failure")
                consecutive_errors += 1
                if consecutive_errors >= loop_profile.recovery.max_consecutive_model_errors:
                    yield LoopEvent(
                        type=LoopEventType.ERROR,
                        error=RUNTIME_FAILURE,
                    )
                    yield _make_terminal_event(
                        TerminalReason.MODEL_ERROR, state.turn_count, final_usage,
                    )
                    return
                continue

        if consecutive_errors >= loop_profile.recovery.max_consecutive_model_errors:
            # Consume this stream's DONE usage before closing the query. Do
            # not save failed history, execute Tools, or issue another request.
            logger.debug(f" {consecutive_errors} consecutive errors -> MODEL_ERROR")
            if not authoritative_terminal_seen:
                yield _make_terminal_event(
                    TerminalReason.MODEL_ERROR, state.turn_count, final_usage,
                )
            return

        if provider_failure_seen:
            # The Provider client has already exhausted its bounded same-slot
            # policy. Close the query locally when the provider only emitted a
            # raw DONE, instead of letting the TUI synthesize protocol_error or
            # letting generic query recovery issue another request.
            if not authoritative_terminal_seen:
                yield _make_terminal_event(
                    TerminalReason.MODEL_ERROR, state.turn_count, final_usage,
                )
            return

        if visible_deliberation_interrupted:
            elapsed_ms = int((time.monotonic() - request_started) * 1000)
            request_records = getattr(deps, "_request_records", None)
            if request_records is None:
                request_records = []
                deps._request_records = request_records
            request_records.append({
                "model": current_model,
                "latencyMs": elapsed_ms,
                "inputTokens": 0,
                "outputTokens": 0,
                "cacheReadTokens": 0,
                "cacheMissTokens": 0,
                "cacheDetailsSupported": False,
                "interruptedVisibleChars": len(current_text),
                "outcome": "visible_deliberation_recovery",
            })
            interrupted_usage = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_hit_tokens": 0,
                "cache_miss_tokens": 0,
                "provider_attempts": 1,
                "provider_retries": 0,
                "provider_timeouts": 0,
                "cache_details_supported": False,
            }
            yield LoopEvent(
                type=LoopEventType.DONE,
                stop_reason=StopReason.MAX_TOKENS,
                usage=interrupted_usage,
            )
            recovery_text = str(
                loop_profile.recovery
                .visible_deliberation_recovery_message
                or ""
            )
            state.messages[:] = messages_for_query + [{
                "role": "user",
                "content": [{"type": "text", "text": recovery_text}],
                "_is_meta": True,
            }]
            state.visible_deliberation_recovery_count += 1
            state.transition = Transition(
                reason=TransitionReason.VISIBLE_DELIBERATION_RECOVERY,
                detail={
                    "attempt": state.visible_deliberation_recovery_count,
                    "visibleChars": len(current_text),
                },
            )
            llm_tracker.stop(completed="visible_deliberation_recovery")
            continue

        assistant_msg = _build_assistant_msg(
            current_text,
            tool_use_blocks,
            (
                ""
                if tool_use_blocks and current_reasoning_content is None
                else current_reasoning_content
            ),
        )
        # Native reasoning alone is not a completed assistant exchange. This
        # also covers ordinary token-limit responses, not just drafts removed
        # by the repetition guard. Keep reasoning paired with public text or
        # a completed Tool call, but do not replay a standalone private draft.
        # A visible fragment does not turn a failed generation into a valid
        # exchange. Apply the mode policy to body and native reasoning together.
        incomplete_draft = bool(
            is_withheld_max_output
            and not tool_use_blocks
            and not loop_profile.recovery.preserve_incomplete_assistant
        )
        if (current_text.strip() or tool_use_blocks) and not incomplete_draft:
            assistant_messages.append(assistant_msg)

        # A provider ERROR is not a valid assistant completion. Retry through
        # the shared query loop and do not run mode stop hooks against an empty
        # failed response. The third consecutive error is already converted to
        # MODEL_ERROR after consuming the Provider stream above.
        if consecutive_errors and not current_text.strip() and not tool_use_blocks:
            state.transition = Transition(reason=TransitionReason.MODEL_ERROR_RECOVERY)
            continue

        logger.debug(f" tool_uses={len(tool_use_blocks)} "
              f"needs_follow_up={needs_follow_up} withheld_max={is_withheld_max_output}")

        
        if abort_event is not None and abort_event.is_set():
            abort_reason = getattr(abort_event, 'reason', '')
            if abort_reason == 'interrupt':
                logger.debug(f" USER_ABORT after streaming (turn {state.turn_count})")
                state.last_stop_reason = "user_abort"
                yield _make_terminal_event(
                    TerminalReason.USER_ABORT, state.turn_count, final_usage,
                )
            else:
                logger.debug(f" ABORTED after streaming (turn {state.turn_count})")
                state.last_stop_reason = "aborted_streaming"
                yield _make_terminal_event(
                    TerminalReason.ABORTED_STREAMING, state.turn_count, final_usage,
                )
            return

        # ═══ 分叉：needs_follow_up? → ⑤ 工具执行 | → ⑥⑦⑨ return ═══

        if not needs_follow_up:
            # ── ⑥ 错误恢复 (3级) ──

            if (
                is_withheld_max_output
                and not is_withheld_provider_draft
                and not state.invalid_tool_arguments_recovery
                and current_max_tokens < max_tokens_escalated
            ):
                prior_max_tokens = current_max_tokens
                current_max_tokens = max_tokens_escalated
                state.messages[:] = messages_for_query
                state.transition = Transition(reason=TransitionReason.MAX_OUTPUT_TOKENS_ESCALATE)
                state.max_output_tokens_recovery_count = 0
                logger.debug(
                    " [6] max_output_tokens escalate: "
                    f"{prior_max_tokens} -> {max_tokens_escalated}, continue"
                )
                continue

            if (
                is_withheld_max_output
                and state.max_output_tokens_recovery_count
                < max_output_tokens_recovery_limit
            ):
                recovery_msg = _make_recovery_message(
                    state.max_output_tokens_recovery_count + 1,
                    loop_profile,
                )
                state.messages[:] = messages_for_query + assistant_messages + [recovery_msg]
                state.max_output_tokens_recovery_count += 1
                state.transition = Transition(
                    reason=TransitionReason.MAX_OUTPUT_TOKENS_RECOVERY,
                    detail={"attempt": state.max_output_tokens_recovery_count},
                )
                logger.debug(f" [6] max_output_tokens recovery "
                      f"{state.max_output_tokens_recovery_count}/{max_output_tokens_recovery_limit}, continue")
                continue

            if is_withheld_max_output:
                if is_withheld_provider_draft or state.output_budget_recovery or state.invalid_tool_arguments_recovery:
                    # Repetition or a locally enforced output budget is not
                    # evidence of context overflow. The
                    # profile's convergence recovery is exhausted; stop without
                    # an extra summarization request or resetting its budget.
                    yield LoopEvent(
                        type=LoopEventType.ERROR,
                        error=PROVIDER_REQUEST_FAILED,
                        provider_failure=ProviderFailureInfo(
                            reason=("invalid_tool_arguments" if state.invalid_tool_arguments_recovery else
                                    "output_limit" if state.output_budget_recovery else "repeated_output"),
                            retryable_same_slot=False,
                            switch_slot=False,
                        ),
                    )
                    yield _make_terminal_event(
                        TerminalReason.MODEL_ERROR, state.turn_count, final_usage,
                    )
                    return
                # ── ⑥b: collapse_drain (1x) ──
                
                # Gate: skip if previous transition was already collapse_drain_retry.
                prev_reason = state.transition.reason if state.transition else None
                if prev_reason != TransitionReason.COLLAPSE_DRAIN_RETRY:
                    drained = _try_collapse_drain(
                        messages_for_query,
                        model=current_model,
                    )
                    if drained is not None:
                        state.messages[:] = drained
                        state.max_output_tokens_recovery_count = 0
                        state.transition = Transition(
                            reason=TransitionReason.COLLAPSE_DRAIN_RETRY,
                        )
                        logger.debug(" [6b] collapse_drain retry, continue")
                        continue

                # ── ⑥c: reactive_compact (1x) ──
                
                if not state.has_attempted_reactive_compact:
                    try:
                        reactive_compact = await _try_reactive_compact(
                            messages_for_query,
                            current_model,
                            loop_profile.compaction,
                            provider_slot=provider_slot, completion=deps.complete_text,
                            observation_callback=observe_compaction,
                            request_session_id=request_session_id,
                        )
                    except CompactionProviderStopped as stopped:
                        for event in compaction_events:
                            yield event
                        compaction_events.clear()
                        yield LoopEvent(
                            type=LoopEventType.ERROR, error=PROVIDER_REQUEST_FAILED,
                            provider_failure=stopped.provider_failure,
                        )
                        yield _make_terminal_event(
                            TerminalReason.MODEL_ERROR, state.turn_count, final_usage,
                        )
                        return
                    if reactive_compact is not None:
                        _compact_boundary_relink(
                            reactive_compact,
                            deps,
                            user_context or {},
                            system_context or {},
                            effective_permission_mode,
                            assistant_messages,
                            messages_for_query,
                            relink_source_messages=relink_source_messages,
                            cwd=cwd,
                            session_id=session_id,
                            _attachment_turn=_attachment_turn,
                            _loop_profile=loop_profile,
                        )
                        state.messages[:] = reactive_compact.messages
                    for event in compaction_events:
                        yield event
                    compaction_events.clear()
                    if reactive_compact is not None:
                        if deps.compaction_tick:
                            deps.compaction_tick(
                                "Compacting (recovery)",
                                (
                                    f"{reactive_compact.pre_compact_tokens} "
                                    "tokens -> summary"
                                ),
                            )
                        state.has_attempted_reactive_compact = True
                        state.max_output_tokens_recovery_count = 0
                        state.transition = Transition(
                            reason=TransitionReason.REACTIVE_COMPACT_RETRY,
                        )
                        logger.debug(" [6c] reactive_compact retry, continue")
                        continue

                # ── ⑥d: all recovery exhausted → prompt_too_long ──
                logger.debug(" [6d] recovery exhausted → prompt_too_long")
                yield _make_terminal_event(
                    TerminalReason.PROMPT_TOO_LONG, state.turn_count, final_usage,
                )
                return

            
            if "assistant_stop" in loop_profile.stop_hook.boundaries:
                stop_result = await _run_profile_stop_hooks(
                    loop_profile,
                    deps,
                    messages_for_query=messages_for_query,
                    assistant_messages=assistant_messages,
                    tool_use_context=state.tool_use_context,
                    system_prompt=full_system_prompt,
                    user_context=user_context,
                    system_context=system_context,
                    cwd=cwd,
                    model=current_model,
                    assistant_text=current_text,
                    tool_use_blocks=tool_use_blocks,
                )

                if stop_result.prevent_continuation:
                    yield _make_terminal_event(
                        TerminalReason.STOP_HOOK_PREVENTED,
                        state.turn_count,
                        final_usage,
                    )
                    return
                if stop_result.blocking_errors:
                    state.messages[:] = (
                        messages_for_query
                        + assistant_messages
                        + stop_result.blocking_errors
                    )
                    state.transition = Transition(
                        reason=TransitionReason.STOP_HOOK_BLOCKING,
                    )
                    logger.debug(" [7] stop hooks blocked — retrying")
                    continue

            
            if deps.budget_tracker is not None and deps.budget_tracker.budget_total:
                if final_usage:
                    state.budget_global_turn_tokens += final_usage.get("output_tokens", 0)
                decision = deps.budget_tracker.check(state.budget_global_turn_tokens)
                if decision.get("action") == "continue":
                    nudge = decision.get("nudge_message", "")
                    if nudge:
                        state.messages[:] = (
                            messages_for_query + assistant_messages
                            + [{
                                "role": "user",
                                "content": [{"type": "text", "text": nudge}],
                                "_is_meta": True,
                            }]
                        )
                        state.transition = Transition(reason=TransitionReason.TOKEN_BUDGET_CONTINUATION)
                        logger.debug(
                            f" [7b] token_budget_continuation: pct={decision.get('pct')}%, "
                            f"continuation_count={decision.get('continuation_count')}, continue"
                        )
                        continue
                # action='stop' → COMPLETED

            # 正常完成
            state.messages[:] = messages_for_query + assistant_messages
            yield _make_terminal_event(
                TerminalReason.COMPLETED, state.turn_count, final_usage,
            )
            return

        # ═══ ⑤ 工具执行 ═══
        logger.debug(f" [5] execute {len(tool_use_blocks)} tools...")
        tool_results: list[dict] = []
        submit_ended = False

        # Auto-recover blocked tools: if model switched to different tools, clear blocks
        if deps.loop_detector is not None:
            deps.loop_detector.auto_recover_block([tname for _, tname, _ in tool_use_blocks])

        for tid, tname, targs in tool_use_blocks:
            tool_permission_mode = (
                loop_profile.tools.resolve_permission_mode(
                    permission_context,
                    permission_mode,
                )
            )
            session_cwd = loop_profile.tools.resolve_cwd(cwd)
            if session_cwd != cwd:
                cwd = session_cwd
                deps.current_cwd = cwd
            permission_spec = permission_specs.get(
                tname,
                ToolPermissionSpec(),
            )
            unavailable_outcome = None
            if submit_ended:
                # A trusted lifecycle tool has finished the user's request.
                # Pair every remaining call without asking permission or
                # executing work from the now-closed request.
                unavailable_outcome = ToolExecutionOutcome(
                    value="Not executed: the request was ended by the preceding tool.",
                    is_error=True,
                    metadata={"not_executed": True},
                )
                allowed = False
                needs_ask = False
            elif tname not in final_surface_names:
                unavailable_outcome = tool_unavailable_outcome()
                allowed = False
                reason = "Tool unavailable on final Provider surface"
                needs_ask = False
            elif (
                tname not in loop_profile.tools.loop_block_exempt_tools
                and deps.loop_detector is not None
                and deps.loop_detector.is_blocked(tname)
            ):
                allowed = False
                reason = f"tool '{tname}' blocked by loop detector (use a different tool)"
                needs_ask = False
                checkpoint("permission", tool=tname, allowed=False, reason=reason[:80])
            else:
                # ── ⑧ 权限检查 ──
                allowed, reason, needs_ask = _check_tool_permission(
                    tname, targs, permission_spec, permission_context,
                    tool_permission_mode, cwd,
                )
                checkpoint("permission", tool=tname, allowed=allowed, reason=reason[:80])

            if needs_ask and ask_callback is not None:
                try:
                    decision = await ask_callback(tname, targs, reason)
                    if isinstance(decision, bool):
                        allowed = decision
                    elif hasattr(decision, 'behavior'):
                        allowed = decision.behavior == 'allow'
                    else:
                        allowed = str(decision).lower().startswith('y')
                except Exception:
                    allowed = False
                if not allowed:
                    reason = f"denied by user: {reason}"

            if unavailable_outcome is not None:
                outcome = unavailable_outcome
                result = outcome.value
                is_error = outcome.is_error
                result_metadata = dict(outcome.metadata)
            elif allowed:
                outcome = loop_profile.tools.execute(ToolInvocationContext(
                    name=tname,
                    arguments=targs,
                    handlers=iteration_handlers,
                    messages=messages_for_query,
                    system_prompt=full_system_prompt,
                    cwd=cwd,
                    permission_context=permission_context,
                    ask_callback=ask_callback,
                    permission_mode=tool_permission_mode,
                    model=current_model,
                    permission_spec=permission_spec,
                    error_policy=loop_profile.tools.error_policy,
                ))
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                result = outcome.value
                is_error = outcome.is_error
                result_metadata = dict(outcome.metadata)
            else:
                result = f"Permission denied: {reason}"
                is_error = True
                result_metadata = {}

            checkpoint("tool_exec", tool=tname, is_error=is_error,
                       result_len=len(str(result)))

            yield LoopEvent(
                type=LoopEventType.TOOL_RESULT,
                tool_result=ToolResultBlock(
                    tool_use_id=tid,
                    content=str(result),
                    is_error=is_error,
                    metadata=result_metadata,
                ),
            )
            tool_results.append({
                "role": "user",
                "type": "tool_result",
                "tool_use_id": tid,
                "content": result,
                "is_error": is_error,
                "metadata": result_metadata,
                "_timestamp": time.time(),
            })
            submit_ended = submit_ended or result_metadata.get("end_submit") is True
            if result_metadata.get("outcome_indeterminate") is True:
                # The handler started but its durable outcome is unknown.
                # Preserve only the stable public result, then stop before
                # Stop Hooks, Attachments, or another Provider request.
                state.messages[:] = (
                    messages_for_query + assistant_messages + tool_results
                )
                yield _make_terminal_event(
                    TerminalReason.TOOL_OUTCOME_INDETERMINATE,
                    state.turn_count,
                    final_usage,
                )
                return
        logger.debug(f" tools done, {len(tool_results)} results")

        if submit_ended:
            # Close at the result boundary, before summaries, hooks, or a
            # fresh Provider request. Keep the complete tool history for
            # persistence and the next user-initiated request.
            state.messages[:] = messages_for_query + assistant_messages + tool_results
            yield _make_terminal_event(
                TerminalReason.COMPLETED, state.turn_count, final_usage,
            )
            return

        # ── HOOK_STOPPED check: 工具钩子返回 hook_stopped_continuation → 终止 ──
        
        for tr in tool_results:
            content = str(tr.get("content", ""))
            if "hook_stopped_continuation" in content:
                logger.debug(" HOOK_STOPPED detected in tool result")
                yield _make_terminal_event(
                    TerminalReason.HOOK_STOPPED, state.turn_count, final_usage,
                )
                return

        if (
            callable(loop_profile.tools.stop_after_result)
            and loop_profile.tools.stop_after_result()
        ):
            terminal_messages = (
                messages_for_query + assistant_messages + tool_results
            )
            if "tool_result_stop" in loop_profile.stop_hook.boundaries:
                stop_result = await _run_profile_stop_hooks(
                    loop_profile,
                    deps,
                    messages_for_query=terminal_messages,
                    assistant_messages=[],
                    tool_use_context=state.tool_use_context,
                    system_prompt=full_system_prompt,
                    user_context=user_context,
                    system_context=system_context,
                    cwd=cwd,
                    model=current_model,
                    assistant_text=current_text,
                    tool_use_blocks=tool_use_blocks,
                )
                if stop_result.prevent_continuation:
                    state.messages[:] = terminal_messages
                    yield _make_terminal_event(
                        TerminalReason.STOP_HOOK_PREVENTED,
                        state.turn_count,
                        final_usage,
                    )
                    return
                if stop_result.blocking_errors:
                    state.messages[:] = terminal_messages + stop_result.blocking_errors
                    state.transition = Transition(
                        reason=TransitionReason.STOP_HOOK_BLOCKING,
                    )
                    continue
            state.messages[:] = terminal_messages
            yield _make_terminal_event(
                TerminalReason.COMPLETED, state.turn_count, final_usage,
            )
            return

        
        # Runs in background; result is yielded at the start of next turn.
        last_assistant_text = current_text if current_text.strip() else ""
        if tool_use_blocks and loop_profile.output.enable_tool_use_summary:
            state.pending_tool_use_summary = asyncio.create_task(
                _generate_tool_use_summary(
                    [(tid, tname, targs) for tid, tname, targs in tool_use_blocks],
                    tool_results,
                    assistant_text=last_assistant_text,
                    model=current_model,
                )
            )

        
        if abort_event is not None and abort_event.is_set():
            abort_reason = getattr(abort_event, 'reason', '')
            if abort_reason == 'interrupt':
                logger.debug(f" USER_ABORT during tool execution (turn {state.turn_count})")
                state.last_stop_reason = "user_abort"
                yield _make_terminal_event(
                    TerminalReason.USER_ABORT, state.turn_count, final_usage,
                )
            else:
                logger.debug(f" ABORTED during tool execution (turn {state.turn_count})")
                state.last_stop_reason = "aborted_tools"
                yield _make_terminal_event(
                    TerminalReason.ABORTED_TOOLS, state.turn_count, final_usage,
                )
            return


        # ── 死循环检测（渐进式干预）──
        loop_alert = _check_loop(deps, tool_use_blocks, tool_results)
        intervention = loop_profile.tools.apply_loop_intervention or _apply_loop_intervention
        should_terminate = intervention(
            deps, loop_alert, tool_use_blocks, tool_results,
        )
        if should_terminate:
            # Preserve the assistant tool call, authoritative results, and the
            # critical injection before returning MAX_TURNS.  Game runtimes may
            # grant a fresh submit for recovery; dropping this window makes an
            # exact retry attachment refer to history that no longer exists.
            state.messages[:] = messages_for_query + assistant_messages + tool_results
            yield _make_terminal_event(
                TerminalReason.MAX_TURNS, state.turn_count, final_usage,
            )
            return

        # ═══ ⑩ getAttachmentMessages — 统一 Attachment 收集 ═══

        if attachment_iteration is not None:
            provider_context = ProviderContext(
                profile=_attachment_turn.profile,
                messages=messages_for_query,
                tool_uses=tool_use_blocks,
                deps=deps,
                state=state,
                tool_use_context=state.tool_use_context,
                cwd=cwd,
                model=current_model,
                permission_mode=loop_profile.tools.resolve_permission_mode(
                    permission_context,
                    permission_mode,
                ),
                phase="post_tool",
            )
            pipeline_messages = await attachment_iteration.collect_after_tools(
                provider_context,
            )
            tool_results.extend(pipeline_messages)
            if pipeline_messages:
                logger.debug(
                    " [10] code attachments: %s injected",
                    len(pipeline_messages),
                )
                observer = loop_profile.output.on_attachments_appended
                if observer is not None:
                    observed = observer(deps, pipeline_messages)
                    if inspect.isawaitable(observed):
                        await observed

        # ═══ ⑪ 组装下一轮 ═══
        next_turn = state.turn_count + 1
        if next_turn > max_turns:
            terminal_messages = (
                messages_for_query + assistant_messages + tool_results
            )
            if "max_turn" in loop_profile.stop_hook.boundaries:
                stop_result = await _run_profile_stop_hooks(
                    loop_profile,
                    deps,
                    messages_for_query=terminal_messages,
                    assistant_messages=[],
                    tool_use_context=state.tool_use_context,
                    system_prompt=full_system_prompt,
                    user_context=user_context,
                    system_context=system_context,
                    cwd=cwd,
                    model=current_model,
                    assistant_text=current_text,
                    tool_use_blocks=tool_use_blocks,
                )
                if stop_result.prevent_continuation:
                    state.messages[:] = terminal_messages
                    yield _make_terminal_event(
                        TerminalReason.STOP_HOOK_PREVENTED,
                        state.turn_count,
                        final_usage,
                    )
                    return
                if stop_result.blocking_errors:
                    # This is the profile-owned final-turn extra round. It uses
                    # the same Stop Hook slot instead of a runtime re-submit.
                    state.messages[:] = terminal_messages + stop_result.blocking_errors
                    state.turn_count = next_turn
                    state.transition = Transition(
                        reason=TransitionReason.STOP_HOOK_BLOCKING,
                    )
                    logger.debug(" [7] final-boundary hook granted one continuation")
                    continue
            state.messages[:] = terminal_messages
            logger.debug(f" max_turns ({max_turns}) -> MAX_TURNS")
            yield _make_terminal_event(
                TerminalReason.MAX_TURNS, state.turn_count, final_usage,
            )
            return

        state.messages[:] = messages_for_query + assistant_messages + tool_results
        state.turn_count = next_turn
        state.max_output_tokens_recovery_count = 0
        state.output_budget_recovery = False
        state.invalid_tool_arguments_recovery = False
        state.has_attempted_reactive_compact = False
        state.transition = Transition(reason=TransitionReason.NEXT_TURN)
        consecutive_errors = 0

        # Incremental save — every configured runner uses the same transcript
        # path, including game teammates. Mode changes prompt/hooks, not history.
        if session_id:
            try:
                from bglab.persistence import save_transcript as _st
                _st(state.messages, cwd=cwd, session_id=session_id)
                logger.debug(f" [save] turn {next_turn} — {len(state.messages)} msgs")
            except Exception:
                pass

        logger.debug(f" [11] continue -> turn {next_turn} (transition=next_turn)")


# ══════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════

def _default_deps() -> QueryDeps:
    from bglab.llm.client import call_model as _call_model, complete_text
    from bglab.engine.stubs import BudgetTracker
    return QueryDeps(
        call_model=_call_model,
        complete_text=complete_text,
        compact_tracker=CompactTracker(),
        loop_detector=LoopDetector(),
        stop_hooks_state=StopHooksState(),
        budget_tracker=BudgetTracker(),
    )


async def _run_profile_stop_hooks(
    profile: QueryLoopProfile,
    deps: QueryDeps,
    *,
    messages_for_query: list[dict[str, Any]],
    assistant_messages: list[dict[str, Any]],
    tool_use_context: Any,
    system_prompt: str,
    user_context: dict[str, str] | None,
    system_context: dict[str, str] | None,
    cwd: str,
    model: str,
    assistant_text: str,
    tool_use_blocks: list[tuple[str, str, dict]],
):
    """Invoke the complete Stop Hook runner selected by the Profile."""

    result = profile.stop_hook.run(
        deps=deps,
        messages_for_query=messages_for_query,
        assistant_messages=assistant_messages,
        tool_use_context=tool_use_context,
        system_prompt=system_prompt,
        user_context=user_context,
        system_context=system_context,
        cwd=cwd,
        model=model,
        assistant_text=assistant_text,
        tool_use_blocks=tool_use_blocks,
    )
    if inspect.isawaitable(result):
        result = await result
    return result


def _get_messages_after_compact_boundary(messages: list[dict]) -> list[dict]:
    """Find last compact_boundary marker, return messages from it onward.
    Aligns with utils/messages.ts getMessagesAfterCompactBoundary().
    Boundary itself is a meta message — API-sent but filtered downstream."""
    for i in range(len(messages) - 1, -1, -1):
        cb = messages[i].get("_compact_boundary")
        if isinstance(cb, dict):
            return messages[i:]
    return messages


def _compact_boundary_relink(result, deps, user_context, system_context,
                              permission_mode: str,
                              assistant_messages: list[dict],
                              messages_for_query: list[dict],
                              relink_source_messages: list[dict] | None = None,
                              cwd: str = "",
                              session_id: str | None = None,
                              *,
                              _attachment_turn: AttachmentTurn,
                              _loop_profile: QueryLoopProfile) -> None:
    """Post-compact relink: capture state, flush transcript, inject attachments.
    Aligns with compact.ts post-compact flow:
      1. build relink dict (5 items)
      2. embed into boundary marker → survives JSONL roundtrip
      3. flush transcript (H1) → preserved tail on disk before next yield
      4. inject attachment messages from relink (H3) → LLM sees them
    """
    if result is None:
        return

    relink = dict(
        _loop_profile.relink.build(
            deps,
            permission_mode,
            [
                *(relink_source_messages or messages_for_query),
                *assistant_messages,
            ],
        )
    )
    # Boundary-scoped IDs allow the same typed lifecycle fact to be restored
    # again after a later compaction without being swallowed as a duplicate.
    boundary = getattr(result, "boundary_marker", None)
    if isinstance(boundary, dict):
        relink["_compact_id"] = str(
            boundary.get("timestamp")
            or boundary.get("pre_compact_tokens")
            or id(result)
        )

    deps.compact_relink = relink
    invalidate_prompt_context = getattr(
        deps,
        "invalidate_prompt_context",
        None,
    )
    if callable(invalidate_prompt_context):
        invalidate_prompt_context("compaction")

    # Embed into boundary marker so it survives transcript save/load
    if result.messages:
        first = result.messages[0]
        if isinstance(first, dict) and "_compact_boundary" in first:
            cb = first["_compact_boundary"]
            if isinstance(cb, dict):
                cb["relink"] = relink

        # H3: profile-owned typed relink values are appended after the compact
        # boundary, summary, and preserved tail.
        post_context = ProviderContext(
            profile=_attachment_turn.profile,
            messages=messages_for_query,
            deps=deps,
            state=None,
            phase="post_compact",
            local={"compact_relink": relink},
        )
        attachments = _attachment_turn.collect_post_compact(post_context)
        if attachments:
            result.messages[:] = [*result.messages, *attachments]

    # H1: flush transcript immediately so preserved tail + boundary are on disk
    # before the next yield. If process crashes mid-turn, resume sees the boundary.
    try:
        from bglab.persistence import save_transcript as _persist
        _persist(result.messages, cwd=cwd or None, session_id=session_id)
    except Exception:
        pass  # fire-and-forget; errors don't block the agent loop


def _make_terminal_event(
    reason: TerminalReason, turn_count: int, usage: dict[str, int] | None = None,
) -> LoopEvent:
    return LoopEvent(
        type=LoopEventType.DONE,
        stop_reason=StopReason.END_TURN,
        usage=usage,
        terminal=TerminalInfo(
            reason=reason.value,
            turn_count=turn_count,
            total_usage=usage,
        ),
    )


def _make_recovery_message(
    attempt: int,
    profile: QueryLoopProfile,
) -> dict:
    return {
        "role": "user",
        "content": [{
            "type": "text",
            "text": profile.recovery.max_output_recovery_message(attempt),
        }],
        "_is_meta": True,
    }


def _check_tool_permission(
    tool_name: str, tool_input: dict,
    permission_spec: ToolPermissionSpec,
    permission_context, permission_mode: str, cwd: str = "",
) -> tuple[bool, str, bool]:
    """Returns (allowed, reason, needs_ask).
    needs_ask=True means the user should be prompted (interactive only)."""
    from bglab.engine.query_profiles import _code_permission_mode

    effective_mode = _code_permission_mode(
        permission_context,
        permission_mode,
    )

    from bglab.permissions.checker import evaluate_tool_permission
    from bglab.permissions.types import PermissionBehavior as PB

    decision = evaluate_tool_permission(
        tool_name,
        tool_input,
        permission_spec,
        permission_context,
        permission_mode=effective_mode,
        project_cwd=cwd,
    )
    if decision.behavior == PB.ALLOW:
        return True, decision.reason, False
    if decision.behavior == PB.ASK:
        return False, decision.reason, True  # needs_ask=True
    return False, decision.reason, False


def _check_loop(deps: QueryDeps, tool_use_blocks, tool_results) -> Any:
    """死循环检测。逐工具检测，返回最严重 alert (含 hint/warning/critical)。"""
    from bglab.loop_detector.detector import LoopSeverity
    detector = deps.loop_detector
    if detector is None:
        return _NoLoopAlert()
    severity_order = {LoopSeverity.CRITICAL: 3, LoopSeverity.WARNING: 2, LoopSeverity.HINT: 1, LoopSeverity.NONE: 0}
    most_severe: Any = _NoLoopAlert()
    for (tid, tname, targs), tr in zip(tool_use_blocks, tool_results):
        alert = detector.check(tname, targs, tr.get("content", ""))
        s = getattr(alert, "severity", "none")
        if severity_order.get(s, 0) >= severity_order[LoopSeverity.CRITICAL]:
            return alert
        if severity_order.get(s, 0) > severity_order.get(getattr(most_severe, "severity", "none"), 0):
            most_severe = alert
    return most_severe


def _apply_loop_intervention(
    deps: QueryDeps, loop_alert: Any, tool_use_blocks: list, tool_results: list,
) -> bool:
    """渐进式干预。返回 True 表示应该终止循环。

    HINT:   注入轻提示 → 模型继续
    WARNING: 注入强提示 + block 被检测工具 → 下轮拒绝该工具
    CRITICAL: 记录日志，返回 True（外层 yield terminal）
    """
    from bglab.loop_detector.detector import LoopSeverity

    severity = getattr(loop_alert, "severity", "none")
    detector_name = getattr(loop_alert, "detector", "none")
    message = getattr(loop_alert, "message", "")
    tool_name = getattr(loop_alert, "tool_name", "")

    if severity == LoopSeverity.CRITICAL:
        loop_detector_result(detector_name, "critical", tool_name)
        logger.debug(f" LOOP CRITICAL: {detector_name} — {message}")
        injection = {
            "role": "user",
            "content": [{"type": "text", "text": (
                "<system-reminder>\n"
                "CRITICAL: The loop detector determined you are stuck in a repetitive loop.\n"
                f"Reason: {message}\n"
                "This agent run will now stop. Summarize your current progress for the user.\n"
                "</system-reminder>"
            )}],
            "_is_meta": True,
        }
        tool_results.append(injection)
        return True  # signal caller to terminate

    if severity == LoopSeverity.WARNING:
        loop_detector_result(detector_name, "warning", tool_name)
        logger.debug(f" LOOP WARNING: {detector_name} — {message}")
        # Block ALL tools in tool_use_blocks (not just the detected one)
        for _, tname, _ in tool_use_blocks:
            deps.loop_detector.block_tool(tname)
            logger.debug(f"   blocked: {tname}")
        injection = {
            "role": "user",
            "content": [{"type": "text", "text": (
                "<system-reminder>\n"
                "You are repeating the same tool calls without making progress.\n"
                f"Details: {message}\n"
                "The tool(s) in your last call have been disabled for the next turn.\n"
                "You MUST now pick a different tool to advance or summarize your findings.\n"
                "</system-reminder>"
            )}],
            "_is_meta": True,
        }
        tool_results.append(injection)
        return False

    if severity == LoopSeverity.HINT:
        loop_detector_result(detector_name, "hint", tool_name)
        logger.debug(f" LOOP HINT: {detector_name} — {message}")
        injection = {
            "role": "user",
            "content": [{"type": "text", "text": (
                "<system-reminder>\n"
                f"Hint: {message}\n"
                "Consider trying a different approach to avoid getting stuck in a loop.\n"
                "</system-reminder>"
            )}],
            "_is_meta": True,
        }
        tool_results.append(injection)
        return False

    return False


class _NoLoopAlert:
    severity = "none"
    detector = "none"
    message = ""


def _build_assistant_msg(
    text: str,
    tool_uses: list[tuple[str, str, dict]],
    reasoning_content: str | None = None,
) -> dict:
    blocks: list[dict] = []
    if reasoning_content is not None:
        blocks.append({"type": "reasoning", "text": reasoning_content})
    if text.strip():
        blocks.append({"type": "text", "text": text})
    for tid, tname, targs in tool_uses:
        blocks.append({
            "type": "tool_use",
            "id": tid,
            "name": tname,
            "input": targs,
        })
    return {
        "role": "assistant",
        "content": blocks,
        "_timestamp": time.time(),
    }


def _extract_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user" and not msg.get("_is_meta"):
            # Skip tool_result messages — their content is a raw string, not user input
            if msg.get("type") == "tool_result":
                continue
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return block.get("text", "")
            if isinstance(content, str):
                return content
    return ""


def _capture_for_show(
    *,
    system_prompt: str,
    system_prompt_sections: list[str],
    user_context: dict[str, str],
    system_context: dict[str, str],
    messages: list[dict],
    tools: list,
    model: str,
    turn_count: int,
) -> None:
    """Capture complete assembled prompt for /show command. Always captures."""
    viewer.capture(
        system_prompt=system_prompt,
        system_prompt_sections=system_prompt_sections,
        user_context=user_context,
        system_context=system_context,
        messages=messages,
        tools=tools,
        model=model,
        turn_count=turn_count,
    )


# ══════════════════════════════════════════════

# ══════════════════════════════════════════════

_TOOL_USE_SUMMARY_SYSTEM_PROMPT = (
    "Write a short summary label describing what these tool calls accomplished. "
    "It appears as a single-line row and truncates around 30 characters, "
    "so think git-commit-subject, not sentence. "
    "Keep the verb in past tense and the most distinctive noun. "
    "Drop articles, connectors, and long location context first."
)

async def _generate_tool_use_summary(
    tool_uses: list[tuple[str, str, dict]],
    tool_results: list[dict],
    assistant_text: str = "",
    model: str = "deepseek-chat",
) -> str | None:
    """Generate a human-readable summary of completed tool calls.

    Fire-and-forget — errors are silently ignored.
    Aligns with: toolUseSummaryGenerator.ts generateToolUseSummary()
    """
    if not tool_uses:
        return None
    try:
        tool_info_parts: list[str] = []
        for (tid, tname, targs), tr in zip(tool_uses, tool_results):
            input_str = str(targs)[:300]
            output_str = str(tr.get("content", ""))[:300]
            tool_info_parts.append(f"Tool: {tname}\nInput: {input_str}\nOutput: {output_str}")

        context_prefix = ""
        if assistant_text:
            context_prefix = f"User's intent (from assistant): {assistant_text[:200]}\n\n"

        from bglab.llm.client import complete_text
        text = await complete_text(
            model=model,
            system_prompt=_TOOL_USE_SUMMARY_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": (
                    f"{context_prefix}Tools completed:\n\n"
                    + "\n\n".join(tool_info_parts)
                    + "\n\nLabel:"
                )},
            ],
            temperature=0.0,
            max_tokens=64,
        )
        return text.strip() or None
    except Exception:
        return None


# ══════════════════════════════════════════════

# ══════════════════════════════════════════════

def _try_collapse_drain(messages: list[dict], model: str = "deepseek-chat") -> list[dict] | None:
    ""
    from bglab.compaction.autocompact import drain_collapses
    collapsed, saved = drain_collapses(messages, model=model)
    if saved > 0:
        return collapsed
    return None


async def _try_reactive_compact(
    messages: list[dict], model: str, compaction_profile: Any,
    *, provider_slot: Any = None, completion: Any = None, observation_callback: Any = None,
    request_session_id: str | None = None,
) -> Any:
    ""
    from bglab.compaction.autocompact import full_compact
    try:
        result = await full_compact(
            messages,
            model=model,
            profile=compaction_profile,
            provider_slot=provider_slot, completion=completion,
            observation_callback=observation_callback,
            request_session_id=request_session_id,
        )
        if result and result.messages:
            return result
    except CompactionProviderStopped:
        raise
    except Exception:
        pass
    return None


def _compaction_observation(
    observation: CompletionObservation, provider_slot: Any,
) -> tuple[dict[str, Any], LoopEvent]:
    """Separate auxiliary accounting from the decision response state machine."""
    usage = observation.usage
    def token(key: str) -> int | None:
        return None if usage is None else int(usage.get(key, 0) or 0)

    record = {
        "kind": "compaction", "model": observation.model,
        "providerSlot": getattr(provider_slot, "id", None),
        "status": observation.status, "usageAvailable": usage is not None,
        "latencyMs": int(observation.elapsed_ms),
        "inputTokens": token("input_tokens"), "outputTokens": token("output_tokens"),
        "cacheReadTokens": token("cache_hit_tokens"), "cacheMissTokens": token("cache_miss_tokens"),
        "cacheDetailsSupported": bool((usage or {}).get("cache_details_supported", False)),
        "providerAttempts": observation.provider_attempts,
        "providerTimeouts": int(bool(observation.provider_failure and observation.provider_failure.reason == "timeout")),
        "providerFailure": asdict(observation.provider_failure) if observation.provider_failure else None,
    }
    event_usage = {**(usage or {}), "provider_attempts": observation.provider_attempts,
        "provider_timeouts": record["providerTimeouts"],
        "usage_missing_requests": int(usage is None)}
    return record, LoopEvent(type=LoopEventType.DONE, usage=event_usage, request_purpose="compaction")
