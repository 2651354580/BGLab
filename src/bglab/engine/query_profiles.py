"""Profiles for the fixed unified query loop.

The loop owns one sequence of lifecycle positions.  A profile may choose the
content and trigger policy used at those positions, but it cannot add another
query path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable

from bglab.compaction.autocompact import (
    CODE_COMPACTION_PROFILE,
    CompactionProfile,
    apply_context_collapse,
    should_collapse,
)
from bglab.compaction.budget import apply_tool_result_budget
from bglab.compaction.microcompact import microcompact_messages
from bglab.compaction.snip import snip_if_needed
from bglab.engine.attachments import AttachmentProfile
from bglab.engine.attachments.profiles import CODE_ATTACHMENT_PROFILE
from bglab.engine.prompt_profiles import CODE_PROMPT_PROFILE, PromptProfile
from bglab.engine.error_policy import (
    DEFAULT_TOOL_ERROR_POLICY,
    ToolErrorPolicy,
)
from bglab.engine.tool_execution import execute_code_tool
from bglab.engine.turn_input import TurnInput, normalize_turn_input


CompactRelinkBuilder = Callable[[Any, str, list[dict[str, Any]]], dict[str, Any]]
AttachmentAppendObserver = Callable[[Any, list[dict[str, Any]]], Any]
StopAfterToolResult = Callable[[], bool]
ToolSurfaceAdapter = Callable[[list[Any], Any, str], list[Any]]
ToolChoiceResolver = Callable[[list[dict[str, Any]], list[Any]], str | dict | None]
ToolExecutor = Callable[[Any], Any]
PermissionModeResolver = Callable[[Any, str], str]
CwdResolver = Callable[[str], str]
RecoveryMessageBuilder = Callable[[int], str]
StopHookRunner = Callable[..., Any]
ContextShapingAdapter = Callable[[list[dict[str, Any]], str], Any]
InitialMemoryAdapter = Callable[[Any, Any], Any]
TurnMessageBuilder = Callable[[TurnInput | str], dict[str, Any]]


def _successful_tool_calls(
    messages: list[dict[str, Any]],
) -> list[tuple[str, dict[str, Any], str]]:
    results: dict[str, tuple[bool, str]] = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("type") != "tool_result":
            continue
        tool_id = str(message.get("tool_use_id", ""))
        if tool_id:
            results[tool_id] = (
                not bool(message.get("is_error", False)),
                str(message.get("content", "")),
            )
    calls: list[tuple[str, dict[str, Any], str]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool_id = str(block.get("id", ""))
            ok, result = results.get(tool_id, (False, ""))
            if ok:
                calls.append((
                    str(block.get("name", "")),
                    dict(block.get("input", {})),
                    result,
                ))
    return calls


def build_code_compact_relink(
    deps: Any,
    permission_mode: str,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    ""
    calls = _successful_tool_calls(messages)
    skill_names = list(dict.fromkeys(
        str(arguments.get("skill", ""))
        for name, arguments, result in calls
        if name == "Skill"
        and str(arguments.get("skill", "")).strip()
        and " loaded" in result
    ))
    skill_bodies: list[dict[str, str]] = []
    if skill_names:
        from bglab.skills.base import get_file_skill

        for skill_name in skill_names:
            skill = get_file_skill(skill_name)
            if not isinstance(skill, dict):
                continue
            skill_bodies.append({
                "name": skill_name,
                "path": str(skill.get("_source_path") or skill.get("_source_dir") or ""),
                "body": str(skill.get("body", ""))[:8_000],
            })
    agent_calls = [
        {
            "description": str(arguments.get("description", ""))[:200],
            "type": str(arguments.get("subagent_type") or "fork"),
            "background": bool(arguments.get("run_in_background", False)),
        }
        for name, arguments, _result in calls
        if name == "Agent"
    ][-8:]
    task_state = next((
        result[:8_000]
        for name, _arguments, result in reversed(calls)
        if name in {"TaskList", "TodoWrite", "TaskUpdate"}
    ), "")
    assembly = getattr(deps, "_last_assembly", None)
    deferred_tools = [
        str(entry.get("name", ""))
        for entry in getattr(getattr(deps, "deferred_registry", None), "_catalog", [])
        if isinstance(entry, dict) and str(entry.get("name", "")).strip()
    ] if getattr(assembly, "activated", False) else []

    return {
        # CLAUDE.md and user context stay in the request-local session head.
        "claude_md": "",
        "skill_names": skill_names,
        "skill_bodies": skill_bodies,
        "session_meta": {
            "title": getattr(deps, "session_title", ""),
            "labels": getattr(deps, "session_labels", []),
        },
        "permission_mode": permission_mode,
        "agent_calls": agent_calls,
        "task_state": task_state,
        "deferred_tools": deferred_tools,
    }


def _code_tool_surface(
    tools: list[Any],
    permission_context: Any,
    permission_mode: str,
) -> list[Any]:
    """Apply Code-only permission visibility outside the shared loop."""

    from bglab.tools.base import filter_tools_by_deny_rules, filter_tools_by_mode

    effective_mode = _code_permission_mode(
        permission_context,
        permission_mode,
    )
    visible = filter_tools_by_mode(list(tools), effective_mode)
    return filter_tools_by_deny_rules(visible, permission_context)


def _code_permission_mode(permission_context: Any, fallback: str) -> str:
    """Return the single trusted Code permission mode for this query.

    A typed ``PermissionContext`` is an explicit authority object and must not
    be rewritten from process-global session state.  The global state remains
    a compatibility source only for callers that did not supply that object.
    """

    from bglab.permissions.types import PermissionContext, PermissionMode

    if isinstance(permission_context, PermissionContext):
        mode = permission_context.mode
        return mode.value if isinstance(mode, PermissionMode) else str(mode)

    from bglab.session.state import session_state

    return str(session_state.permission_mode or fallback)


def _code_cwd(fallback: str) -> str:
    from bglab.session.state import session_state

    return str(getattr(session_state, "cwd", "") or fallback)


def identity_permission_mode(_permission_context: Any, fallback: str) -> str:
    return fallback


def identity_cwd(fallback: str) -> str:
    return fallback


@dataclass(frozen=True)
class ToolProfile:
    """Final model-visible Tool surface and execution policy."""

    name: str
    resolve_surface: ToolSurfaceAdapter
    execute: ToolExecutor
    resolve_permission_mode: PermissionModeResolver
    resolve_cwd: CwdResolver
    error_policy: ToolErrorPolicy = DEFAULT_TOOL_ERROR_POLICY
    loop_block_exempt_tools: frozenset[str] = frozenset()
    apply_loop_intervention: Callable[[Any, Any, list, list], bool] | None = None
    stop_after_result: StopAfterToolResult | None = None
    resolve_request_tool_choice: ToolChoiceResolver | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool profile name must be non-empty")


@dataclass(frozen=True)
class StopHookProfile:
    name: str
    run: StopHookRunner
    boundaries: frozenset[str] = frozenset({"assistant_stop"})

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("stop hook profile name must be non-empty")
        unknown = self.boundaries - {
            "assistant_stop", "tool_result_stop", "max_turn",
        }
        if unknown:
            raise ValueError(f"unknown stop-hook boundary: {sorted(unknown)}")


@dataclass(frozen=True)
class OutputProfile:
    enable_tool_use_summary: bool = True
    on_attachments_appended: AttachmentAppendObserver | None = None


@dataclass(frozen=True)
class ContextShapingResult:
    messages: list[dict[str, Any]]
    snip_tokens_freed: int = 0
    microcompact_tokens_freed: int = 0
    collapse_tokens_saved: int = 0
    collapse_owns_headroom: bool = False


@dataclass(frozen=True)
class ContextShapingProfile:
    """One ordered context-shaping implementation at the shared seam."""

    name: str
    shape: ContextShapingAdapter

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("context shaping profile name must be non-empty")


def _provider_safe_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Copy messages while retaining lifecycle identity for canonical history.

    Provider protocol adapters serialize only role/content/tool fields, so
    retained underscore-prefixed lifecycle keys never enter the wire payload.
    They must survive here because the shaped list is also promoted back into
    the persistent teammate history after each iteration.
    """

    provider_safe_messages: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            provider_safe_messages.append(message)
            continue
        safe = {
            key: value
            for key, value in message.items()
            if key != "metadata" and not str(key).startswith("_")
        }
        for key in (
            "_attachment_id",
            "_attachment_kind",
            "_attachment_metadata",
            "_is_meta",
            "_authoritative_decision_frame",
            "_turn_input",
            "_compact_boundary",
            "_timestamp",
        ):
            if key in message:
                safe[key] = message[key]
        content = safe.get("content")
        if isinstance(content, list):
            safe["content"] = [
                {
                    key: value
                    for key, value in block.items()
                    if key != "metadata" and not str(key).startswith("_")
                }
                if isinstance(block, dict)
                else block
                for block in content
            ]
        provider_safe_messages.append(safe)
    return provider_safe_messages


