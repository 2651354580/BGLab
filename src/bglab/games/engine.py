""

from __future__ import annotations

import copy
import random
import re
from dataclasses import dataclass, field
from typing import Any

# ═══════════════════════════════════════════════
# Key Map
# ═══════════════════════════════════════════════
# C = 钻石/白 (Diamond)
# S = 蓝宝石 (Sapphire)
# E = 绿宝石 (Emerald)
# R = 红宝石 (Ruby)
# O = 黑玛瑙 (Onyx)
# G = 黄金/万能 (Gold/Joker)

COLORS = ["C", "S", "E", "R", "O"]
GOLD = "G"
ALL_GEMS = COLORS + [GOLD]

COST_RE = re.compile(r"([CESRO])(\d+)")


def parse_cost(s: str) -> dict[str, int]:
    cost = {c: 0 for c in COLORS}
    for m in COST_RE.finditer(s):
        cost[m.group(1)] = int(m.group(2))
    return cost


# ── Card Data ──
CARD_DATA: dict[str, list[dict]] = {
    "O": [
        {"cost": "CE1R1S1", "points": 0, "lvl": 1}, {"cost": "CE2R1S1", "points": 0, "lvl": 1},
        {"cost": "C2S2R1", "points": 0, "lvl": 1}, {"cost": "E2R1", "points": 0, "lvl": 1},
        {"cost": "C2E2", "points": 0, "lvl": 1}, {"cost": "E3", "points": 0, "lvl": 1},
        {"cost": "C0E1O1R3", "points": 0, "lvl": 1}, {"cost": "S4", "points": 1, "lvl": 1},
        {"cost": "C3E2S2", "points": 1, "lvl": 2}, {"cost": "C3E3O2", "points": 1, "lvl": 2},
        {"cost": "E4R2S1", "points": 2, "lvl": 2}, {"cost": "E5R3", "points": 2, "lvl": 2},
        {"cost": "C5", "points": 2, "lvl": 2}, {"cost": "O6", "points": 3, "lvl": 2},
        {"cost": "C3E5R3S3", "points": 3, "lvl": 3}, {"cost": "E3O3R6", "points": 4, "lvl": 3},
        {"cost": "R7", "points": 4, "lvl": 3}, {"cost": "R7O3", "points": 5, "lvl": 3},
    ],
    "S": [
        {"cost": "C1E1O1R1", "points": 0, "lvl": 1}, {"cost": "C1O2", "points": 0, "lvl": 1},
        {"cost": "E2O2", "points": 0, "lvl": 1}, {"cost": "C1E2R2", "points": 0, "lvl": 1},
        {"cost": "C1E1O1R2", "points": 0, "lvl": 1}, {"cost": "O3", "points": 0, "lvl": 1},
        {"cost": "E3R1S1", "points": 0, "lvl": 1}, {"cost": "R4", "points": 1, "lvl": 1},
        {"cost": "C2O4R1", "points": 2, "lvl": 2}, {"cost": "E2R3S2", "points": 1, "lvl": 2},
        {"cost": "E3O3S2", "points": 1, "lvl": 2}, {"cost": "S5", "points": 2, "lvl": 2},
        {"cost": "C5S3", "points": 2, "lvl": 2}, {"cost": "S6", "points": 3, "lvl": 2},
        {"cost": "C3E3O5R3", "points": 3, "lvl": 3}, {"cost": "C6O3S3", "points": 4, "lvl": 3},
        {"cost": "C7", "points": 4, "lvl": 3}, {"cost": "C7S3", "points": 5, "lvl": 3},
    ],
    "E": [
        {"cost": "C1O1R1S1", "points": 0, "lvl": 1}, {"cost": "C1O2R1S1", "points": 0, "lvl": 1},
        {"cost": "R2S2", "points": 0, "lvl": 1}, {"cost": "O2R2S1", "points": 0, "lvl": 1},
        {"cost": "C2S1", "points": 0, "lvl": 1}, {"cost": "C1E1S3", "points": 0, "lvl": 1},
        {"cost": "R3", "points": 0, "lvl": 1}, {"cost": "O4", "points": 1, "lvl": 1},
        {"cost": "C4O1S2", "points": 2, "lvl": 2}, {"cost": "C3E2R3", "points": 1, "lvl": 2},
        {"cost": "C2O2S3", "points": 1, "lvl": 2}, {"cost": "E3S5", "points": 2, "lvl": 2},
        {"cost": "E5", "points": 2, "lvl": 2}, {"cost": "E6", "points": 3, "lvl": 2},
        {"cost": "C3E3O3S3", "points": 3, "lvl": 3}, {"cost": "C6E3S3", "points": 4, "lvl": 3},
        {"cost": "S7", "points": 4, "lvl": 3}, {"cost": "S7E3", "points": 5, "lvl": 3},
    ],
    "R": [
        {"cost": "C1E1O1S1", "points": 0, "lvl": 1}, {"cost": "C2E1O2", "points": 0, "lvl": 1},
        {"cost": "C2E1O1S1", "points": 0, "lvl": 1}, {"cost": "C1O3R1", "points": 0, "lvl": 1},
        {"cost": "E1S2", "points": 0, "lvl": 1}, {"cost": "C2R2", "points": 0, "lvl": 1},
        {"cost": "C3", "points": 0, "lvl": 1}, {"cost": "C4", "points": 1, "lvl": 1},
        {"cost": "C2O3R2", "points": 1, "lvl": 2}, {"cost": "O3R2S3", "points": 1, "lvl": 2},
        {"cost": "C3O5", "points": 2, "lvl": 2}, {"cost": "C1E2S4", "points": 2, "lvl": 2},
        {"cost": "O5", "points": 2, "lvl": 2}, {"cost": "R6", "points": 3, "lvl": 2},
        {"cost": "C3E3O3S5", "points": 3, "lvl": 3}, {"cost": "E6R3S3", "points": 4, "lvl": 3},
        {"cost": "E7", "points": 4, "lvl": 3}, {"cost": "E7R3", "points": 5, "lvl": 3},
    ],
    "C": [
        {"cost": "C3O1S1", "points": 0, "lvl": 1}, {"cost": "C0E1O1R1S1", "points": 0, "lvl": 1},
        {"cost": "C0E2O1R1S1", "points": 0, "lvl": 1}, {"cost": "O2S2", "points": 0, "lvl": 1},
        {"cost": "C0E2O1S2", "points": 0, "lvl": 1}, {"cost": "O1R2", "points": 0, "lvl": 1},
        {"cost": "S3", "points": 0, "lvl": 1}, {"cost": "E4", "points": 1, "lvl": 1},
        {"cost": "E3O2R2", "points": 1, "lvl": 2}, {"cost": "C2R3S3", "points": 1, "lvl": 2},
        {"cost": "E1O2R4", "points": 2, "lvl": 2}, {"cost": "R5", "points": 2, "lvl": 2},
        {"cost": "R5O3", "points": 2, "lvl": 2}, {"cost": "C6", "points": 3, "lvl": 2},
        {"cost": "C3E3O3S3", "points": 3, "lvl": 3}, {"cost": "C3E3O6", "points": 4, "lvl": 3},
        {"cost": "O7", "points": 4, "lvl": 3}, {"cost": "C3O7", "points": 5, "lvl": 3},
    ],
}

