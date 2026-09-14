"""Persistent in-process teammate built on the standard query loop.

The runner owns teammate lifecycle and transcript continuity only. Prompts,
tools, permissions, dependencies, and mode-specific hooks are injected by its
caller, so game mode and future modes do not need a second model loop.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable
from uuid import uuid4

from bglab.llm.usage import empty_usage_totals, record_request_usage
from bglab.engine.deps import QueryDeps
from bglab.engine.query import AbortEvent
from bglab.engine.turn_input import TurnInput, normalize_turn_input
from bglab.llm.types import (
    LoopEvent,
    LoopEventType,
    ProviderFailureInfo,
    ToolDefinition,
)


HistorySaver = Callable[..., Any]
EventCallback = Callable[[LoopEvent], Any]
AttemptGuard = Callable[[], bool]


@dataclass
class InProcessTeammateConfig:
    name: str
    team_name: str
    system_prompt: str
    tools: list[ToolDefinition]
    handlers: dict[str, Callable[[dict], str]]
    deps: QueryDeps
    model: str = "deepseek-chat"
    max_turns: int = 50
    permission_mode: str = "default"
    cwd: str = ""
    session_id: str = ""
    messages: list[dict] = field(default_factory=list)
    history_saver: HistorySaver | None = None
    event_callback: EventCallback | None = None
    session_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class TeammateRunResult:
    final_text: str
    terminal_reason: str
    errors: list[str]
    events: list[LoopEvent]
    usage: dict[str, Any]
    provider_failure: ProviderFailureInfo | None = None
    provider_slot: Any = None


@dataclass
class InProcessAttemptHandle:
    """Public ownership handle for one in-process provider attempt."""

    identity: str
    task: asyncio.Task
    abort_event: AbortEvent

    @property
    def done(self) -> bool:
        return self.task.done()

    def abort(self, reason: str = "cancelled") -> None:
        self.abort_event.set(str(reason or "cancelled")[:200])

    async def drain(self, reason: str = "cancelled") -> None:
        self.abort(reason)
        current = asyncio.current_task()
        if self.task is not current and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


def _final_pure_text(messages: list[dict]) -> str:
    """Return text only when the final model message contains no tool use."""
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content", [])
        if not isinstance(content, list):
            return str(content).strip()
        if any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in content
        ):
            return ""
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
    return ""


class InProcessTeammateRunner:
    """One durable teammate that serializes prompts and retains full history."""

    def __init__(self, config: InProcessTeammateConfig):
        self.config = config
        self._request_session_id = config.session_id or uuid4().hex
        self.messages = list(config.messages)
        self._prompt_lock = asyncio.Lock()
        self._active_attempt_handle: InProcessAttemptHandle | None = None
        self._attempt_sequence = 0
        self._stopped = False
        self._delivered_attachment_ids = {
            message["_attachment_id"]
            for message in self.messages
            if isinstance(message, dict) and message.get("_attachment_id")
        }

    @property
    def is_idle(self) -> bool:
        return self._active_attempt_handle is None and not self._stopped

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    @property
    def active_attempt(self) -> InProcessAttemptHandle | None:
        return self._active_attempt_handle

    async def submit(
        self,
        prompt: TurnInput | str,
        *,
        attachments: list[dict] | None = None,
        handlers: dict[str, Callable[[dict], str]] | None = None,
        event_callback: EventCallback | None = None,
        attempt_guard: AttemptGuard | None = None,
        prompt_delivery: Callable[[], Any] | None = None,
        provider_slot: Any = None,
    ) -> TeammateRunResult:
        """Queue one prompt; later prompts for this teammate wait in order."""
        prompt = normalize_turn_input(prompt)
        async with self._prompt_lock:
            if self._stopped:
                raise RuntimeError(f"teammate {self.config.name} is stopped")
            if not prompt.text:
                raise ValueError("teammate prompt must not be empty")

            from bglab.engine.query import query_loop

            for attachment in attachments or []:
                attachment_id = attachment.get("_attachment_id")
                if attachment_id and attachment_id in self._delivered_attachment_ids:
                    continue
                self.messages.append(attachment)
                if attachment_id:
                    self._delivered_attachment_ids.add(attachment_id)
            abort_event = AbortEvent(reason="active")
            active_task = asyncio.current_task()
            if active_task is None:
                raise RuntimeError("teammate submit requires an asyncio task")
            self._attempt_sequence += 1
            active_handle = InProcessAttemptHandle(
                identity=(
                    f"{self.config.session_id or self.config.name}:"
                    f"{self._attempt_sequence}"
                ),
                task=active_task,
                abort_event=abort_event,
            )
            self._active_attempt_handle = active_handle
            active_handlers = handlers or self.config.handlers
            active_event_callback = event_callback or self.config.event_callback
            # Capture the immutable slot and model for this submit.  A later
            # controller failover must never alter an in-flight query.
            captured_provider_slot = provider_slot
            captured_model = getattr(
                captured_provider_slot, "reference", self.config.model,
            )
            events: list[LoopEvent] = []
            errors: list[str] = []
            provider_failure: ProviderFailureInfo | None = None
            terminal_reason = ""
            usage = empty_usage_totals()
            try:
                async for event in query_loop(
                    messages=self.messages,
                    system_prompt=self.config.system_prompt,
                    tools=self.config.tools,
                    handlers=active_handlers,
                    deps=self.config.deps,
                    model=captured_model,
                    provider_slot=captured_provider_slot,
                    max_turns=self.config.max_turns,
                    permission_mode=self.config.permission_mode,
                    cwd=self.config.cwd,
                    session_id=self.config.session_id or None,
                    request_session_id=self.config.session_id or self._request_session_id,
                    abort_event=abort_event,
                    turn_input=prompt,
                    turn_input_appended=prompt_delivery,
                ):
                    events.append(event)
                    if event.provider_failure is not None:
                        provider_failure = event.provider_failure
                    if event.type == LoopEventType.ERROR and event.error:
                        errors.append(event.error)
                    if event.type == LoopEventType.DONE and event.terminal is None:
                        record_request_usage(
                            usage, event.usage, purpose=event.request_purpose,
                        )
                    if event.terminal is not None:
                        terminal_reason = event.terminal.reason
                    callback = active_event_callback
                    if callback is not None:
                        callback_result = callback(event)
                        if inspect.isawaitable(callback_result):
                            await callback_result
            finally:
                # A closed DecisionFrame is fenced before cancellation.  Do
                # not append a late transcript or clear a newer attempt's
                # runner references when an old task finally drains.
                if attempt_guard is None or attempt_guard():
                    self._persist_history()
                if self._active_attempt_handle is active_handle:
                    self._active_attempt_handle = None

            return TeammateRunResult(
                final_text=_final_pure_text(self.messages),
                terminal_reason=terminal_reason,
                errors=errors,
                events=events,
                usage=usage,
                provider_failure=provider_failure,
                provider_slot=captured_provider_slot,
            )

    async def cancel_current_attempt(
        self,
        reason: str = "cancelled",
        *,
        handle: InProcessAttemptHandle | None = None,
    ) -> None:
        """Abort and drain only the active query; keep the runner reusable."""
        reason = str(reason or "cancelled").strip()[:200] or "cancelled"
        target = handle or self._active_attempt_handle
        if target is None:
            return
        await target.drain(reason)
        if self._active_attempt_handle is target and target.done:
            self._active_attempt_handle = None

    async def stop(self) -> None:
        """Reject new work, abort an active query, and flush the transcript."""
        await self.cancel_current_attempt("shutdown")
        self._stopped = True
        self._persist_history()

    def persist(self) -> None:
        """Flush the current accumulated history without stopping the runner."""
        self._persist_history()

    def _persist_history(self) -> None:
        saver = self.config.history_saver
        if saver is None:
            from bglab.persistence import save_transcript

            saver = save_transcript
        meta = {
            "model": self.config.model,
            "tools": [tool.name for tool in self.config.tools],
            "permission_mode": self.config.permission_mode,
            "cwd": self.config.cwd,
            "team_name": self.config.team_name,
            "teammate": self.config.name,
            **self.config.session_meta,
        }
        saver(
            self.messages,
            cwd=self.config.cwd or None,
            session_id=self.config.session_id or None,
            session_meta=meta,
        )
