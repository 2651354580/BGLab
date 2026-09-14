"""Pure card-fact helper plus canonical production tool export.

The previous module exposed an opaque strategy score. That score is intentionally
removed: callers receive only payment and card facts and make their own decision.
"""

from bglab.games.tools.tool_factory import (
    _calc_discount,
    _card_bonus_color,
    _card_payment_fact,
    _player_gems,
    create_card_eval_tool,
)


def compute_card_facts(card: dict, state: dict, seat: int) -> dict:
    players = state.get("playerstorage", [])
    player = players[seat] if 0 <= seat < len(players) else {}
    fact = _card_payment_fact(
        card,
        _calc_discount(state, player),
        _player_gems(player),
    )
    return {
        "card_id": card.get("id"),
        "level": card.get("lvl", card.get("level")),
        "points": int(card.get("points", 0) or 0),
        "bonus_color": _card_bonus_color(card),
        **fact,
    }


__all__ = ["compute_card_facts", "create_card_eval_tool"]
