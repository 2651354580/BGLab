"""BG tools — compatible with both Python engine state and frontend game.js WS state.

Python engine: state.market list, state.gamestorage.gems, state.wrapper
Frontend WS:   state.gamestorage.cards (filtered by location), state.gamestorage.gems, state.wrapper
"""

from __future__ import annotations

import copy
import json, re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from bglab.games.features import DEFAULT_GAME_PROFILE, GameFeatureProfile
from bglab.games.plans import create_plan_tool

from bglab.games.tools.profiles import create_profile_tools, register_action_profile
from bglab.tools.base import Tool

if TYPE_CHECKING:
    from bglab.games.registry import GameDefinition

COLORS = ["C", "S", "E", "R", "O"]


class AttemptClosedError(RuntimeError):
    """Raised by an action sink when its captured DecisionFrame is fenced."""

    code = "STALE_ATTEMPT_REJECTED"

_MODEL_VISIBLE_OUTCOME_COVERAGE_FIELDS = (
    "coverageStatus",
    "enumerationComplete",
    "code",
    "decisionId",
    "optionalExitCount",
    "remainingResourceRanges",
    "observedOptionalExitCount",
    "observedRemainingResourceRanges",
    "observedOutcomeRanges",
    "triggeredFamilies",
    "triggeredFamiliesObserved",
)


def model_visible_outcome_coverage(summary: Any) -> dict[str, Any]:
    """Project coverage facts without graph-size or traversal signals."""
    if not isinstance(summary, dict):
        return {"coverageStatus": "not_explored", "enumerationComplete": False}
    return {
        key: copy.deepcopy(summary[key])
        for key in _MODEL_VISIBLE_OUTCOME_COVERAGE_FIELDS
        if key in summary
    }


def _exact_action_match(submitted: Any, legal: Any) -> bool:
    """Structural equality with type checks, so 1.0 cannot impersonate integer 1."""
    if type(submitted) is not type(legal):
        return False
    if isinstance(legal, dict):
        return submitted.keys() == legal.keys() and all(
            _exact_action_match(submitted[key], legal[key]) for key in legal
        )
    if isinstance(legal, list):
        return len(submitted) == len(legal) and all(
            _exact_action_match(left, right) for left, right in zip(submitted, legal)
        )
    return submitted == legal
COLOR_NAME = {"C": "White", "S": "Blue", "E": "Green", "R": "Red", "O": "Black", "G": "Gold"}


def _gs(state: dict) -> dict:
    return state.get("gamestorage", {})

def _players(state: dict) -> list[dict]:
    return state.get("playerstorage", [])

def _market(state: dict) -> list[dict]:
    """Compatible with both Python engine and frontend game.js state format."""
    if "market" in state and state["market"]:
        return state["market"]
    cards = _gs(state).get("cards", [])
    return [c for c in cards if isinstance(c, dict) and c.get("location", "").startswith("market_")]

def _player(state: dict, seat: int) -> dict:
    return _players(state)[seat]

def _player_id(player: dict, index: int) -> int:
    """Return the explicit engine pid or the browser array-index pid."""
    pid = player.get("pid", index)
    return pid if isinstance(pid, int) and not isinstance(pid, bool) else index

def _nobles(state: dict) -> list[dict]:
    n = _gs(state).get("nobles", [])
    if n: return n
    for c in _gs(state).get("cards", []):
        if isinstance(c, dict) and c.get("location") == "noble":
            n.append(c)
    return n

def _turn(state: dict) -> int:
    w = state.get("wrapper", {})
    return w.get("turn", 0) if w else 0

def _phase(state: dict) -> str:
    return state.get("wrapper", {}).get("phase", state.get("phase", "?"))


# ═══════════════════════════════════════════════
# Core tools
# ═══════════════════════════════════════════════