NOBLE_DATA: list[dict[str, int]] = [
    {"C": 0, "S": 0, "E": 4, "R": 4, "O": 0},
    {"C": 4, "S": 0, "E": 4, "R": 0, "O": 0},
    {"C": 0, "S": 3, "E": 3, "R": 3, "O": 0},
    {"C": 3, "S": 3, "E": 3, "R": 0, "O": 0},
    {"C": 4, "S": 4, "E": 0, "R": 0, "O": 0},
    {"C": 4, "S": 0, "E": 0, "R": 0, "O": 4},
    {"C": 3, "S": 3, "E": 0, "R": 0, "O": 3},
    {"C": 0, "S": 0, "E": 0, "R": 4, "O": 4},
    {"C": 0, "S": 4, "E": 4, "R": 0, "O": 0},
    {"C": 3, "S": 0, "E": 3, "R": 3, "O": 0},
]

GEM_SUPPLY = {2: 4, 3: 5, 4: 7}
WIN_SCORE = 15


# ═══════════════════════════════════════════════
# Engine
# ═══════════════════════════════════════════════

def init(pc: int, *, seed: int | None = None) -> dict:
    """初始化 Splendor 游戏状态。"""
    rng = random.Random(seed)

    # Match the browser engine: shuffle the complete card set, then split it
    # into the three level decks.  Bonus colour is card data, not a deck key.
    all_cards = []
    for color in COLORS:
        cards = copy.deepcopy(CARD_DATA[color])
        for card in cards:
            card["color"] = color
            all_cards.append(card)
    rng.shuffle(all_cards)
    decks = {str(lvl): [] for lvl in (1, 2, 3)}
    for card in all_cards:
        decks[str(card["lvl"])].append(card)

    # 市场: 4 列 × 3 层 = 12 张正面朝上
    market = []
    for lvl in [1, 2, 3]:
        for _ in range(4):
            card = _draw_from_deck(decks, lvl)
            if card:
                card["location"] = f"market_lvl{lvl}"
                market.append(card)
    for card_id, card in enumerate(market, start=1001):
        card["id"] = card_id

    # 贵族: 随机选 pc+1 张
    nobles_copy = copy.deepcopy(NOBLE_DATA)
    rng.shuffle(nobles_copy)
    nobles = nobles_copy[:pc + 1]
    for i, n in enumerate(nobles):
        n["id"] = i + 1

    # 宝石供应
    supply = {c: GEM_SUPPLY.get(pc, 5) for c in COLORS}
    supply[GOLD] = 5

    # 玩家
    players = [{
        "pid": i,
        "gems": {c: 0 for c in ALL_GEMS},
        "boughtCards": [],
        "reservedCards": [],
        "boughtNobles": [],
        "score": 0,
    } for i in range(pc)]

    state = {
        "wrapper": {"phase": "playing", "turn": 0, "currentPlayer": 0, "pc": pc},
        "gamestorage": {"gems": supply, "nobles": nobles},
        "playerstorage": players,
        "decks": copy.deepcopy(decks),
        "market": market,
        "lastRound": False,
        "lastRoundStartPlayer": -1,
        # Refill/reserve IDs must continue after the initial market IDs. Starting
        # again at 1000 aliases a newly drawn card with an existing market card.
        "_nextId": max((card["id"] for card in market), default=1000),
        "_rng_state": rng.getstate(),
    }

    return state


