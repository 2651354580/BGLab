"""Pure presentation state projection for the Code Agent timeline.

The reducer in this module deliberately has no provider, filesystem, or Textual
dependencies.  It owns one local turn and exposes immutable snapshots for a
passive UI to render.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Literal, Mapping

from bglab.llm.failure_presentation import (
    ProviderIssuePresentation,
    project_provider_failure,
)
from bglab.llm.types import (
    LoopEvent,
    LoopEventType,
    ToolResultBlock,
    ToolUseBlock,
)
from bglab.engine.error_policy import RUNTIME_FAILURE


ClosureKind = Literal["completed", "failed", "canceled", "stopped", "protocol_error"]
ToolState = Literal["running", "approval", "success", "failure", "declined", "canceled"]


@dataclass(frozen=True)
class TerminalPresentation:
    reason: str
    turn_count: int
    total_usage: Mapping[str, int] | None
    elapsed_ms: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "total_usage", _immutable_mapping(self.total_usage))


@dataclass(frozen=True)
class TurnKey:
    session_id: str
    turn_id: int


@dataclass(frozen=True)
class ToolKey:
    turn: TurnKey
    tool_use_id: str


@dataclass(frozen=True)
class BoundedOutput:
    preview: str
    detail: str
    omitted_lines: int
    omitted_chars: int
    expanded: bool


@dataclass(frozen=True)
class ToolPresentationState:
    key: ToolKey
    tool_use_id: str
    name: str
    target_summary: str
    state: ToolState
    input_summary: str
    output: BoundedOutput
    error_summary: str | None
    duration_ms: int | None
    expanded: bool
    anonymous_result: bool


@dataclass(frozen=True)
class PermissionPresentation:
    tool_name: str
    tool_input: Mapping[str, object]
    reason: str
    tool_use_id: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_input", _immutable_mapping(self.tool_input))


@dataclass(frozen=True)
class TurnPresentationState:
    key: TurnKey
    preparing_tool: bool
    user_text: str
    assistant_text_segments: tuple[str, ...]
    tools: tuple[ToolPresentationState, ...]
    file_summaries: tuple[str, ...]
    test_summaries: tuple[str, ...]
    terminal: TerminalPresentation | None
    turn_error_summary: str | None
    provider_issue: ProviderIssuePresentation | None = None
    usage: Mapping[str, int | float | str] = field(default_factory=dict)
    closure: ClosureKind | None = None
    pending_permission: PermissionPresentation | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "usage", _immutable_mapping(self.usage))


class PresentationReducer:
    """Reduce LoopEvent frames into a stable presentation snapshot."""

    def __init__(
        self,
        session_id: str,
        turn_id: int,
        user_text: str,
        *,
        preview_line_limit: int = 8,
        detail_line_limit: int = 80,
        preview_char_limit: int = 512,
        detail_char_limit: int = 8192,
    ) -> None:
        self._state = TurnPresentationState(
            key=TurnKey(session_id, turn_id),
            preparing_tool=False,
            user_text=user_text,
            assistant_text_segments=(),
            tools=(),
            file_summaries=(),
            test_summaries=(),
            terminal=None,
            turn_error_summary=None,
            provider_issue=None,
        )
        self._active_tool_id: str | None = None
        self._limits = (
            preview_line_limit,
            detail_line_limit,
            preview_char_limit,
            detail_char_limit,
        )

    def reduce(self, event: LoopEvent) -> TurnPresentationState:
        if self._state.closure is not None:
            return self._state
        if event.usage:
            self._state = replace(self._state, usage=dict(event.usage))
        if event.type is LoopEventType.TOOL_USE_START:
            return self._replace(preparing_tool=True)
        if event.type is LoopEventType.TEXT:
            if event.text:
                return self._replace(
                    preparing_tool=False,
                    assistant_text_segments=(
                        *self._state.assistant_text_segments,
                        event.text,
                    )
                )
            return self._state
        if event.type is LoopEventType.TOOL_USE_END:
            return self._upsert_end(event.tool_use)
        if event.type is LoopEventType.TOOL_RESULT:
            return self._upsert_result(event.tool_result)
        if event.type is LoopEventType.PERMISSION_ASK:
            return self._state
        if event.type is LoopEventType.ERROR:
            if event.provider_failure is not None:
                return self._replace(
                    turn_error_summary=None,
                    provider_issue=project_provider_failure(
                        event.provider_failure,
                    ).with_usage(event.usage),
                )
            return self._replace(turn_error_summary=RUNTIME_FAILURE)
        if event.type is LoopEventType.DONE:
            if event.terminal is None:
                return self._replace(
                    preparing_tool=False,
                    turn_error_summary="DONE 缺少可信 TerminalInfo",
                    closure="protocol_error",
                )
            terminal = event.terminal
            terminal_usage = terminal.total_usage or self._state.usage
            provider_issue = self._state.provider_issue
            if provider_issue is not None:
                provider_issue = provider_issue.with_usage(terminal_usage)
            return self._replace(
                preparing_tool=False,
                terminal=TerminalPresentation(
                    reason=terminal.reason,
                    turn_count=terminal.turn_count,
                    total_usage=terminal.total_usage,
                    elapsed_ms=terminal.elapsed_ms,
                ),
                usage=terminal_usage,
                provider_issue=provider_issue,
                closure=map_terminal_reason(terminal.reason),
            )
        return self._replace(turn_error_summary="忽略未知的非终结事件")

    def snapshot(self) -> TurnPresentationState:
        return self._state

    def begin_permission(
        self,
        tool_name: str,
        tool_input: Mapping[str, object],
        reason: str,
    ) -> TurnPresentationState:
        """Mark the real active END card as awaiting approval when available."""

        if self._state.closure is not None:
            return self._state
        tools = list(self._state.tools)
        active = next(
            (
                item
                for item in tools
                if item.tool_use_id == self._active_tool_id
                and item.state in {"running", "approval"}
            ),
            None,
        )
        tool_use_id: str | None = None
        if active is not None:
            index = tools.index(active)
            input_summary = _summarize_input(tool_input)
            tools[index] = replace(
                active,
                name=tool_name,
                input_summary=input_summary,
                target_summary=input_summary,
                state="approval",
                error_summary=reason or None,
            )
            tool_use_id = active.tool_use_id
        pending = PermissionPresentation(
            tool_name=tool_name,
            tool_input=dict(tool_input),
            reason=reason,
            tool_use_id=tool_use_id,
        )
        return self._replace(tools=tuple(tools), pending_permission=pending)

    def resolve_permission(self, allowed: bool) -> TurnPresentationState:
        """Resolve local approval without creating a synthetic tool card."""

        if self._state.closure is not None:
            return self._state
        pending = self._state.pending_permission
        if pending is None:
            return self._state
        tools = list(self._state.tools)
        if pending.tool_use_id is not None:
            existing = next(
                (item for item in tools if item.tool_use_id == pending.tool_use_id),
                None,
            )
            if existing is not None:
                index = tools.index(existing)
                tools[index] = replace(
                    existing,
                    state="running" if allowed else "declined",
                    error_summary=None if allowed else pending.reason or "被拒绝",
                )
        return self._replace(tools=tuple(tools), pending_permission=None)

    def toggle_tool(self, tool_use_id: str) -> TurnPresentationState:
        """Toggle expansion for an existing card; unknown ids are a no-op."""

        if self._state.closure is not None:
            return self._state
        tools = list(self._state.tools)
        for index, item in enumerate(tools):
            if item.tool_use_id == tool_use_id:
                expanded = not item.expanded
                tools[index] = replace(
                    item,
                    expanded=expanded,
                    output=replace(item.output, expanded=expanded),
                )
                return self._replace(tools=tuple(tools))
        return self._state

    def _replace(self, **changes: object) -> TurnPresentationState:
        self._state = replace(self._state, **changes)
        return self._state

    def _new_tool(
        self,
        *,
        tool_id: str,
        name: str,
        input_summary: str,
        target_summary: str,
        state: ToolState,
        error_summary: str | None,
        anonymous_result: bool,
    ) -> ToolPresentationState:
        return ToolPresentationState(
            key=ToolKey(self._state.key, tool_id),
            tool_use_id=tool_id,
            name=name,
            target_summary=target_summary,
            state=state,
            input_summary=input_summary,
            output=bound_output(
                "",
                preview_line_limit=self._limits[0],
                detail_line_limit=self._limits[1],
                preview_char_limit=self._limits[2],
                detail_char_limit=self._limits[3],
            ),
            error_summary=error_summary,
            duration_ms=None,
            expanded=False,
            anonymous_result=anonymous_result,
        )

    def _upsert_end(self, tool_use: ToolUseBlock | None) -> TurnPresentationState:
        if tool_use is None:
            return self._replace(
                preparing_tool=False,
                turn_error_summary="TOOL_USE_END 缺少 tool_use",
            )
        self._active_tool_id = tool_use.id
        tools = list(self._state.tools)
        existing = next(
            (item for item in tools if item.tool_use_id == tool_use.id),
            None,
        )
        input_summary = _summarize_input(tool_use.input)
        pending = self._state.pending_permission
        pending_for_this_tool = (
            pending is not None
            and pending.tool_use_id is None
            and pending.tool_name == tool_use.name
        )
        if existing is None:
            tools.append(
                self._new_tool(
                    tool_id=tool_use.id,
                    name=tool_use.name,
                    input_summary=input_summary,
                    target_summary=input_summary,
                    state="approval" if pending_for_this_tool else "running",
                    error_summary=(pending.reason if pending_for_this_tool else None),
                    anonymous_result=False,
                )
            )
        elif existing.anonymous_result:
            index = tools.index(existing)
            tools[index] = replace(
                existing,
                name=tool_use.name,
                input_summary=input_summary,
                target_summary=input_summary,
                anonymous_result=False,
            )
        if pending_for_this_tool and pending is not None:
            pending = replace(pending, tool_use_id=tool_use.id)
        return self._replace(
            preparing_tool=False,
            tools=tuple(tools),
            pending_permission=pending,
        )

    def _upsert_result(self, result: ToolResultBlock | None) -> TurnPresentationState:
        if result is None:
            return self._replace(turn_error_summary="TOOL_RESULT 缺少 tool_result")
        tools = list(self._state.tools)
        tool_state: ToolState = "failure" if result.is_error else "success"
        output = bound_output(
            result.content,
            preview_line_limit=self._limits[0],
            detail_line_limit=self._limits[1],
            preview_char_limit=self._limits[2],
            detail_char_limit=self._limits[3],
        )
        existing = next(
            (item for item in tools if item.tool_use_id == result.tool_use_id),
            None,
        )
        if existing is None:
            tools.append(
                replace(
                    self._new_tool(
                        tool_id=result.tool_use_id,
                        name="工具结果",
                        input_summary="",
                        target_summary="",
                        state=tool_state,
                        error_summary=result.content if result.is_error else None,
                        anonymous_result=True,
                    ),
                    output=output,
                )
            )
        else:
            index = tools.index(existing)
            tools[index] = replace(
                existing,
                state=tool_state,
                output=output,
                error_summary=result.content if result.is_error else None,
            )
        return self._replace(tools=tuple(tools))


def bound_output(
    text: str,
    *,
    preview_line_limit: int,
    detail_line_limit: int,
    preview_char_limit: int,
    detail_char_limit: int,
    expanded: bool = False,
) -> BoundedOutput:
    """Return a bounded preview/detail pair for tool output."""

    lines = text.splitlines() or [""]
    detail_lines = lines[: max(0, detail_line_limit)]
    detail = "\n".join(detail_lines)[: max(0, detail_char_limit)]
    preview = "\n".join(detail_lines[: max(0, preview_line_limit)])[
        : max(0, preview_char_limit)
    ]
    preview_line_count = len(preview.splitlines()) if text else 0
    original_line_count = len(text.splitlines())
    return BoundedOutput(
        preview=preview,
        detail=detail,
        omitted_lines=max(0, original_line_count - preview_line_count),
        omitted_chars=max(0, len(text) - len(preview)),
        expanded=expanded,
    )


def map_terminal_reason(reason: str | None) -> ClosureKind:
    """Map an authoritative TerminalInfo reason to a presentation closure."""

    if reason == "completed":
        return "completed"
    if reason == "hook_stopped":
        return "stopped"
    if reason in {"user_abort", "aborted_streaming", "aborted_tools"}:
        return "canceled"
    if reason in {
        "blocking_limit",
        "stop_hook_prevented",
        "max_turns",
        "model_error",
        "prompt_too_long",
        "image_error",
        "tool_outcome_indeterminate",
    }:
        return "failed"
    return "protocol_error"


def _summarize_input(value: object) -> str:
    if not isinstance(value, Mapping):
        return ""
    parts = [f"{key}={str(item)[:80]}" for key, item in list(value.items())[:2]]
    return ", ".join(parts)


def _immutable_mapping(value: Mapping[object, object] | None) -> Mapping[object, object] | None:
    if value is None:
        return None
    return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return value
