"""Pure affordability helper plus canonical production tool export."""

from bglab.games.tools.tool_factory import (
    _card_payment_fact,
    create_affordability_tool,
)


def check_affordability(
    cost: dict, player_gems: dict, discounts: dict | None = None,
) -> dict | None:
    fact = _card_payment_fact(
        {"cost": cost}, discounts or {}, player_gems,
    )
    return None if fact["affordable"] else fact["missing_by_color"]


__all__ = ["check_affordability", "create_affordability_tool"]