def _shared_context_shaping(
    messages: list[dict[str, Any]],
    model: str,
    *,
    max_tool_result_chars: int | None = 2_000,
    transform_history: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> ContextShapingResult:
    shaped = _provider_safe_messages(messages)
    if transform_history is not None:
        shaped = transform_history(shaped)
    if max_tool_result_chars is not None:
        shaped = apply_tool_result_budget(shaped, max_per_result=max_tool_result_chars)
    shaped, snip_freed = snip_if_needed(shaped)
    shaped, microcompact_freed = microcompact_messages(shaped)
    shaped, collapse_saved = apply_context_collapse(shaped, model=model)
    collapse_owns_headroom = bool(
        collapse_saved > 0 and not should_collapse(shaped, model)[0]
    )
    return ContextShapingResult(
        messages=shaped,
        snip_tokens_freed=snip_freed,
        microcompact_tokens_freed=microcompact_freed,
        collapse_tokens_saved=collapse_saved,
        collapse_owns_headroom=collapse_owns_headroom,
    )


def build_shared_context_shaping_profile(
    *,
    name: str,
    max_tool_result_chars: int | None,
    transform_history: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> ContextShapingProfile:
    """Bind Profile content to the one shared shaping implementation."""

    if not name.strip():
        raise ValueError("context shaping profile name must be non-empty")
    if max_tool_result_chars is not None and max_tool_result_chars <= 0:
        raise ValueError("max_tool_result_chars must be positive")

    if transform_history is not None and not callable(transform_history):
        raise ValueError("transform_history must be callable")

    def shape(
        messages: list[dict[str, Any]],
        model: str,
    ) -> ContextShapingResult:
        return _shared_context_shaping(
            messages,
            model,
            max_tool_result_chars=max_tool_result_chars,
            transform_history=transform_history,
        )

    return ContextShapingProfile(name=name, shape=shape)


SHARED_CONTEXT_SHAPING_PROFILE = build_shared_context_shaping_profile(
    name="shared-default",
    max_tool_result_chars=2_000,
)


async def _collect_initial_memory(turn: Any, context: Any) -> list[dict[str, Any]]:
    return await turn.collect_initial_memory(context)


async def _skip_initial_memory(_turn: Any, _context: Any) -> list[dict[str, Any]]:
    return []


@dataclass(frozen=True)
class MemoryProfile:
    """Initial memory delivery adapter for the fixed turn-start seam."""

    name: str
    collect_initial: InitialMemoryAdapter

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("memory profile name must be non-empty")


@dataclass(frozen=True)
class AgentRuntimeProfile:
    """Own how one current turn input becomes durable conversation history."""

    name: str
    build_turn_message: TurnMessageBuilder

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("agent runtime profile name must be non-empty")


def _code_turn_message(value: TurnInput | str) -> dict[str, Any]:
    turn_input = normalize_turn_input(value)
    return {
        "role": "user",
        "content": [{"type": "text", "text": turn_input.text}],
        **({"_is_meta": True} if turn_input.is_meta else {}),
    }


CODE_MEMORY_PROFILE = MemoryProfile(
    name="code",
    collect_initial=_collect_initial_memory,
)
DISABLED_MEMORY_PROFILE = MemoryProfile(
    name="disabled",
    collect_initial=_skip_initial_memory,
)
CODE_AGENT_RUNTIME_PROFILE = AgentRuntimeProfile(
    name="code",
    build_turn_message=_code_turn_message,
)


@dataclass(frozen=True)
class BudgetThinkingProfile:
    initial_max_tokens: int = 8_000
    initial_thinking_effort: str | None = None
    disable_thinking_after_turn: int | None = None
    # Default circuit blocking follows the current provider/model capacity.
    # An explicit value preserves the profile's existing blocking override.
    context_window_tokens: int | None = None
    context_blocking_ratio: float = 0.95

    def __post_init__(self) -> None:
        if self.initial_max_tokens <= 0:
            raise ValueError("initial_max_tokens must be positive")
        if self.context_window_tokens is not None and self.context_window_tokens <= 0:
            raise ValueError("context_window_tokens must be positive")
        if not 0 < self.context_blocking_ratio <= 1:
            raise ValueError("context_blocking_ratio must be in (0, 1]")
        if self.initial_thinking_effort not in {None, "low", "high", "max"}:
            raise ValueError(
                "initial_thinking_effort must be low, high, max, or None",
            )
        if (
            self.disable_thinking_after_turn is not None
            and self.disable_thinking_after_turn < 0
        ):
            raise ValueError("disable_thinking_after_turn must be non-negative")


@dataclass(frozen=True)
class ProviderRequestPolicy:
    """Provider-attempt behavior owned by one ErrorRecoveryProfile."""

    name: str
    max_attempts: int = 1
    atomic_attempts: bool = False
    first_event_timeout_seconds: float | None = None
    idle_timeout_seconds: float | None = None
    attempt_timeout_seconds: float | None = None
    parallel_tool_calls: bool | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("provider request policy name must be non-empty")
        if self.max_attempts <= 0:
            raise ValueError("provider max_attempts must be positive")
        for field_name in (
            "first_event_timeout_seconds",
            "idle_timeout_seconds",
            "attempt_timeout_seconds",
        ):
            value = getattr(self, field_name)
            if value is not None and value <= 0:
                raise ValueError(f"{field_name} must be positive")


@dataclass(frozen=True)
class RequestRecoveryDecision:
    """Profile-owned request controls, never generated tool arguments."""

    tool_name: str | None = None
    disable_thinking: bool = False
    stop_reason: str | None = None
    temperature: float | None = None

    def __post_init__(self) -> None:
        if self.temperature is not None and (
            isinstance(self.temperature, bool)
            or not isinstance(self.temperature, (int, float))
            or not 0 <= self.temperature <= 2
        ):
            raise ValueError("recovery temperature must be a finite number from 0 to 2")


@dataclass(frozen=True)
class ErrorRecoveryProfile:
    name: str
    max_output_recovery_message: RecoveryMessageBuilder
    escalated_max_tokens: int = 64_000
    max_output_recovery_attempts: int = 3
    # Output length is not proof of input overflow. Profiles that recover
    # generation separately must not turn this into a compaction request.
    compact_after_output_exhaustion: bool = True
    # Code can continue a partial answer; Game must retry from valid facts.
    preserve_incomplete_assistant: bool = True
    max_consecutive_model_errors: int = 3
    provider_request: ProviderRequestPolicy | None = None
    visible_deliberation_limit_chars: int | None = None
    visible_deliberation_recovery_message: str | None = None
    max_visible_deliberation_recoveries: int = 0
    deliberation_repetition_guard: bool = False
    # Opt-in request controls for an output recovery only. A successful tool
    # result returns the next request to the ordinary budget/thinking profile.
    output_recovery_disable_thinking: bool = False
    output_recovery_tool_name: str | None = None
    prepare_request: Callable[[Any, Any], RequestRecoveryDecision] | None = None
    # Optional request-only projection. Canonical history and persistence keep
    # the original exchanges; the profile owns which read-only attempts to omit.
    project_request_messages: Callable[[Any, list[dict[str, Any]]], list[dict[str, Any]]] | None = None

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("error recovery profile name must be non-empty")
        if self.escalated_max_tokens <= 0:
            raise ValueError("escalated_max_tokens must be positive")
        if self.max_output_recovery_attempts < 0:
            raise ValueError("max_output_recovery_attempts must be non-negative")
        if self.output_recovery_tool_name is not None and not self.output_recovery_tool_name.strip():
            raise ValueError("output recovery tool name must be non-empty")
        if self.max_consecutive_model_errors <= 0:
            raise ValueError("max_consecutive_model_errors must be positive")
        if self.visible_deliberation_limit_chars is None:
            if (
                self.visible_deliberation_recovery_message is not None
                or self.max_visible_deliberation_recoveries != 0
            ):
                raise ValueError(
                    "visible deliberation recovery requires a positive limit",
                )
        elif (
            self.visible_deliberation_limit_chars <= 0
            or not str(self.visible_deliberation_recovery_message or "").strip()
            or self.max_visible_deliberation_recoveries <= 0
        ):
            raise ValueError("visible deliberation recovery is incomplete")


@dataclass(frozen=True)
class RelinkProfile:
    build: CompactRelinkBuilder = build_code_compact_relink


def _code_max_output_recovery_message(attempt: int) -> str:
    return (
        "Output token limit hit. Resume directly — no apology, no recap of "
        "what you were doing. Pick up mid-thought if that is where the cut "
        "happened. Break remaining work into smaller pieces. "
        f"[attempt {attempt}]"
    )


CODE_ERROR_RECOVERY_PROFILE = ErrorRecoveryProfile(
    name="code",
    max_output_recovery_message=_code_max_output_recovery_message,
)


async def _run_code_stop_hooks(**context: Any):
    from bglab.hooks import handle_stop_hooks

    flags = getattr(context["deps"], "feature_flags", None)
    memory_enabled = (
        flags is None
        or bool(getattr(flags, "memory_section_enabled", True))
    )
    return await handle_stop_hooks(
        context["messages_for_query"],
        context["assistant_messages"],
        context["tool_use_context"],
        state=context["deps"].stop_hooks_state,
        system_prompt=context["system_prompt"],
        user_context=context.get("user_context"),
        system_context=context.get("system_context"),
        cwd=context["cwd"],
        model=context["model"],
        skip_background=False,
        memory_extraction="coding" if memory_enabled else None,
        hooks=[],
        extra={},
    )


async def run_passive_stop_hooks(**context: Any):
    """Capture the shared stop snapshot without Code background work."""

    from bglab.hooks import handle_stop_hooks

    return await handle_stop_hooks(
        context["messages_for_query"],
        context["assistant_messages"],
        context["tool_use_context"],
        state=context["deps"].stop_hooks_state,
        system_prompt=context["system_prompt"],
        user_context=context.get("user_context"),
        system_context=context.get("system_context"),
        cwd=context["cwd"],
        model=context["model"],
        skip_background=True,
        memory_extraction=None,
        hooks=[],
        extra={},
    )


CODE_STOP_HOOK_PROFILE = StopHookProfile(
    name="code",
    run=_run_code_stop_hooks,
)


@dataclass(frozen=True)
class QueryLoopProfile:
    """Content and trigger policy plugged into the one fixed query loop."""

    name: str
    prompt: PromptProfile
    tools: ToolProfile
    attachments: AttachmentProfile
    compaction: CompactionProfile
    context_shaping: ContextShapingProfile = SHARED_CONTEXT_SHAPING_PROFILE
    memory: MemoryProfile = CODE_MEMORY_PROFILE
    agent_runtime: AgentRuntimeProfile = CODE_AGENT_RUNTIME_PROFILE
    stop_hook: StopHookProfile = CODE_STOP_HOOK_PROFILE
    output: OutputProfile = OutputProfile()
    budget_thinking: BudgetThinkingProfile = BudgetThinkingProfile()
    recovery: ErrorRecoveryProfile = CODE_ERROR_RECOVERY_PROFILE
    relink: RelinkProfile = RelinkProfile()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("query loop profile name must be non-empty")


CODE_QUERY_PROFILE = QueryLoopProfile(
    name="code",
    prompt=CODE_PROMPT_PROFILE,
    tools=ToolProfile(
        name="code",
        resolve_surface=_code_tool_surface,
        execute=execute_code_tool,
        resolve_permission_mode=_code_permission_mode,
        resolve_cwd=_code_cwd,
    ),
    attachments=CODE_ATTACHMENT_PROFILE,
    compaction=CODE_COMPACTION_PROFILE,
)


CODE_BACKGROUND_QUERY_PROFILE = replace(
    CODE_QUERY_PROFILE,
    name="code-background",
    stop_hook=StopHookProfile(
        name="code-background",
        run=run_passive_stop_hooks,
    ),
)


__all__ = [
    "CODE_BACKGROUND_QUERY_PROFILE",
    "CODE_ERROR_RECOVERY_PROFILE",
    "CODE_AGENT_RUNTIME_PROFILE",
    "CODE_MEMORY_PROFILE",
    "CODE_STOP_HOOK_PROFILE",
    "CODE_QUERY_PROFILE",
    "BudgetThinkingProfile",
    "ContextShapingProfile",
    "ContextShapingResult",
    "DISABLED_MEMORY_PROFILE",
    "ErrorRecoveryProfile",
    "OutputProfile",
    "ProviderRequestPolicy",
    "MemoryProfile",
    "AgentRuntimeProfile",
    "QueryLoopProfile",
    "RelinkProfile",
    "SHARED_CONTEXT_SHAPING_PROFILE",
    "StopHookProfile",
    "ToolProfile",
    "build_code_compact_relink",
    "build_shared_context_shaping_profile",
    "run_passive_stop_hooks",
    "identity_cwd",
    "identity_permission_mode",
]
