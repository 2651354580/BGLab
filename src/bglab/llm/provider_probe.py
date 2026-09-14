"""Small, redacted connectivity probe for one configured Provider slot."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bglab.llm.client import call_model
from bglab.llm.provider_slots import ProviderSlot
from bglab.llm.types import LoopEventType


async def probe_provider_slot(
    slot: ProviderSlot,
    *,
    call_model_fn: Callable[..., Any] = call_model,
) -> bool:
    """Return only whether one minimal request completed without Provider failure."""
    saw_done = False
    try:
        async for event in call_model_fn(
            messages=[{
                "role": "user",
                "content": [{"type": "text", "text": "Reply with exactly OK."}],
            }],
            system_prompt="This is a connectivity check. Reply with exactly OK.",
            tools=[],
            model=slot.reference,
            provider_slot=slot,
            max_tokens=16,
            temperature=0.0,
            max_retries=1,
            atomic_attempts=True,
            first_event_timeout_seconds=20.0,
            idle_timeout_seconds=20.0,
            attempt_timeout_seconds=30.0,
        ):
            if event.provider_failure is not None or event.type == LoopEventType.ERROR:
                return False
            if event.type == LoopEventType.DONE:
                saw_done = True
    except Exception:
        return False
    return saw_done
