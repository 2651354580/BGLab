"""Deterministic, failure-isolated attachment provider collection."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Sequence

from bglab.engine.attachments.types import (
    AttachmentProvider,
    AttachmentValue,
    ProviderContext,
)


logger = logging.getLogger("bglab.engine.attachments")


class AttachmentCollectionError(RuntimeError):
    """A strict profile rejected a provider result before model delivery."""


async def _build(
    provider: AttachmentProvider,
    context: ProviderContext,
) -> Sequence[AttachmentValue]:
    result = provider.build(context)
    if inspect.isawaitable(result):
        result = await result
    if provider.max_bytes is not None:
        size = sum(
            len(value.text.encode("utf-8"))
            for value in result
            if isinstance(value, AttachmentValue)
        )
        if size > provider.max_bytes:
            raise AttachmentCollectionError(
                f"provider {provider.id} exceeds its attachment byte limit"
            )
    return result


async def collect_providers(
    providers: tuple[AttachmentProvider, ...],
    context: ProviderContext,
    *,
    group: str,
) -> list[AttachmentValue]:
    """Run providers concurrently while preserving declared result order."""

    if not providers:
        return []
    results = await asyncio.gather(
        *(_build(provider, context) for provider in providers),
        return_exceptions=True,
    )
    values: list[AttachmentValue] = []
    for provider, result in zip(providers, results):
        if isinstance(result, BaseException):
            if provider.required or context.profile.provider_failure_policy == "raise":
                if isinstance(result, AttachmentCollectionError):
                    raise result
                raise AttachmentCollectionError(
                    f"attachment provider failed: {provider.id}"
                ) from result
            logger.warning(
                "attachment provider failed profile=%s group=%s provider=%s error=%s",
                context.profile.name,
                group,
                provider.id,
                type(result).__name__,
            )
            continue
        for value in result:
            if not isinstance(value, AttachmentValue):
                if provider.required or context.profile.provider_failure_policy == "raise":
                    raise AttachmentCollectionError(
                        f"attachment provider returned invalid value: {provider.id}"
                    )
                logger.warning(
                    "attachment provider returned invalid value profile=%s group=%s provider=%s",
                    context.profile.name,
                    group,
                    provider.id,
                )
                continue
            values.append(value)
    return values
