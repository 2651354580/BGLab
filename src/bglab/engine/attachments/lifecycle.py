"""Owned prefetch handles for one query turn and its loop iterations."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from bglab.engine.attachments.collector import collect_providers
from bglab.engine.attachments.collector import AttachmentCollectionError
from bglab.engine.attachments.messages import (
    attachment_to_head_message,
    attachment_to_message,
)
from bglab.engine.attachments.types import (
    AttachmentProfile,
    AttachmentValue,
    ProviderContext,
)


async def _cancel_and_drain(task: asyncio.Task | None) -> None:
    if task is None:
        return
    if not task.done():
        task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


class AttachmentIteration:
    """One inter-turn skill prefetch, owned until collection or close."""

    def __init__(self, turn: "AttachmentTurn", context: ProviderContext):
        self._turn = turn
        self.context = context
        prefetch = turn.profile.skill_prefetch
        self._skill_task = (
            asyncio.create_task(prefetch(context)) if prefetch is not None else None
        )
        self._closed = False

    async def collect_after_tools(
        self,
        context: ProviderContext,
    ) -> list[dict[str, Any]]:
        if not context.local:
            context.local = self._turn.context.local
        
        # flattens them in declared slot order rather than completion order.
        groups = await asyncio.gather(
            collect_providers(
                self._turn.profile.thread,
                context,
                group="thread",
            ),
            collect_providers(
                self._turn.profile.main,
                context,
                group="main",
            ),
            return_exceptions=True,
        )
        for result in groups:
            if isinstance(result, BaseException):
                raise result
        thread_values, main_values = groups
        values = [*thread_values, *main_values]
        values.extend(await self._turn.collect_ready_memory(context))
        if self._skill_task is not None:
            try:
                result = await self._skill_task
            except (asyncio.CancelledError, Exception):
                result = ()
            values.extend(
                value for value in result if isinstance(value, AttachmentValue)
            )
        await self.aclose()
        return [
            attachment_to_message(value)
            for value in self._turn.accept(values)
        ]

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await _cancel_and_drain(self._skill_task)
        self._turn._iterations.discard(self)


class AttachmentTurn:
    """Turn-level owner for memory and all iteration prefetch tasks."""

    def __init__(self, profile: AttachmentProfile, context: ProviderContext):
        self.profile = profile
        self.context = context
        self._memory_consumed = False
        self._closed = False
        self._iterations: set[AttachmentIteration] = set()
        self._historical_ids = {
            str(message["_attachment_id"])
            for message in context.messages
            if isinstance(message, dict) and message.get("_attachment_id")
        }
        delivered_attr = profile.delivered_ids_attr
        delivered_ids = (
            getattr(context.deps, delivered_attr, None)
            if delivered_attr and context.deps is not None
            else None
        )
        if isinstance(delivered_ids, set):
            self._historical_ids.update(
                str(value) for value in delivered_ids if isinstance(value, str)
            )
        self._emitted_ids: set[str] = set()
        self._memory_task: asyncio.Task | None = None

    @classmethod
    def start(
        cls,
        profile: AttachmentProfile,
        context: ProviderContext,
    ) -> "AttachmentTurn":
        return cls(profile, context)

    async def close_iterations(self) -> None:
        for iteration in tuple(self._iterations):
            await iteration.aclose()

    def start_iteration(
        self,
        context: ProviderContext | None = None,
    ) -> AttachmentIteration:
        if self._closed:
            raise RuntimeError("attachment turn is closed")
        if context is not None and not context.local:
            context.local = self.context.local
        iteration = AttachmentIteration(self, context or self.context)
        self._iterations.add(iteration)
        return iteration

    async def collect_user_input(self) -> list[dict[str, Any]]:
        """Collect the fixed pre-loop groups, then start turn memory prefetch."""
        user_values = await collect_providers(
            self.profile.user_input,
            self.context,
            group="user_input",
        )
        
        # concurrently.  This preserves @file -> nested-memory side effects.
        groups = await asyncio.gather(
            collect_providers(
                self.profile.thread,
                self.context,
                group="thread",
            ),
            collect_providers(
                self.profile.main,
                self.context,
                group="main",
            ),
            return_exceptions=True,
        )
        for result in groups:
            if isinstance(result, BaseException):
                raise result
        thread_values, main_values = groups
        values = [*user_values, *thread_values, *main_values]
        prefetch = self.profile.memory_prefetch
        if prefetch is not None:
            self._memory_task = asyncio.create_task(prefetch(self.context))
        return [attachment_to_message(value) for value in self.accept(values)]

    async def render_head(
        self,
        context: ProviderContext | None = None,
    ) -> list[dict[str, Any]]:
        """Render the profile-owned session head for one provider request.

        Heads are request-local and intentionally bypass ``accept``: they do
        not consume transcript IDs, byte budget, or delivered-ID state.
        """
        render_context = context or self.context
        if not render_context.local:
            render_context.local = self.context.local
        values = await collect_providers(
            self.profile.head,
            render_context,
            group="head",
        )
        return [attachment_to_head_message(value) for value in values]

    def collect_post_compact(
        self,
        context: ProviderContext | None = None,
    ) -> list[dict[str, Any]]:
        """Collect typed lifecycle facts at a compaction boundary.

        Current post-compact providers are synchronous by design: the
        boundary helper remains synchronous for legacy callers while the
        profile-owned seam still validates values, IDs, provider byte limits,
        and failure policy.  An async builder is rejected rather than being
        accidentally persisted as a coroutine.
        """
        render_context = context or self.context
        if not render_context.local:
            render_context.local = self.context.local
        values: list[AttachmentValue] = []
        for provider in self.profile.post_compact:
            try:
                result = provider.build(render_context)
                if inspect.isawaitable(result):
                    result.close() if hasattr(result, "close") else None
                    raise AttachmentCollectionError(
                        f"post-compact provider must be synchronous: {provider.id}"
                    )
                provider_values = list(result)
                if provider.max_bytes is not None:
                    size = sum(
                        len(value.text.encode("utf-8"))
                        for value in provider_values
                        if isinstance(value, AttachmentValue)
                    )
                    if size > provider.max_bytes:
                        raise AttachmentCollectionError(
                            f"provider {provider.id} exceeds its attachment byte limit"
                        )
                for value in provider_values:
                    if not isinstance(value, AttachmentValue):
                        raise AttachmentCollectionError(
                            f"attachment provider returned invalid value: {provider.id}"
                        )
                values.extend(provider_values)
            except Exception as exc:
                if (
                    provider.required
                    or render_context.profile.provider_failure_policy == "raise"
                ):
                    if isinstance(exc, AttachmentCollectionError):
                        raise
                    raise AttachmentCollectionError(
                        f"attachment provider failed: {provider.id}"
                    ) from exc
                continue
        return [
            attachment_to_message(value)
            for value in self.accept(values)
        ]

    def accept(
        self,
        values: list[AttachmentValue],
    ) -> list[AttachmentValue]:
        accepted: list[AttachmentValue] = []
        local_ids: set[str] = set()
        batch_bytes = 0
        for value in values:
            attachment_id = value.attachment_id
            if self.profile.require_ids and not attachment_id:
                raise AttachmentCollectionError(
                    f"profile {self.profile.name} requires stable attachment ids"
                )
            if attachment_id:
                if attachment_id in local_ids:
                    raise AttachmentCollectionError(
                        f"duplicate attachment id: {attachment_id}"
                    )
                local_ids.add(attachment_id)
                if attachment_id in self._historical_ids or attachment_id in self._emitted_ids:
                    continue
            size = len(value.text.encode("utf-8"))
            next_total = batch_bytes + size
            if (
                self.profile.total_max_bytes is not None
                and next_total > self.profile.total_max_bytes
            ):
                raise AttachmentCollectionError(
                    f"profile {self.profile.name} attachment budget exceeded"
                )
            batch_bytes = next_total
            if attachment_id:
                self._emitted_ids.add(attachment_id)
                delivered_attr = self.profile.delivered_ids_attr
                if delivered_attr and self.context.deps is not None:
                    delivered_ids = getattr(
                        self.context.deps, delivered_attr, None,
                    )
                    if not isinstance(delivered_ids, set):
                        delivered_ids = set()
                        setattr(
                            self.context.deps, delivered_attr, delivered_ids,
                        )
                    delivered_ids.add(attachment_id)
            accepted.append(value)
        return accepted

    async def collect_ready_memory(
        self,
        context: ProviderContext | None = None,
        *,
        wait: bool = False,
    ) -> list[AttachmentValue]:
        task = self._memory_task
        if self._memory_consumed or task is None:
            return []
        if wait and not task.done():
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if not task.done():
            return []
        self._memory_consumed = True
        try:
            result = task.result()
        except (asyncio.CancelledError, Exception):
            return []
        values = [
            value for value in result if isinstance(value, AttachmentValue)
        ]
        if context is None:
            return values
        read_state = getattr(
            context.tool_use_context, "read_file_state", {},
        ) or {}
        if not read_state:
            return values
        return [
            value for value in values
            if value.kind != "relevant_memories"
            or not any(
                memory.get("path") in read_state
                for memory in value.metadata.get("memories", [])
                if isinstance(memory, dict)
            )
        ]

    async def collect_initial_memory(
        self,
        context: ProviderContext | None = None,
    ) -> list[dict[str, Any]]:
        """Wait for and render the Profile-selected first-request memory."""

        values = await self.collect_ready_memory(context, wait=True)
        return [
            attachment_to_message(value)
            for value in self.accept(values)
        ]

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.close_iterations()
        await _cancel_and_drain(self._memory_task)
