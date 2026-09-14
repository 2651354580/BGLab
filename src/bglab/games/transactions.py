"""Atomic, model-authored Splendor turn transactions.

Validation always happens against a deep copy.  The caller receives the copy
only after every operation and the completed turn intent have validated.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Literal

from . import engine


@dataclass(frozen=True)
class TransactionResult:
    status: Literal["committed", "invalid"]
    transaction: dict
    canonical_action: dict | None = None
    effects: tuple[dict, ...] = ()
    accepted_steps: tuple[int, ...] = ()
    failed_step: int | None = None
    error: dict | None = None
    state_changed: bool = False
    instruction: str = ""
    next_state: dict | None = field(default=None, repr=False, compare=False)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "transaction": copy.deepcopy(self.transaction),
            "canonicalAction": copy.deepcopy(self.canonical_action),
            "effects": copy.deepcopy(list(self.effects)),
            "acceptedSteps": list(self.accepted_steps),
            "failedStep": self.failed_step,
            "error": copy.deepcopy(self.error),
            "stateChanged": self.state_changed,
            "instruction": self.instruction,
        }


def _invalid(
    transaction: dict,
    index: int,
    code: str,
    message: str,
    *,
    available: Any = None,
    required: Any = None,
    expected: Any = None,
) -> TransactionResult:
    error: dict[str, Any] = {"code": code, "message": message}
    if available is not None:
        error["available"] = available
    if required is not None:
        error["required"] = required
    if expected is not None:
        error["expected"] = expected
    return TransactionResult(
        status="invalid",
        transaction=copy.deepcopy(transaction),
        accepted_steps=tuple(range(max(0, index))),
        failed_step=index,
        error=error,
        state_changed=False,
        instruction=f"前{index}步有效；请修正第{index + 1}步并重新提交完整 steps。",
    )


def _positive_count(step: dict) -> int | None:
    count = step.get("count", 1)
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else None


def _card_for_selection(state: dict, pid: int, step: dict) -> tuple[dict | None, int | None]:
    source = step.get("source")
    card_id = step.get("cardId")
    if source == "market":
        return next((c for c in state.get("market", []) if c.get("id") == card_id), None), None
    if source == "reserved":
        cards = state["playerstorage"][pid].get("reservedCards", [])
        index = next((i for i, c in enumerate(cards) if c.get("id") == card_id), None)
        return (cards[index], index) if index is not None else (None, None)
    return None, None


def validate_transaction(state: dict, pid: int, transaction: dict) -> TransactionResult:
    """Validate and simulate a complete turn without mutating ``state``."""
    if not isinstance(transaction, dict) or not isinstance(transaction.get("steps"), list):
        return _invalid(transaction if isinstance(transaction, dict) else {}, 0, "INVALID_TRANSACTION", "steps 必须是数组")
    steps = transaction["steps"]
    if not steps or not isinstance(steps[0], dict) or steps[0].get("op") != "begin":
        return _invalid(
            transaction, 0, "EXPECTED_BEGIN", "第一步必须包含 op=begin 和 action",
            expected={"op": "begin", "action": "take_gems|buy_card|reserve_card"},
        )
    if not (0 <= pid < len(state.get("playerstorage", []))):
        return _invalid(transaction, 0, "INVALID_PLAYER", f"玩家 {pid} 不存在")
    if state.get("wrapper", {}).get("currentPlayer") != pid:
        return _invalid(transaction, 0, "NOT_YOUR_TURN", "当前不是该玩家回合")

    action = steps[0].get("action")
    if action not in {"take_gems", "buy_card", "reserve_card"}:
        return _invalid(transaction, 0, "UNKNOWN_ACTION", f"未知行动 {action!r}")
    working = copy.deepcopy(state)
    player = working["playerstorage"][pid]
    initial_total = sum(player["gems"].values())

    if action == "take_gems":
        result = _validate_take(working, pid, transaction, initial_total)
    elif action == "buy_card":
        result = _validate_buy(working, pid, transaction)
    else:
        result = _validate_reserve(working, pid, transaction, initial_total)
    return result


def _validate_take(state: dict, pid: int, tx: dict, initial_total: int) -> TransactionResult:
    steps = tx["steps"]
    player = state["playerstorage"][pid]
    supply = copy.deepcopy(state["gamestorage"]["gems"])
    player_gems = copy.deepcopy(player["gems"])
    original_supply = copy.deepcopy(supply)
    taken = {c: 0 for c in engine.COLORS}
    discarded = {c: 0 for c in engine.ALL_GEMS}
    discarding = False
    for index, step in enumerate(steps[1:], 1):
        if not isinstance(step, dict):
            return _invalid(tx, index, "INVALID_STEP", "每一步必须是对象")
        op = step.get("op")
        count = _positive_count(step)
        color = step.get("color")
        if op == "take_gem" and not discarding:
            if color not in engine.COLORS or count is None:
                return _invalid(tx, index, "INVALID_GEM_STEP", "take_gem 需要有效颜色和正整数 count")
            if supply.get(color, 0) < count:
                return _invalid(tx, index, "GEM_UNAVAILABLE", f"供应中没有足够的 {color}", available={color: supply.get(color, 0)}, required={color: count})
            supply[color] -= count
            player_gems[color] += count
            taken[color] += count
        elif op == "discard_gem":
            discarding = True
            if color not in engine.ALL_GEMS or count is None:
                return _invalid(tx, index, "INVALID_DISCARD_STEP", "discard_gem 需要有效颜色和正整数 count")
            if player_gems.get(color, 0) < count:
                return _invalid(tx, index, "INSUFFICIENT_GEMS", f"无法弃掉{count}个{color}宝石：当前只有{player_gems.get(color, 0)}个", available={color: player_gems.get(color, 0)}, required={color: count})
            player_gems[color] -= count
            supply[color] += count
            discarded[color] += count
        else:
            return _invalid(tx, index, "OUT_OF_ORDER_OPERATION", "取宝石事务只能先 take_gem，后 discard_gem")

    nonzero = {c: n for c, n in taken.items() if n}
    legal_take = (
        len(nonzero) == 3 and all(n == 1 for n in nonzero.values())
    ) or (
        len(nonzero) == 1
        and next(iter(nonzero.values()), 0) == 2
        and original_supply[next(iter(nonzero), "")] >= 4
    )
    if not legal_take:
        return _invalid(tx, len(steps), "ILLEGAL_GEM_COMBINATION", "必须拿三种不同颜色各1个，或供应原有至少4个时拿同色2个")
    required_discard = max(0, initial_total + sum(taken.values()) - 10)
    actual_discard = sum(discarded.values())
    if actual_discard != required_discard or sum(player_gems.values()) > 10:
        return _invalid(tx, len(steps), "HAND_LIMIT_EXCEEDED" if actual_discard < required_discard else "UNNECESSARY_DISCARD", f"本回合必须恰好弃掉 {required_discard} 个宝石", available={"discarded": actual_discard}, required={"discard": required_discard})

    canonical = {"type": "take_2", "color": next(iter(nonzero))} if len(nonzero) == 1 else {"type": "take_3", "gems": nonzero}
    canonical["discard"] = {c: n for c, n in discarded.items() if n}
    return _commit_result(state, pid, tx, canonical)


def _validate_buy(state: dict, pid: int, tx: dict) -> TransactionResult:
    steps = tx["steps"]
    if len(steps) < 2 or steps[1].get("op") != "select_card":
        return _invalid(tx, 1, "EXPECTED_CARD_SELECTION", "buy_card 的第二步必须选择卡牌")
    card, reserved_index = _card_for_selection(state, pid, steps[1])
    if card is None:
        return _invalid(tx, 1, "CARD_UNAVAILABLE", "所选卡牌不在声明的位置")
    source = steps[1].get("source")
    player = state["playerstorage"][pid]
    raw_cost = card["cost"]
    required = (
        {c: int(raw_cost.get(c, 0) or 0) for c in engine.COLORS}
        if isinstance(raw_cost, dict) else engine.parse_cost(raw_cost)
    )
    discount = engine._get_discount(player)
    required = {c: max(0, required[c] - discount.get(c, 0)) for c in engine.COLORS}
    payment = {c: 0 for c in engine.ALL_GEMS}
    for index, step in enumerate(steps[2:], 2):
        if step.get("op") != "pay_gem":
            return _invalid(tx, index, "OUT_OF_ORDER_OPERATION", "选择卡牌后只能提交 pay_gem")
        color, count = step.get("color"), _positive_count(step)
        if color not in engine.ALL_GEMS or count is None:
            return _invalid(tx, index, "INVALID_PAYMENT_STEP", "pay_gem 需要有效颜色和正整数 count")
        if payment[color] + count > player["gems"].get(color, 0):
            return _invalid(tx, index, "INSUFFICIENT_GEMS", f"没有足够的 {color} 用于支付", available={color: player["gems"].get(color, 0)}, required={color: payment[color] + count})
        payment[color] += count
    colored_shortfall = 0
    for color in engine.COLORS:
        if payment[color] > required[color]:
            return _invalid(tx, len(steps), "INVALID_PAYMENT", f"{color} 支付超过折扣后费用", available={color: payment[color]}, required={color: required[color]})
        colored_shortfall += required[color] - payment[color]
    if payment[engine.GOLD] != colored_shortfall:
        return _invalid(tx, len(steps), "INVALID_PAYMENT", "彩色宝石与黄金必须恰好支付折扣后费用", available=payment, required={**required, "G": colored_shortfall})
    canonical = {
        "type": "buy_market" if source == "market" else "buy_reserved",
        "payment": payment,
        "cardType": card.get("color", "?"),
        "points": card.get("points", 0),
        "cost": card["cost"],
    }
    if source == "market":
        canonical["cardId"] = card["id"]
        canonical["lvl"] = card.get("lvl", 1)
    else:
        canonical["cardIndex"] = reserved_index
        canonical["cardId"] = card.get("id")
    return _commit_result(state, pid, tx, canonical)


def _validate_reserve(state: dict, pid: int, tx: dict, initial_total: int) -> TransactionResult:
    steps = tx["steps"]
    player = state["playerstorage"][pid]
    if len(player.get("reservedCards", [])) >= 3:
        return _invalid(tx, 1, "RESERVE_LIMIT", "最多保留三张卡牌")
    if len(steps) < 2:
        return _invalid(tx, 1, "EXPECTED_RESERVE_TARGET", "reserve_card 必须选择卡牌或牌堆")
    target = steps[1]
    if target.get("op") == "select_card":
        card, _ = _card_for_selection(state, pid, target)
        if target.get("source") != "market" or card is None:
            return _invalid(tx, 1, "CARD_UNAVAILABLE", "只能保留当前市场中的卡牌")
        canonical = {"type": "reserve_market", "cardId": card["id"], "cardType": card.get("color", "?"), "points": card.get("points", 0), "lvl": card.get("lvl", 1)}
    elif target.get("op") == "select_deck":
        level = target.get("level")
        if level not in (1, 2, 3) or not engine._deck_has_cards(state, level):
            return _invalid(tx, 1, "DECK_UNAVAILABLE", "所选等级牌堆不可用")
        canonical = {"type": "reserve_deck", "lvl": level}
    else:
        return _invalid(tx, 1, "EXPECTED_RESERVE_TARGET", "reserve_card 的第二步必须是 select_card 或 select_deck")
    take_gold = state["gamestorage"]["gems"].get(engine.GOLD, 0) > 0
    canonical["takeGold"] = take_gold
    discarded = {c: 0 for c in engine.ALL_GEMS}
    simulated_gems = copy.deepcopy(player["gems"])
    if take_gold:
        simulated_gems[engine.GOLD] += 1
    for index, step in enumerate(steps[2:], 2):
        if step.get("op") != "discard_gem":
            return _invalid(tx, index, "OUT_OF_ORDER_OPERATION", "保留目标后只能提交 discard_gem")
        color, count = step.get("color"), _positive_count(step)
        if color not in engine.ALL_GEMS or count is None:
            return _invalid(tx, index, "INVALID_DISCARD_STEP", "discard_gem 需要有效颜色和正整数 count")
        if simulated_gems.get(color, 0) < count:
            return _invalid(tx, index, "INSUFFICIENT_GEMS", f"没有足够的 {color} 可弃", available={color: simulated_gems.get(color, 0)}, required={color: count})
        simulated_gems[color] -= count
        discarded[color] += count
    required_discard = max(0, initial_total + int(take_gold) - 10)
    if sum(discarded.values()) != required_discard:
        return _invalid(tx, len(steps), "HAND_LIMIT_EXCEEDED" if sum(discarded.values()) < required_discard else "UNNECESSARY_DISCARD", f"本回合必须恰好弃掉 {required_discard} 个宝石")
    canonical["discard"] = {c: n for c, n in discarded.items() if n}
    return _commit_result(state, pid, tx, canonical)


def _commit_result(state: dict, pid: int, tx: dict, canonical: dict) -> TransactionResult:
    next_state = None
    if not state.get("_browser_validation_only"):
        next_state = copy.deepcopy(state)
        engine.apply_action(next_state, pid, canonical)
    effects = ({"type": canonical["type"], "player": pid},)
    return TransactionResult(
        status="committed",
        transaction=copy.deepcopy(tx),
        canonical_action=copy.deepcopy(canonical),
        effects=effects,
        accepted_steps=tuple(range(len(tx["steps"]))),
        state_changed=True,
        instruction="事务已提交；本回合不要再次调用 BgAct。",
        next_state=next_state,
    )


def commit_transaction(state: dict, result: TransactionResult) -> dict:
    """Return the validated next state; never mutate the caller's snapshot."""
    if result.status != "committed" or result.next_state is None:
        raise ValueError("cannot commit an invalid transaction")
    return copy.deepcopy(result.next_state)
