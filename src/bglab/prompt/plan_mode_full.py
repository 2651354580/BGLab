"""Code-Agent Plan Mode attachment text at the shared thread slot."""

from __future__ import annotations


_REMINDER_INTERVAL = 5


def get_plan_mode_attachment(
    turn_count: int,
    last_attachment_turn: int | None,
) -> str | None:
    """Return the first/periodic read-only reminder, deduplicated by turn."""

    current = max(1, int(turn_count or 1))
    previous = (
        int(last_attachment_turn)
        if isinstance(last_attachment_turn, int)
        and not isinstance(last_attachment_turn, bool)
        and last_attachment_turn > 0
        else None
    )
    if previous == current:
        return None
    if previous is not None and current - previous < _REMINDER_INTERVAL:
        return None
    return (
        "Plan mode is active. Continue read-only exploration and produce a "
        "concrete implementation plan; do not edit files or run state-changing "
        "commands. When the plan is complete, use ExitPlanMode if it is present."
    )


__all__ = ["get_plan_mode_attachment"]
