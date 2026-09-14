"""Request usage accounting shared by query, runners, and game totals.

Token totals contain only reported usage. Missing or partial attempt reports
remain visible; context estimates are never substituted for Provider usage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


_COUNTERS = (
    "input_tokens", "output_tokens", "cache_hit_tokens", "cache_miss_tokens",
    "requests", "provider_attempts", "provider_retries", "provider_timeouts",
    "compaction_requests", "usage_missing_requests",
    "usage_missing_attempts", "usage_partial_attempts",
)

_TOKEN_COUNTERS = (
    "input_tokens", "output_tokens", "cache_hit_tokens", "cache_miss_tokens",
    "reasoning_tokens",
)


@dataclass
class ProviderAttemptUsage:
    """Latest cumulative report for one stream, independent of its content buffer."""

    usage: dict[str, Any] = field(default_factory=dict)
    finished: bool = False
    closed: bool = False

    def observe(self, usage: dict[str, Any] | None = None, *, finished: bool = False) -> None:
        if self.closed:
            return  # A timed-out SDK task may unwind after the next attempt starts.
        for key in (*_TOKEN_COUNTERS, "cache_details_supported"):
            if usage is not None and usage.get(key) is not None:
                self.usage[key] = usage[key]
        self.finished |= finished

    def snapshot(self, attempt: int) -> dict[str, Any]:
        known = any(key in self.usage for key in ("input_tokens", "output_tokens"))
        complete = self.finished and all(key in self.usage for key in ("input_tokens", "output_tokens"))
        return {"attempt": attempt, "status": "complete" if complete else "partial" if known else "missing",
                "usage": dict(self.usage)}


def with_attempt_usage(usage: dict[str, Any] | None, attempts: list[ProviderAttemptUsage]) -> dict[str, Any]:
    """Sum distinct attempts once; never sum cumulative chunks or invent missing tokens."""
    result = {key: value for key, value in (usage or {}).items()
              if key not in (*_TOKEN_COUNTERS, "cache_details_supported")}
    records = [attempt.snapshot(index) for index, attempt in enumerate(attempts, 1)]
    for record in records:
        for key in _TOKEN_COUNTERS:
            if key in record["usage"]:
                result[key] = result.get(key, 0) + record["usage"][key]
        if "cache_details_supported" in record["usage"]:
            result["cache_details_supported"] = bool(result.get("cache_details_supported")
                                                      or record["usage"]["cache_details_supported"])
    result["provider_attempt_usage"] = records
    result["usage_missing_attempts"] = sum(record["status"] == "missing" for record in records)
    result["usage_partial_attempts"] = sum(record["status"] == "partial" for record in records)
    result["usage_missing_requests"] = int(any(record["status"] != "complete" for record in records))
    return result


def empty_usage_totals() -> dict[str, Any]:
    return {**dict.fromkeys(_COUNTERS, 0), "cache_details_supported": False}


def merge_usage_totals(target: dict[str, Any], usage: dict[str, Any] | None) -> None:
    usage = usage or {}
    for key in _COUNTERS:
        target[key] = int(target.get(key, 0) or 0) + int(usage.get(key, 0) or 0)
    target["cache_details_supported"] = bool(
        target.get("cache_details_supported", False)
        or usage.get("cache_details_supported", False)
    )


def record_request_usage(
    target: dict[str, Any], usage: dict[str, Any] | None, *, purpose: str = "action",
) -> None:
    """Count one completed client call, independently of its API attempt count."""
    values = dict(usage or {})
    values["requests"] = 1
    values["compaction_requests"] = int(purpose == "compaction")
    values.setdefault("usage_missing_requests", int(
        usage is None
        or usage.get("input_tokens") is None
        or usage.get("output_tokens") is None
    ))
    merge_usage_totals(target, values)
