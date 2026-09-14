"""Game content plugged into the engine's fixed attachment lifecycle."""

from __future__ import annotations

from typing import Any

from bglab.engine.attachments import (
    AttachmentProfile,
    AttachmentProvider,
    AttachmentValue,
    ProviderContext,
)
from bglab.games.attachments import (
    GAME_ATTACHMENT_PROVIDERS,
    GAME_TURN_ATTACHMENT_MAX_BYTES,
    GameAttachmentProviderSpec,
    _capability_enabled,
    _provider_context,
)


def _message_value(message: dict[str, Any]) -> AttachmentValue:
    text = "".join(
        str(block.get("text", ""))
        for block in message.get("content", [])
        if isinstance(block, dict)
    )
    return AttachmentValue(
        kind=str(message.get("attachment_type", "game_context")),
        text=text,
        attachment_id=message.get("_attachment_id"),
    )


def _turn_provider(
    spec: GameAttachmentProviderSpec,
):
    def provider(context: ProviderContext) -> tuple[AttachmentValue, ...]:
        agent = getattr(context.deps, "_game_attachment_agent", None)
        if agent is None or not _capability_enabled(agent, spec.capability):
            return ()
        game_context = _provider_context(agent)
        if spec.timing == "first_turn" and game_context["turn"] != 1:
            return ()
        return tuple(
            _message_value(item)
            for item in spec.build(agent, game_context) or []
        )

    return provider


def _thread_provider(spec: GameAttachmentProviderSpec):
    def provider(context: ProviderContext) -> tuple[AttachmentValue, ...]:
        if context.phase != spec.timing:
            return ()
        actual_turn = int(getattr(context.deps, "_game_turn", 0) or 0)
        content = spec.build(context.deps, context.state, context.tool_uses)
        if content:
            if isinstance(content, str):
                text = content
                trigger_id = "default"
            else:
                text = content.text
                trigger_id = content.trigger_id
            return (AttachmentValue(
                kind="game_thread_reminder",
                text=text,
                attachment_id=(
                    f"{spec.provider_id}:game_turn:{actual_turn}:{trigger_id}"
                ),
                metadata={"deliveryStage": "thread_attachment"},
            ),)
        return ()

    return provider


def _context_provider(spec: GameAttachmentProviderSpec):
    def provider(context: ProviderContext):
        return spec.build(context)

    return provider


_turn_slots = tuple(
    AttachmentProvider(
        spec.provider_id,
        _turn_provider(spec),
        max_bytes=spec.max_bytes,
        required=spec.required,
    )
    for spec in GAME_ATTACHMENT_PROVIDERS
    if spec.slot == "user_input" and spec.interface == "agent"
)


_context_user_slots = tuple(
    AttachmentProvider(
        spec.provider_id,
        _context_provider(spec),
        max_bytes=spec.max_bytes,
        required=spec.required,
    )
    for spec in GAME_ATTACHMENT_PROVIDERS
    if spec.slot == "user_input" and spec.interface == "context"
)


_thread_slots = tuple(
    AttachmentProvider(
        spec.provider_id,
        _thread_provider(spec),
        max_bytes=spec.max_bytes,
        required=spec.required,
    )
    for spec in GAME_ATTACHMENT_PROVIDERS
    if spec.slot == "thread"
)


_head_slots = tuple(
    AttachmentProvider(
        spec.provider_id,
        _context_provider(spec),
        max_bytes=spec.max_bytes,
        required=spec.required,
    )
    for spec in GAME_ATTACHMENT_PROVIDERS
    if spec.slot == "head"
)


_post_compact_slots = tuple(
    AttachmentProvider(
        spec.provider_id,
        _context_provider(spec),
        max_bytes=spec.max_bytes,
        required=spec.required,
    )
    for spec in GAME_ATTACHMENT_PROVIDERS
    if spec.slot == "post_compact"
)


_memory_prefetch_specs = tuple(
    spec for spec in GAME_ATTACHMENT_PROVIDERS
    if spec.slot == "memory_prefetch"
)
if len(_memory_prefetch_specs) > 1:
    raise ValueError("game attachment registry has multiple memory prefetch providers")


def _memory_prefetch_provider(spec: GameAttachmentProviderSpec):
    async def provider(context: ProviderContext) -> tuple[AttachmentValue, ...]:
        agent = getattr(context.deps, "_game_attachment_agent", None)
        if agent is None or not _capability_enabled(agent, spec.capability):
            return ()
        values = tuple(await spec.build(context) or ())
        size = sum(
            len(value.text.encode("utf-8"))
            for value in values
            if isinstance(value, AttachmentValue)
        )
        if size > spec.max_bytes:
            raise ValueError(
                f"provider {spec.provider_id} exceeds its attachment byte limit"
            )
        return values

    return provider


_memory_prefetch = (
    _memory_prefetch_provider(_memory_prefetch_specs[0])
    if _memory_prefetch_specs
    else None
)


GAME_ATTACHMENT_PROFILE = AttachmentProfile(
    name="game",
    head=(*_head_slots,),
    user_input=(
        *_turn_slots,
        *_context_user_slots,
    ),
    thread=(
        *_thread_slots,
    ),
    main=(),
    post_compact=(*_post_compact_slots,),
    memory_prefetch=_memory_prefetch,
    skill_prefetch=None,
    total_max_bytes=GAME_TURN_ATTACHMENT_MAX_BYTES,
    require_ids=True,
    provider_failure_policy="isolate",
    delivered_ids_attr="_game_delivered_attachment_ids",
)


__all__ = ["GAME_ATTACHMENT_PROFILE"]
