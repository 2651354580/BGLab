"""Provider-neutral retry classification for model requests."""

from __future__ import annotations

import random
import math
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from time import time
from typing import Any


MAX_RETRY_DELAY_SECONDS = 30.0


class ProviderCompletionError(RuntimeError):
    """A provider explicitly ended generation without successful completion."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    reason: str
    retry_after_seconds: float | None = None
    switch_slot: bool = False


def classify_provider_error(error: BaseException) -> RetryDecision:
    """Classify failures recoverable within this request's retry window."""
    if isinstance(error, ProviderCompletionError):
        recoverable = error.reason == "insufficient_system_resource"
        return RetryDecision(recoverable, error.reason, switch_slot=recoverable)
    if _matches_exception(error, (TimeoutError,), {"APITimeoutError", "ReadTimeout"}):
        return RetryDecision(True, "timeout", switch_slot=True)
    if _matches_exception(
        error,
        (ConnectionError,),
        # SDK streams can raise raw HTTPX failures after headers were received,
        # without an APIConnectionError wrapper. Local protocol/input errors
        # remain non-retryable; the caller still owns safe replay boundaries.
        {"APIConnectionError", "ConnectError", "ConnectTimeout", "ReadError", "RemoteProtocolError"},
    ):
        return RetryDecision(True, "connection", switch_slot=True)

    status = _status_code(error)
    if status is None:
        return RetryDecision(False, type(error).__name__)

    retryable = status in {408, 409, 429} or 500 <= status <= 599
    retry_after = _retry_after_seconds(error) if retryable else None
    if retry_after is not None and retry_after > MAX_RETRY_DELAY_SECONDS:
        # Retry-After is the server's minimum delay, not a value we may cap.
        # Let the caller end this request instead of retrying too early.
        retryable = False
    switch_slot = status in {401, 403, 408, 429} or 500 <= status <= 599
    return RetryDecision(
        retryable,
        f"http_{status}",
        retry_after,
        switch_slot=switch_slot,
    )


def retry_delay_seconds(
    decision: RetryDecision,
    *,
    attempt_index: int,
    jitter: float | None = None,
) -> float:
    """Return backoff for a retryable decision, respecting the server minimum."""
    base = (
        decision.retry_after_seconds
        if decision.retry_after_seconds is not None
        else min(MAX_RETRY_DELAY_SECONDS, 2.0 * (2 ** max(0, attempt_index)))
    )
    if jitter is None:
        jitter = random.uniform(0.0, 0.5)
    return base + max(0.0, min(float(jitter), 0.5))


def _matches_exception(
    error: BaseException,
    types: tuple[type[BaseException], ...],
    names: set[str],
) -> bool:
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, types) or type(current).__name__ in names:
            return True
        cause = getattr(current, "__cause__", None)
        context = getattr(current, "__context__", None)
        current = cause if isinstance(cause, BaseException) else context
    return False


def _status_code(error: BaseException) -> int | None:
    direct = getattr(error, "status_code", None)
    if direct is not None:
        try:
            return int(direct)
        except (TypeError, ValueError):
            return None
    response = getattr(error, "response", None)
    value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _retry_after_seconds(error: BaseException) -> float | None:
    response = getattr(error, "response", None)
    headers: Any = getattr(response, "headers", None)
    if headers is None:
        headers = getattr(error, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
        if raw is None:
            raw = headers.get("Retry-After")
    except AttributeError:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        try:
            instant = parsedate_to_datetime(raw)
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            seconds = instant.timestamp() - time()
        except (AttributeError, TypeError, ValueError, OverflowError, OSError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None