def get_legal_actions(state: dict, pid: int) -> list[dict]:
    """获取合法动作列表。"""
    player = state["playerstorage"][pid]
    supply = state["gamestorage"]["gems"]
    market = state["market"]
    actions = []

    # 手牌限制
    gem_count = sum(player["gems"].values())
    can_take = gem_count <= 10

    # 1. Take 3 different gems — enumerate all combinations so AI can copy verbatim
    if can_take:
        available = [c for c in COLORS if supply.get(c, 0) > 0]
        if len(available) >= 3:
            for i in range(len(available)):
                for j in range(i + 1, len(available)):
                    for k in range(j + 1, len(available)):
                        actions.append({
                            "type": "take_3",
                            "gems": {available[i]: 1, available[j]: 1, available[k]: 1},
                        })

    # 2. Take 2 same gems
    if can_take:
        for c in COLORS:
            if supply.get(c, 0) >= 4:
                actions.append({"type": "take_2", "color": c})

    # 3. Buy market cards
    discount = _get_discount(player)
    for card in market:
        if _can_afford(player["gems"], card["cost"], discount):
            payment = _calc_auto_payment(player["gems"], card["cost"], discount)
            actions.append({
                "type": "buy_market",
                "cardId": card["id"],
                "cardType": card.get("color", "?"),
                "points": card.get("points", 0),
                "lvl": card.get("lvl", 1),
                "cost": card["cost"],
                "payment": payment,
            })

    # 4. Buy reserved cards
    for i, card in enumerate(player["reservedCards"]):
        if _can_afford(player["gems"], card["cost"], discount):
            payment = _calc_auto_payment(player["gems"], card["cost"], discount)
            actions.append({
                "type": "buy_reserved",
                "cardIndex": i,
                "cardType": card.get("color", "?"),
                "points": card.get("points", 0),
                "cost": card["cost"],
                "payment": payment,
            })

    # 5. Reserve market cards
    if len(player["reservedCards"]) < 3 and any(market):
        for card in market:
            actions.append({
                "type": "reserve_market",
                "cardId": card["id"],
                "cardType": card.get("color", "?"),
                "points": card.get("points", 0),
                "lvl": card.get("lvl", 1),
                "takeGold": supply.get(GOLD, 0) > 0,
            })

    # 6. Reserve from deck (blind)
    if len(player["reservedCards"]) < 3:
        for lvl in [1, 2, 3]:
            if _deck_has_cards(state, lvl):
                actions.append({
                    "type": "reserve_deck",
                    "lvl": lvl,
                    "takeGold": supply.get(GOLD, 0) > 0,
                })

    return actions