def create_observe_tool(ctx: dict) -> Tool:
    def call(args: dict) -> str:
        del args
        frame = ctx.get("_decision_frame")
        if not isinstance(frame, dict):
            return "OBSERVE_UNAVAILABLE: 当前 DecisionFrame 尚未绑定。"
        from bglab.games.decision_surface import render_runtime_decision_frame

        return render_runtime_decision_frame(frame)

    return Tool(
        name="BgObserve",
        description=(
            "Read-only seat-authorized DecisionFrame re-render for an unclear turn snapshot; it does not "
            "complete the active decision."
        ),
        prompt=(
            "The authoritative current snapshot is already present in every real-turn "
            "message, so normally read that and do not call this tool. Use it only to "
            "re-render the same public resources, market, scores, player information, and enabled decision facts "
            "when the injected snapshot is genuinely missing or hard to interpret. This tool "
            "only reads facts and does not complete the decision; continue observing or check, "
            "or commit afterwards."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        call=call, is_read_only=True, always_load=True,
    )
def create_legal_moves_tool(ctx: dict) -> Tool:
    def call(args: dict) -> str:
        legal = ctx.get("_legal_actions", [])
        seat = ctx.get("_seat", 0)
        lines = [f"Legal actions for P{seat} ({len(legal)} total):"]
        by_type: dict[str, list] = {}
        for a in legal:
            by_type.setdefault(a.get("type", "?"), []).append(a)
        for t, acts in by_type.items():
            lines.append(f"\n=== {t} ({len(acts)}) ===")
            for i, a in enumerate(acts[:8]):
                lines.append(f"  {i+1}. {json.dumps(a, ensure_ascii=False)}")
            if len(acts) > 8:
                lines.append(f"  ... and {len(acts)-8} more")
        return "\n".join(lines)

    return Tool(
        name="BgLegalMoves",
        description="List all legal actions.",
        prompt=(
            "List ALL legal actions available to you this turn, grouped by type. "
            "The list is exhaustive. Your chosen action MUST come from this list "
            "verbatim — do not modify field names or structure."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        call=call, is_read_only=True, always_load=True,
    )


def create_act_tool(ctx: dict) -> Tool:
    """Create an internal slot replaced by the semantic-v2 BgAct adapter."""
    return Tool(
        name="BgAct",
        description="Internal BgAct slot; current runtime replaces this with semantic-v2.",
        prompt="Use the current semantic-v2 Check/Commit tool definition.",
        call=lambda _args: "BG_ACT_SLOT_NOT_BOUND",
        parameters={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        is_read_only=False,
        always_load=True,
    )


def create_chat_tool(ctx: dict) -> Tool:
    def call(args: dict) -> str:
        target = args.get("to")
        message = str(args.get("message", "")).strip()
        if target is None or target == "":
            return "Error: to is required (use a local player pid such as 0 or P0)."
        if str(target).strip().lower() in {"leader", "lead", "code-agent", "code_agent"}:
            return "Error: BgChat can only message players in this game, never the Leader."
        if not message:
            return "Error: message is required."
        if len(message) > 500:
            return "Error: game chat message must be at most 500 characters."
        sink = ctx.get("_chat_sink")
        if sink is not None:
            kwargs = {'reply_to': args['reply_to']} if args.get('reply_to') is not None else {}
            try:
                result = sink(target, message, **kwargs)
            except (ValueError, RuntimeError) as exc:
                return f'Error: {exc}'
            return str(result or f"Game chat queued for {target}.")
        ctx.setdefault("_outgoing_chats", []).append({"to": target, "message": message})
        return f"Game chat queued for {target}."

    return Tool(
        name="BgChat",
        description="Send a short message to another local player in this game.",
        prompt=(
            "Chat with a human or AI player in the current game. Messages cannot target "
            "the Code Agent Leader and cannot create tasks or teams. AI recipients see the "
            "message as a <game-chat> attachment on their next real turn. To answer a received "
            "message, set reply_to to its id; that acknowledges delivery and prevents duplicate "
            "replies. A reply does not replace your current game action. Prefer replying before Commit."
        ),
        parameters={
            "type": "object",
            "properties": {
                "to": {
                    "description": "Target local player pid, for example 0 or P0.",
                    "anyOf": [{"type": "integer"}, {"type": "string"}],
                },
                "message": {"type": "string", "maxLength": 500},
                "reply_to": {"type": "integer", "description": "The received game-chat id when replying."},
            },
            "required": ["to", "message"],
        },
        call=call,
        is_read_only=False,
        always_load=True,
    )


def create_card_eval_tool(ctx: dict) -> Tool:
    def call(args: dict) -> str:
        state = ctx.get("_state", {})
        seat = ctx.get("_seat", 0)
        p = _player(state, seat)
        discount = _calc_discount(state, p)
        gems = p.get("gems", {k: p.get(k, 0) for k in COLORS + ["G"]})
        market = _market(state)
        lines = ["=== Card Facts ===", "", f"Your gems: {gems}", f"Discount: {discount}", ""]
        for c in market:
            fact = _card_payment_fact(c, discount, gems)
            cid = c.get("id", "?")
            lvl = c.get("lvl", c.get("level", "?"))
            pts = c.get("points", 0) or 0
            color = c.get("color", c.get("type", "?"))
            lines.append(
                f"  id={cid} lvl={lvl} pts={pts} bonus={color} "
                f"printed_cost={fact['printed_cost']} effective_cost={fact['effective_cost']} "
                f"colored_shortfall={fact['colored_shortfall']} gold_available={fact['gold_available']} "
                f"affordable={str(fact['affordable']).lower()}"
            )
        return "\n".join(lines)

    return Tool(
        name="BgCardEval",
        description="Show factual cost and affordability data for market cards.",
        prompt=(
            "Show each market card's printed cost, cost after permanent discounts, "
            "colored-token shortfall, available gold, points, tier, and bonus color. "
            "The tool does not rank cards or choose a strategy."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        call=call, is_read_only=True, always_load=True,
    )


def create_board_eval_tool(ctx: dict) -> Tool:
    def call(args: dict) -> str:
        state = ctx.get("_state", {})
        seat = ctx.get("_seat", 0)
        gs = _gs(state)
        supply = {k: gs.get(k, 0) for k in COLORS + ["G"]} if any(k in gs for k in COLORS) else gs.get("gems", {})
        lines = ["=== Board Evaluation ===", "", f"Supply: {supply}", f"Market: {len(_market(state))} cards", f"Nobles: {len(_nobles(state))}", ""]
        players = _players(state)
        for index, p in enumerate(players):
            pid = _player_id(p, index)
            me = " (YOU)" if pid == seat else ""
            score = p.get("score", 0) or sum(c.get("points", 0) for c in p.get("boughtCards", [])) + len(p.get("boughtNobles", [])) * 3
            gems = p.get("gems", {k: p.get(k, 0) for k in COLORS + ["G"]})
            lines.append(f"P{pid}{me}: {score}pts, bought={len(p.get('boughtCards',[]))}, gems={gems}")
        scores = [_pscore(p) for p in players]
        lines.append("")
        lines.append(f"Victory threshold: 15; triggered={any(score >= 15 for score in scores)}")
        lines.append("Distances to 15: " + ", ".join(f"P{_player_id(p, i)}={max(0, 15-scores[i])}" for i, p in enumerate(players)))
        return "\n".join(lines)

    return Tool(
        name="BgBoardEval",
        description="Show factual board, score, and victory-trigger data.",
        prompt=(
            "Show public board totals, scores, discounts, resource holdings, distance to "
            "15, and whether the final round has been triggered. It does not prescribe a strategy."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        call=call, is_read_only=True, always_load=True,
    )


def create_affordability_tool(ctx: dict) -> Tool:
    def call(args: dict) -> str:
        state = ctx.get("_state", {})
        seat = ctx.get("_seat", 0)
        p = _player(state, seat)
        discount = _calc_discount(state, p)
        gems = p.get("gems", {k: p.get(k, 0) for k in COLORS + ["G"]})
        gold = gems.get("G", 0)
        lines = ["=== Affordability Check ===", "", f"Your gems: {gems}", f"Discount: {discount}", f"Gold: {gold}", ""]
        lines.append("CARDS IN MARKET:")
        affordable = []
        almost = []
        for c in _market(state):
            fact = _card_payment_fact(c, discount, gems)
            need = fact["colored_shortfall"]
            info = f"  id={c.get('id','?')} pts={c.get('points',0)} lvl={c.get('lvl', c.get('level','?'))} cost={c.get('cost','?')}"
            chain = _purchase_chain_fact(c, "market", discount, gems, state)
            if chain is not None:
                affordable.append(
                    info + " -> affordable=true " + _purchase_chain_display(chain)
                )
            elif need <= gold + 2:
                almost.append(info + f" -> need {need-gold} more")
        for s in affordable: lines.append(s)
        if not affordable: lines.append("  (none - collect gems first)")
        if almost:
            lines.append(""); lines.append("ALMOST (1-2 gems away):")
            for s in almost: lines.append(s)
        lines.append(""); lines.append("CARDS IN RESERVE:")
        res = p.get("reservedCards", []) or p.get("storedCards", [])
        for i, c in enumerate(res):
            fact = _card_payment_fact(c, discount, gems)
            need = fact["colored_shortfall"]
            info = f"  [{i}] pts={c.get('points',0)} cost={c.get('cost','?')}"
            chain = _purchase_chain_fact(c, "reserved", discount, gems, state)
            if chain is not None:
                lines.append(
                    info + " -> affordable=true " + _purchase_chain_display(chain)
                )
            else: lines.append(info + f" -> need {need-gold} more")
        if not res: lines.append("  (none)")
        scores = [
            int(player.get("score", 0) or 0)
            for player in _players(state)
        ]
        if scores and max(scores) >= 8:
            lines.extend([
                "",
                "RACE CONTEXT: BgCanAfford evaluated only your seat. It contains "
                "no opponent affordability or threat facts.",
            ])
        return "\n".join(lines)

    return Tool(
        name="BgCanAfford",
        description="Check which cards you can afford right now.",
        prompt=(
            "Check exactly which market and reserved cards you can afford RIGHT NOW "
            "with your current resources + discounts. Fast affordability check — "
            "shows what you can buy immediately and what you're close to affording."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        call=call, is_read_only=True, always_load=True,
    )


def create_opponent_tool(ctx: dict) -> Tool:
    def call(args: dict) -> str:
        state = ctx.get("_state", {})
        seat = ctx.get("_seat", 0)
        lines = ["=== Opponent Analysis ===", ""]
        for index, p in enumerate(_players(state)):
            pid = _player_id(p, index)
            if pid == seat: continue
            gems = p.get("gems", {k: p.get(k, 0) for k in COLORS + ["G"]})
            score = p.get("score", 0) or sum(c.get("points", 0) for c in p.get("boughtCards", [])) + len(p.get("boughtNobles", [])) * 3
            lines.append(f"P{pid}: {score}pts, bought={len(p.get('boughtCards',[]))}, reserved={len(p.get('reservedCards',[])) or len(p.get('storedCards',[]))}, gems={gems}")
            discount = _calc_discount(state, p)
            lines.append(f"  Discounts: {discount}")
            buyable = [c for c in _market(state) if _purchase_chain_fact(
                c, "market", discount, gems,
            ) is not None]
            if buyable:
                lines.append("  Immediately affordable market cards:")
                for c in buyable:
                    noble_bonus = _noble_bonus_after_card(state, discount, c)
                    projected = score + int(c.get("points", 0) or 0) + noble_bonus
                    chain = _purchase_chain_fact(c, "market", discount, gems, state)
                    lines.append(
                        f"    id={c.get('id','?')} pts={c.get('points',0)} "
                        f"lvl={c.get('lvl', c.get('level','?'))} noble={noble_bonus} "
                        f"projected_score={projected} " + _purchase_chain_display(chain)
                    )
            else:
                lines.append("  Immediately affordable market cards: none")
            reserved = p.get("reservedCards", []) or p.get("storedCards", [])
            known_buyable_reserved = [c for c in reserved if _purchase_chain_fact(
                c, "reserved", discount, gems,
            ) is not None]
            if known_buyable_reserved:
                lines.append("  Immediately affordable known reserved cards:")
                for c in known_buyable_reserved:
                    noble_bonus = _noble_bonus_after_card(state, discount, c)
                    projected = score + int(c.get("points", 0) or 0) + noble_bonus
                    chain = _purchase_chain_fact(c, "reserved", discount, gems, state)
                    lines.append(
                        f"    id={c.get('id','?')} pts={c.get('points',0)} "
                        f"noble={noble_bonus} projected_score={projected} " + _purchase_chain_display(chain)
                    )
        return "\n".join(lines)

    return Tool(
        name="BgOpponentAnalysis",
        description="Show opponents' public position and immediately affordable cards.",
        prompt=(
            "Show opponents' public scores, resources, discounts, reserves, and market "
            "cards they can immediately afford. It does not infer intent or choose a response."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        call=call, is_read_only=True, always_load=True,
    )


def create_noble_tool(ctx: dict) -> Tool:
    def call(args: dict) -> str:
        state = ctx.get("_state", {})
        seat = ctx.get("_seat", 0)
        discount = _calc_discount(state, _player(state, seat))
        nobles = _nobles(state)
        lines = ["=== Noble Analysis ===", "", f"Your discounts: {discount}", ""]
        for n in nobles:
            requirement = _parse_cost(n.get("cost", n))
            need_str = ", ".join(f"{col}:{requirement[col]}" for col in COLORS if requirement[col] > 0)
            progress = ", ".join(f"{col}:{discount.get(col,0)}/{requirement[col]}" for col in COLORS if requirement[col] > 0)
            missing = {col: max(0, requirement[col] - discount.get(col, 0)) for col in COLORS if requirement[col] > 0}
            lines.append(f"Noble #{n.get('id','?')}: need {need_str} -> progress {progress}; missing={missing}; distance={sum(missing.values())}")
        if not nobles: lines.append("No nobles available.")
        return "\n".join(lines)

    return Tool(
        name="BgNobleAnalysis",
        description="Analyze progress toward bonus objectives.",
        prompt=(
            "Show exact permanent-bonus progress and remaining card distance for each noble. "
            "Each noble is worth 3 points; the tool does not decide whether to pursue one."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        call=call, is_read_only=True, always_load=True,
    )


# ═══════════════════════════════════════════════
# Real analysis tools (continued)
# ═══════════════════════════════════════════════

def create_color_focus_tool(ctx: dict) -> Tool:
    def call(args: dict) -> str:
        state = ctx.get("_state", {})
        seat = ctx.get("_seat", 0)
        discount = _calc_discount(state, _player(state, seat))
        market = _market(state)
        # Keep this summary strictly descriptive; strategy belongs to the model.
        demand = {c: 0 for c in COLORS}
        supply_gems = {k: _gs(state).get(k, 0) for k in COLORS} if any(k in _gs(state) for k in COLORS) else _gs(state).get("gems", {})
        for c in market:
            cost = _parse_cost(c.get("cost", ""))
            for col in COLORS:
                demand[col] += cost.get(col, 0)
        lines = ["=== Color Focus ===", "", f"Supply gems: {supply_gems}", f"Your discounts: {discount}", ""]
        for col in COLORS:
            have = discount.get(col, 0)
            supply = supply_gems.get(col, 0)
            lines.append(
                f"  {col}: supply={supply}, visible_total_cost={demand.get(col, 0)}, "
                f"your_discount={have}"
            )
        return "\n".join(lines)

    return Tool(
        name="BgColorFocus",
        description="Show visible resource demand, supply, and current discounts.",
        prompt=(
            "Show each color's visible market demand, current bank supply, your discount, "
            "and supply feasibility. Scarcity is not treated as value and no color is recommended."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        call=call, is_read_only=True, always_load=True,
    )


def create_card_path_tool(ctx: dict) -> Tool:
    def call(args: dict) -> str:
        state = ctx.get("_state", {})
        seat = ctx.get("_seat", 0)
        p = _player(state, seat)
        discount = _calc_discount(state, p)
        gems = p.get("gems", {k: p.get(k, 0) for k in COLORS + ["G"]})
        market = _market(state)
        requested = args.get("card_id")
        if requested is not None:
            candidates = [c for c in market if str(c.get("id")) == str(requested)]
            heading = f"Target card id={requested}"
        else:
            candidates = [c for c in market if (c.get("points", 0) or 0) > 0]
            heading = "Visible scoring cards (unranked)"
        lines = ["=== Card Path Facts ===", "", f"Gems: {gems}", f"Discounts: {discount}", heading, ""]
        if not candidates:
            lines.append("No matching visible card. Revalidate the target against the current market.")
            return "\n".join(lines)
        supply = _supply(state)
        for c in candidates:
            fact = _card_payment_fact(c, discount, gems)
            missing = fact["missing_by_color"]
            available_missing = sum(min(missing[col], supply.get(col, 0)) for col in COLORS)
            total_missing = sum(missing.values())
            uncovered_after_gold = max(0, fact["colored_shortfall"] - fact["gold_available"])
            bank_can_cover_now = available_missing == total_missing
            gem_turn_lower_bound: int | str = (
                (uncovered_after_gold + 2) // 3
                if bank_can_cover_now
                else "unavailable_from_current_bank"
            )
            lines.append(
                f"  id={c.get('id','?')} pts={c.get('points',0)} lvl={c.get('lvl', c.get('level','?'))} "
                f"effective_cost={fact['effective_cost']} missing_by_color={missing} "
                f"colored_shortfall={fact['colored_shortfall']} affordable={str(fact['affordable']).lower()} "
                f"bank_can_cover_now={str(bank_can_cover_now).lower()} "
                f"gem_turn_lower_bound={gem_turn_lower_bound}"
            )
        return "\n".join(lines)

    return Tool(
        name="BgCardPath",
        description="Show factual resource gaps for a specified or visible scoring card.",
        prompt=(
            "Pass card_id to inspect one visible target. Without a target, show all visible "
            "scoring cards. Reports effective cost, exact colored gaps, and whether the bank "
            "currently contains those missing colors. For comparing several targets, omit card_id "
            "instead of making repeated single-card calls. It never selects a target or sequence."
        ),
        parameters={
            "type": "object",
            "properties": {"card_id": {"description": "Optional visible market card id"}},
            "required": [],
        },
        call=call, is_read_only=True, always_load=True,
    )


def create_deny_tool(ctx: dict) -> Tool:
    def call(args: dict) -> str:
        state = ctx.get("_state", {})
        seat = ctx.get("_seat", 0)
        lines = ["=== Denial Analysis ===", ""]
        opponents = [
            (index, player, _pscore(player))
            for index, player in enumerate(_players(state))
            if _player_id(player, index) != seat
        ]
        if opponents:
            lead_score = max(score for _index, _player, score in opponents)
            leaders = [
                f"P{_player_id(player, index)}"
                for index, player, score in opponents
                if score == lead_score
            ]
            lines.append(
                "Public scoring leader(s): " + ", ".join(leaders)
                + f" at {lead_score}pts"
            )
        me = _player(state, seat)
        reserved = me.get("reservedCards", []) or me.get("storedCards", [])
        lines.append(f"Your reserve slots available: {max(0, 3-len(reserved))}; bank gold: {_supply(state).get('G', 0)}")
        for index, p in enumerate(_players(state)):
            pid = _player_id(p, index)
            if pid == seat: continue
            op_score = _pscore(p)
            op_gems = p.get("gems", {k: p.get(k, 0) for k in COLORS + ["G"]})
            op_discount = _calc_discount(state, p)
            lines.append(f"P{pid}: {op_score}pts, gems={op_gems}, discount={op_discount}")
            known_reserved = p.get("reservedCards", []) or p.get("storedCards", [])
            scoring_candidates = [
                (card, source)
                for source, cards in (
                    ("market", _market(state)),
                    ("reserved", known_reserved),
                )
                for card in cards
                if (card.get("points", 0) or 0) > 0
                and _purchase_chain_fact(
                    card, source, op_discount, op_gems,
                ) is not None
            ]
            if scoring_candidates:
                lines.append("  Immediately affordable scoring cards:")
                for c, source in scoring_candidates:
                    noble_bonus = _noble_bonus_after_card(state, op_discount, c)
                    projected = op_score + int(c.get("points", 0) or 0) + noble_bonus
                    lines.append(
                        f"    source={source} id={c.get('id','?')} "
                        f"pts={c.get('points',0)} noble={noble_bonus} "
                        f"projected_score={projected}"
                        + (" WIN_TRIGGER" if projected >= 15 else "")
                    )
            lines.append(f"  Distance to 15: {max(0, 15-op_score)}")
        return "\n".join(lines)

    return Tool(
        name="BgDenyAnalysis",
        description="Show visible cards opponents can buy and the cost of reserving a slot.",
        prompt=(
            "Show the public scoring leader, immediately affordable market and known reserved "
            "scoring cards for every opponent, card-plus-noble projected scores, distance to "
            "15, your free reserve slots, and bank gold. These are facts for comparing racing "
            "and denial; the tool never tells you to reserve."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        call=call, is_read_only=True, always_load=True,
    )


def create_endgame_tool(ctx: dict) -> Tool:
    def call(args: dict) -> str:
        state = ctx.get("_state", {})
        seat = ctx.get("_seat", 0)
        my_score = _pscore(_player(state, seat))
        lines = ["=== Endgame Estimator ===", "", f"Your score: {my_score}"]
        best_opp = max(
            (
                _pscore(p)
                for index, p in enumerate(_players(state))
                if _player_id(p, index) != seat
            ),
            default=0,
        )
        lines.append(f"Best opponent: {best_opp}")
        turn = _turn(state)
        lines.append(f"Turn: {turn}; final_round_triggered={my_score >= 15 or best_opp >= 15}")
        lines.append(f"Your distance to 15: {max(0, 15-my_score)}")
        lines.append(f"Best opponent distance to 15: {max(0, 15-best_opp)}")
        p = _player(state, seat)
        discount = _calc_discount(state, p)
        gems = _player_gems(p)
        own_reserved = p.get("reservedCards", []) or p.get("storedCards", [])
        affordable_scoring = [
            (c, source)
            for source, cards in (("market", _market(state)), ("reserved", own_reserved))
            for c in cards
            if (c.get("points", 0) or 0) > 0
            and _purchase_chain_fact(c, source, discount, gems) is not None
        ]
        lines.append("Your immediately affordable scoring cards: " + (
            ", ".join(
                f"source={source} id={c.get('id')}({c.get('points',0)}pts)"
                for c, source in affordable_scoring
            ) or "none"
        ))
        nobles = list(_gs(state).get("nobles", []) or [])
        lines.append("Opponent immediate scoring projections:")
        for index, opponent in enumerate(_players(state)):
            opponent_pid = _player_id(opponent, index)
            if opponent_pid == seat:
                continue
            opponent_score = _pscore(opponent)
            opponent_discount = _calc_discount(state, opponent)
            opponent_gems = _player_gems(opponent)
            projections = []
            opponent_reserved = (
                opponent.get("reservedCards", []) or opponent.get("storedCards", [])
            )
            for source, cards in (
                ("market", _market(state)),
                ("reserved", opponent_reserved),
            ):
              for card in cards:
                card_points = int(card.get("points", 0) or 0)
                if card_points <= 0:
                    continue
                if _purchase_chain_fact(
                    card, source, opponent_discount, opponent_gems,
                ) is None:
                    continue
                post_discount = dict(opponent_discount)
                bonus_color = _card_bonus_color(card)
                if bonus_color in COLORS:
                    post_discount[bonus_color] = post_discount.get(bonus_color, 0) + 1
                noble_bonus = 3 if any(
                    all(
                        post_discount.get(color, 0) >= amount
                        for color, amount in _noble_requirement(noble).items()
                    )
                    for noble in nobles
                ) else 0
                projected = opponent_score + card_points + noble_bonus
                projections.append(
                    f"source={source} id={card.get('id')} card={card_points}pts "
                    f"noble={noble_bonus} projected={projected}"
                    + (" WIN_TRIGGER" if projected >= 15 else "")
                )
            lines.append(
                f"  P{opponent_pid}: "
                + ("; ".join(projections) if projections else "none")
            )
        card_counts = [
            (_player_id(player, index), len(player.get("boughtCards", [])))
            for index, player in enumerate(_players(state))
        ]
        lines.append(
            "Tiebreak fact (only if scores tie): fewer bought development cards wins; "
            + ", ".join(f"P{pid}={count}" for pid, count in card_counts)
        )
        return "\n".join(lines)

    return Tool(
        name="BgEndgameEstimator",
        description="Show victory distances, trigger state, and immediate scoring facts.",
        prompt=(
            "Show each side's distance to 15, whether the final round is triggered, your "
            "immediate scoring cards, and each opponent's immediate card+noble projections. "
            "It does not estimate an arbitrary "
            "game length or prescribe racing versus engine building."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        call=call, is_read_only=True, always_load=True,
    )


def create_race_facts_tool(ctx: dict) -> Tool:
    """Summarize immediate public scoring threats in one bounded tool call."""
    def call(args: dict) -> str:
        del args
        state = ctx.get("_state", {})
        seat = ctx.get("_seat", 0)
        lines = [
            "=== Race Facts ===",
            "A market card is deniable by reserving it before its owner acts. "
            "A reserved card cannot be removed from that player.",
        ]
        market = _market(state)
        for index, player in enumerate(_players(state)):
            pid = _player_id(player, index)
            score = _pscore(player)
            discount = _calc_discount(state, player)
            gems = _player_gems(player)
            reserved = player.get("reservedCards", []) or player.get("storedCards", [])
            lines.append(
                f"P{pid}{' (YOU)' if pid == seat else ''}: score={score} "
                f"distance_to_15={max(0, 15-score)} "
                f"bought={len(player.get('boughtCards', []))} reserved={len(reserved)}"
            )
            immediate = 0
            for source, cards in (("market", market), ("reserved", reserved)):
                for card in cards:
                    points = int(card.get("points", 0) or 0)
                    if points <= 0:
                        continue
                    chain = _purchase_chain_fact(card, source, discount, gems, state)
                    if chain is None:
                        continue
                    noble_bonus = _noble_bonus_after_card(state, discount, card)
                    projected = score + points + noble_bonus
                    lines.append(
                        f"  source={source} id={card.get('id')} card={points}pts "
                        f"noble={noble_bonus} projected={projected} "
                        f"deniable={str(source == 'market').lower()}"
                        + (" WIN_TRIGGER" if projected >= 15 else "")
                        + (
                            " " + _purchase_chain_display(chain)
                            if pid == seat else ""
                        )
                    )
                    immediate += 1
            if immediate == 0:
                lines.append("  immediately_affordable_scoring=none")
        lines.append(
            "Tiebreak: if scores tie after equal turns, fewer bought development cards wins."
        )
        return "\n".join(lines)

    return Tool(
        name="BgRaceFacts",
        description="Compare immediate public scoring and deniable threats in one call.",
        prompt=(
            "Use when comparing score conversion, the race to 15, or a possible block. "
            "Shows each player's immediately affordable market and known reserved scoring "
            "cards, card-plus-noble projection, whether the card can still be denied, and "
            "your exact purchase chains. It reports facts and does not choose an action."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        call=call, is_read_only=True, always_load=True,
    )


# ═══════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════

def _calc_discount(state: dict, p: dict) -> dict[str, int]:
    discount = {}
    for c in p.get("boughtCards", []):
        ct = _card_bonus_color(c)
        if ct == "?":
            carddef = state.get("carddb", {}).get(str(c.get("id")), {})
            ct = _card_bonus_color(carddef)
        if ct != "?":
            discount[ct] = discount.get(ct, 0) + 1
    return discount


def _card_bonus_color(card: Mapping[str, Any]) -> str:
    color = card.get("color", card.get("type", card.get("type2", "?")))
    if isinstance(color, int) and not isinstance(color, bool):
        return COLORS[color] if 0 <= color < len(COLORS) else "?"
    return str(color) if color in COLORS else "?"


def _player_gems(p: dict) -> dict[str, int]:
    nested = p.get("gems")
    if isinstance(nested, Mapping):
        return {color: int(nested.get(color, 0) or 0) for color in COLORS + ["G"]}
    return {color: int(p.get(color, 0) or 0) for color in COLORS + ["G"]}


def _supply(state: dict) -> dict[str, int]:
    gs = _gs(state)
    nested = gs.get("gems")
    if isinstance(nested, Mapping):
        return {color: int(nested.get(color, 0) or 0) for color in COLORS + ["G"]}
    return {color: int(gs.get(color, 0) or 0) for color in COLORS + ["G"]}


def _card_payment_fact(
    card: dict, discount: Mapping[str, int], gems: Mapping[str, int],
) -> dict[str, Any]:
    printed = _parse_cost(card.get("cost", ""))
    effective = {
        color: max(0, printed[color] - int(discount.get(color, 0) or 0))
        for color in COLORS
    }
    missing = {
        color: max(0, effective[color] - int(gems.get(color, 0) or 0))
        for color in COLORS
    }
    colored_shortfall = sum(missing.values())
    gold = int(gems.get("G", 0) or 0)
    return {
        "printed_cost": printed,
        "effective_cost": effective,
        "missing_by_color": missing,
        "colored_shortfall": colored_shortfall,
        "gold_available": gold,
        "affordable": colored_shortfall <= gold,
    }


def _purchase_chain_fact(
    card: dict,
    source: str,
    discount: Mapping[str, int],
    gems: Mapping[str, int],
    state: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return factual payment steps and any required player-owned noble choice."""
    fact = _card_payment_fact(card, discount, gems)
    if not fact["affordable"]:
        return None
    payment: dict[str, int] = {}
    for color in COLORS:
        count = min(
            int(fact["effective_cost"].get(color, 0) or 0),
            int(gems.get(color, 0) or 0),
        )
        if count > 0:
            payment[color] = count
    gold = sum(
        int(fact["effective_cost"].get(color, 0) or 0)
        - payment.get(color, 0)
        for color in COLORS
    )
    if gold > 0:
        payment["G"] = gold
    steps: list[dict[str, Any]] = [
        {"op": "begin", "action": "buy_card"},
        {"op": "select_card", "source": source, "cardId": int(card["id"])},
    ]
    steps.extend(
        {"op": "pay_gem", "color": color, "count": payment[color]}
        for color in COLORS + ["G"]
        if payment.get(color, 0) > 0
    )
    result: dict[str, Any] = {"steps": steps}
    if state is not None:
        eligible = _eligible_nobles_after_card(state, discount, card)
        if len(eligible) > 1:
            result["nobleChoiceRequired"] = [noble["id"] for noble in eligible]
    return result


def _purchase_chain_display(chain: Mapping[str, Any]) -> str:
    payment_chain = {"steps": chain["steps"]}
    choices = chain.get("nobleChoiceRequired")
    if choices:
        return (
            "payment_chain=" + json.dumps(payment_chain, ensure_ascii=False)
            + " noble_choice_required=" + json.dumps(choices, ensure_ascii=False)
        )
    return "exact_chain=" + json.dumps(payment_chain, ensure_ascii=False)


def _noble_bonus_after_card(
    state: dict, discount: Mapping[str, int], card: Mapping[str, Any],
) -> int:
    return 3 if _eligible_nobles_after_card(state, discount, card) else 0


def _eligible_nobles_after_card(
    state: Mapping[str, Any], discount: Mapping[str, int], card: Mapping[str, Any],
) -> list[dict]:
    post_discount = dict(discount)
    bonus_color = _card_bonus_color(card)
    if bonus_color in COLORS:
        post_discount[bonus_color] = post_discount.get(bonus_color, 0) + 1
    return [noble for noble in _nobles(state) if (
        all(
            post_discount.get(color, 0) >= amount
            for color, amount in _noble_requirement(noble).items()
        )
    )]


def _noble_requirement(noble: Mapping[str, Any]) -> dict[str, int]:
    """Normalize both engine ``{cost:{...}}`` and legacy top-level nobles."""
    return _parse_cost(noble.get("cost", noble))


def _pscore(p: dict) -> int:
    return p.get("score", 0) or sum(c.get("points", 0) for c in p.get("boughtCards", [])) + len(p.get("boughtNobles", [])) * 3


def _parse_cost(value: Any) -> dict[str, int]:
    """Normalize engine text and browser mapping card-cost representations."""
    cost = {c: 0 for c in COLORS}
    if value is None or value == "":
        return cost
    if isinstance(value, Mapping):
        for color in COLORS:
            amount = value.get(color, 0)
            if isinstance(amount, bool) or not isinstance(amount, (int, float)):
                continue
            cost[color] = max(0, int(amount))
        return cost
    if isinstance(value, (str, bytes)):
        for match in re.finditer(r"([CESRO])(\d+)", value):
            cost[match.group(1)] = int(match.group(2))
        return cost
    return cost


# ═══════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════

def _create_splendor_tools(seat_index: int) -> tuple[list[Tool], dict[str, Any]]:
    ctx: dict[str, Any] = {
        "_state": {}, "_seat": seat_index,
        "_action_profile": "splendor-v1",
        "_plan": "", "_scratchpad": "", "_plan_phase": "unknown",
        "_strategy_phase": "unknown",
        "_plan_dirty": False, "_act_submitted": False,
        "_committed_transaction": None, "_canonical_action": None,
        "_rejected_attempts": [], "_transaction_validator": None,
    }
    tools = [
        create_observe_tool(ctx),
        create_act_tool(ctx),
        create_affordability_tool(ctx),
        create_opponent_tool(ctx),
        create_noble_tool(ctx),
        create_card_path_tool(ctx),
        create_deny_tool(ctx),
        create_endgame_tool(ctx),
        create_race_facts_tool(ctx),
        create_plan_tool(ctx),
        create_chat_tool(ctx),
    ]
    return tools, ctx


def _create_generic_tools(seat_index: int) -> tuple[list[Tool], dict[str, Any]]:
    ctx: dict[str, Any] = {
        "_state": {}, "_seat": seat_index,
        "_action_profile": "generic-v1",
        "_plan": "", "_scratchpad": "", "_plan_phase": "unknown",
        "_strategy_phase": "unknown",
        "_plan_dirty": False, "_act_submitted": False,
        "_committed_transaction": None, "_canonical_action": None,
        "_rejected_attempts": [], "_transaction_validator": None,
    }
    return [
        create_observe_tool(ctx),
        create_act_tool(ctx),
        create_plan_tool(ctx),
        create_chat_tool(ctx),
    ], ctx


def create_azul_position_facts_tool(ctx: dict) -> Tool:
    """Render compact public drafting facts without reproducing Azul legality."""
    def call(args: dict) -> str:
        del args
        state = ctx.get("_state", {})
        adapter_view = state.get("adapterView", {})
        game = adapter_view.get("publicState") or state.get("game", state)

        def counts(tiles: list[Any]) -> str:
            totals: dict[str, int] = {}
            for tile in tiles:
                color = str(tile)
                totals[color] = totals.get(color, 0) + 1
            return ", ".join(
                f"{color}={totals[color]}" for color in sorted(totals)
            ) or "empty"

        lines = [
            "=== Azul Position Facts ===",
            f"round={game.get('round', '?')} turn={game.get('turn', '?')}",
            "Sources:",
        ]
        for index, factory in enumerate(game.get("factories", [])):
            if factory:
                lines.append(f"  factory[{index}]: {counts(factory)}")
        lines.append(f"  center: {counts(game.get('center', []))}")
        lines.append("Players:")
        seat = int(ctx.get("_seat", 0) or 0)
        for index, player in enumerate(game.get("players", [])):
            pid = int(player.get("id", index) or 0)
            wall = player.get("wall", [])
            wall_tiles = sum(
                cell is not None for row in wall if isinstance(row, list) for cell in row
            )
            lines.append(
                f"  P{pid}{' (YOU)' if pid == seat else ''}: "
                f"score={player.get('score', 0)} wall_tiles={wall_tiles} "
                f"floor_occupied={len(player.get('floor', []))}"
            )
            for row, tiles in enumerate(player.get("patternLines", [])):
                if not tiles:
                    continue
                capacity = row + 1
                lines.append(
                    f"    row={row} color={tiles[0]} filled={len(tiles)} "
                    f"capacity={capacity} remaining={max(0, capacity-len(tiles))}"
                )
        analysis = adapter_view.get("analysis", [])
        if analysis:
            lines.append("Authoritative legal placement facts:")
            grouped: dict[tuple[str, Any, str, int], list[dict]] = {}
            for item in analysis:
                key = (
                    str(item.get("source")), item.get("factoryIndex"),
                    str(item.get("color")), int(item.get("count", 0) or 0),
                )
                grouped.setdefault(key, []).append(item)
            for (source, factory_index, color, count), candidates in grouped.items():
                source_label = (
                    f"factory[{factory_index}]" if source == "factory" else "center"
                )
                placements = []
                for item in candidates:
                    if item.get("destination") != "pattern":
                        continue
                    placements.append(
                        f"row={item.get('row')} fit={item.get('fits')} "
                        f"overflow={item.get('overflow')} "
                        f"completes={str(bool(item.get('completesLine'))).lower()} "
                        f"floor_added={item.get('floorAdded', '?')} "
                        f"floor_penalty_after={item.get('floorPenaltyAfter', '?')}"
                    )
                first = candidates[0]
                pressure = (
                    f" pushes_to_center={first.get('pushedToCenter', '?')} "
                    f"pushed_colors={first.get('pushedColors', {})} "
                    f"sources_after={first.get('remainingSourcesAfter', '?')} "
                    f"ends_round={str(bool(first.get('endsRound'))).lower()}"
                )
                if first.get("endsRound"):
                    pressure += f" round_score_delta={first.get('roundScoreDelta', '?')}"
                lines.append(
                    f"  {source_label} {color}={count} -> "
                    + ("; ".join(placements) if placements else "floor only")
                    + pressure
                )
        return "\n".join(lines)

    return Tool(
        name="BgAzulPositionFacts",
        description="Show compact public Azul source, placement, floor-cost, and late-round pressure facts.",
        prompt=(
            "Show color counts in every non-empty factory and the center, plus each "
            "player's score, wall occupancy, floor occupancy, and started pattern-line "
            "color/capacity/remaining spaces. Legal placements also show overflow, "
            "projected floor penalty, tiles pushed to center, sources left, and exact "
            "round score delta when the draft ends. Use it to inspect drafting pressure or "
            "possible blocking. Legality and scoring remain authoritative in the Adapter."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        call=call, is_read_only=True, always_load=True,
    )


def _create_azul_tools(seat_index: int) -> tuple[list[Tool], dict[str, Any]]:
    tools, ctx = _create_generic_tools(seat_index)
    tools.insert(2, create_azul_position_facts_tool(ctx))
    return tools, ctx


register_action_profile("splendor-v1", _create_splendor_tools)
register_action_profile("generic-v1", _create_generic_tools)
register_action_profile("azul-v1", _create_azul_tools)


def create_all_tools(
    seat_index: int,
    action_profile: str = "splendor-v1",
    *,
    profile: GameFeatureProfile = DEFAULT_GAME_PROFILE,
    enabled_tool_names: frozenset[str] | None = None,
    action_protocol: str = "semantic-v2",
    definition: GameDefinition | None = None,
) -> tuple[list[Tool], dict[str, Any]]:
    """Expose only the selected profile's model-visible game tools.

    BgAct is the only default model-visible game tool.  Observe and strategy
    helpers remain available only to an experiment seat that explicitly names
    them; an empty or omitted allowlist exposes none of those optional tools.
    """
    tools, context = create_profile_tools(action_profile, seat_index)
    if action_protocol != "semantic-v2":
        raise ValueError(
            f"unsupported historical action protocol: {action_protocol}; "
            "current runtime requires semantic-v2"
        )
    if definition is None:
        raise ValueError("semantic-v2 requires a game definition")
    from bglab.games.tools.semantic_act_v2 import create_semantic_operation_tool

    tools = [
        create_semantic_operation_tool(context, definition)
        if tool.name == "BgAct" else tool
        for tool in tools
    ]
    context["_action_protocol"] = action_protocol
    context["_decision_facts_enabled"] = profile.decision_facts
    optional_names = (
        None if enabled_tool_names is None
        else frozenset(str(name).strip() for name in enabled_tool_names)
    )
    allowed = {"BgAct"}
    if optional_names is not None and "BgObserve" in optional_names:
        allowed.add("BgObserve")
    if profile.strategy_tools:
        strategy_names = {
            tool.name for tool in tools
            if tool.name not in {"BgObserve", "BgAct", "BgPlan", "BgChat"}
        }
        allowed.update(
            strategy_names if optional_names is None
            else strategy_names & optional_names
        )
    if profile.plan:
        allowed.add("BgPlan")
    if profile.chat:
        allowed.add("BgChat")
    return [tool for tool in tools if tool.name in allowed], context


create_tools = create_all_tools
