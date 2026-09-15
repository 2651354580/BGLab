from __future__ import annotations

from collections.abc import Mapping


def validate_public_summary(value: object, *, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("publicSummary must be non-empty text")
    return value


def render_public_outcome(outcome: Mapping[str, object]) -> str:
    """Render explicit package text; legacy facts have no inferred game meaning."""
    summary = validate_public_summary(outcome.get("publicSummary"))
    return summary if summary is not None else "未提供公开结果摘要。"