def apply_action(state: dict, pid: int, action: dict) -> dict:
    """执行动作，返回更新后的 state。"""
    player = state["playerstorage"][pid]
    atype = action.get("type", "")

    if atype == "take_3":
        gems = _normalize_gems(action)
        for c, n in gems.items():
            if n > 0:
                player["gems"][c] = player["gems"].get(c, 0) + n
                state["gamestorage"]["gems"][c] = state["gamestorage"]["gems"].get(c, 0) - n

    elif atype == "take_2":
        c = action.get("color", "")
        n = min(2, state["gamestorage"]["gems"].get(c, 0))
        player["gems"][c] = player["gems"].get(c, 0) + n
        state["gamestorage"]["gems"][c] = state["gamestorage"]["gems"].get(c, 0) - n

    elif atype in ("buy_market", "buy_reserved"):
        # Payment
        payment = action.get("payment", {})
        for c, n in payment.items():
            player["gems"][c] = player["gems"].get(c, 0) - n
            state["gamestorage"]["gems"][c] = state["gamestorage"]["gems"].get(c, 0) + n

        if atype == "buy_market":
            card = _remove_card_from_market(state, action["cardId"])
        else:
            card = player["reservedCards"].pop(action["cardIndex"])
        if card:
            player["boughtCards"].append(card)
            player["score"] += card.get("points", 0)

    elif atype == "reserve_market":
        card = _remove_card_from_market(state, action["cardId"])
        if card:
            player["reservedCards"].append(card)
        if action.get("takeGold") and state["gamestorage"]["gems"].get(GOLD, 0) > 0:
            player["gems"][GOLD] = player["gems"].get(GOLD, 0) + 1
            state["gamestorage"]["gems"][GOLD] -= 1

    elif atype == "reserve_deck":
        lvl = action.get("lvl", 1)
        card = _draw_from_deck(state.get("decks", {}), lvl)
        if card:
            card["location"] = "reserved"
            card["id"] = _next_id(state)
            player["reservedCards"].append(card)
        if action.get("takeGold") and state["gamestorage"]["gems"].get(GOLD, 0) > 0:
            player["gems"][GOLD] = player["gems"].get(GOLD, 0) + 1
            state["gamestorage"]["gems"][GOLD] -= 1

    # Every non-deterministic discard must be explicit. Validation happens in
    # the transaction layer; this low-level compatibility action only applies
    # a supplied discard map and never chooses one for the player.
    explicit_discard = action.get("discard")
    if explicit_discard is not None:
        for c, n in explicit_discard.items():
            if c in ALL_GEMS and n > 0:
                player["gems"][c] -= n
                state["gamestorage"]["gems"][c] += n
    # ── Post-action: check nobles, fill market, advance turn ──
    _check_nobles(state, pid)
    _fill_market(state)
    _advance_turn(state, pid)

    return state


def is_game_over(state: dict) -> bool:
    phase = state["wrapper"].get("phase", "playing")
    return phase == "finished"


def get_winner(state: dict) -> int | None:
    players = state["playerstorage"]
    if not players:
        return None
    best = max(players, key=lambda p: (p["score"], -len(p["boughtCards"])))
    return best["pid"]


# ═══════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════

def _normalize_gems(action: dict) -> dict[str, int]:
    """兼容 take_3 的多种格式：gems / colors / availableColors"""
    gems = action.get("gems", {})
    if not gems:
        colors = action.get("colors", []) or action.get("availableColors", [])
        if colors and isinstance(colors, list):
            gems = {c: 1 for c in colors if c in COLORS}
            if not gems:
                gems = {c: 1 for c in colors[:3] if c in COLORS}
    return gems

_next_id_counter = 1000


def _next_id(state: dict | None = None) -> int:
    global _next_id_counter
    if state and "_nextId" in state:
        state["_nextId"] += 1
        return state["_nextId"]
    _next_id_counter += 1
    return _next_id_counter


def _draw_from_deck(decks: dict, lvl: int) -> dict | None:
    # Current snapshots use one shared deck per level, as the browser does.
    cards = decks.get(str(lvl), decks.get(lvl, []))
    if cards:
        return cards.pop()

    # Compatibility for games saved before level decks were introduced.
    for color in COLORS:
        cards = decks.get(color, [])
        if cards:
            idx = next((i for i, c in enumerate(cards) if c.get("lvl") == lvl), -1)
            if idx >= 0:
                card = cards.pop(idx)
                card["color"] = color  # card inherits its deck color
                return card
    return None


def _deck_has_cards(state: dict, lvl: int) -> bool:
    decks = state.get("decks", {})
    if decks.get(str(lvl), decks.get(lvl, [])):
        return True
    drawcounts = state.get("drawcounts") or state.get("gamestorage", {}).get("drawcounts", [])
    if isinstance(drawcounts, (list, tuple)) and len(drawcounts) > lvl and drawcounts[lvl] > 0:
        return True
    if isinstance(drawcounts, dict) and int(drawcounts.get(str(lvl), drawcounts.get(lvl, 0)) or 0) > 0:
        return True
    # Compatibility for legacy colour-keyed snapshots.
    for color in COLORS:
        if any(c.get("lvl") == lvl for c in decks.get(color, [])):
            return True
    return False


def _get_discount(player: dict) -> dict[str, int]:
    discount = {c: 0 for c in COLORS}
    for card in player.get("boughtCards", []):
        ct = card.get("color", "")
        if ct in discount:
            discount[ct] += 1
    return discount


def _can_afford(gems: dict, cost_str: str, discount: dict) -> bool:
    cost = parse_cost(cost_str)
    gold = gems.get(GOLD, 0)
    shortfall = 0
    for c in COLORS:
        need = max(0, cost[c] - discount.get(c, 0))
        have = gems.get(c, 0)
        if have < need:
            shortfall += need - have
    return shortfall <= gold


def _calc_auto_payment(gems: dict, cost_str: str, discount: dict) -> dict[str, int]:
    cost = parse_cost(cost_str)
    payment = {c: 0 for c in ALL_GEMS}
    gold_used = 0
    for c in COLORS:
        need = max(0, cost[c] - discount.get(c, 0))
        use = min(need, gems.get(c, 0))
        payment[c] = use
        gold_used += need - use
    payment[GOLD] = gold_used
    return payment


def _remove_card_from_market(state: dict, card_id: int) -> dict | None:
    market = state.get("market", [])
    for i, c in enumerate(market):
        if c.get("id") == card_id:
            return market.pop(i)
    return None


def _fill_market(state: dict):
    market = state.get("market", [])
    decks = state.get("decks", {})
    # 市场有 12 个槽位（4×3），找到空缺
    slots_per_lvl = {1: 0, 2: 0, 3: 0}
    for c in market:
        lvl = c.get("lvl", 1)
        slots_per_lvl[lvl] = slots_per_lvl.get(lvl, 0) + 1
    for lvl in [1, 2, 3]:
        while slots_per_lvl[lvl] < 4:
            card = _draw_from_deck(decks, lvl)
            if not card:
                break
            card["location"] = f"market_lvl{lvl}"
            card["id"] = _next_id(state)
            market.append(card)
            slots_per_lvl[lvl] += 1


def _check_nobles(state: dict, pid: int):
    player = state["playerstorage"][pid]
    discount = _get_discount(player)
    nobles = state["gamestorage"].get("nobles", [])
    for noble in nobles:
        req = {k: v for k, v in noble.items() if k in COLORS}
        if all(discount.get(c, 0) >= req.get(c, 0) for c in COLORS):
            player["boughtNobles"].append(noble)
            player["score"] += 3
            nobles.remove(noble)


def _advance_turn(state: dict, pid: int):
    pc = state["wrapper"]["pc"]
    # Check win condition
    player = state["playerstorage"][pid]
    if player["score"] >= WIN_SCORE:
        if not state.get("lastRound"):
            state["lastRound"] = True
            state["lastRoundStartPlayer"] = pid

    state["wrapper"]["turn"] = state["wrapper"].get("turn", 0) + 1
    # Splendor finishes at the end of the current seating round, so every
    # player receives the same number of turns. If the last seat triggers 15,
    # the game ends immediately; it must not grant P0 an extra turn.
    if state.get("lastRound") and pid == pc - 1:
        state["wrapper"]["phase"] = "finished"
        return
    state["wrapper"]["currentPlayer"] = (pid + 1) % pc


def snapshot(state: dict) -> dict:
    return copy.deepcopy(state)
