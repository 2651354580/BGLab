"""Offline strategy-quality diagnostics for replayable game decisions.

These metrics describe observed patterns. They never rank or select an action.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class StrategyMetricsError(ValueError):
    """Recorded diagnostics are insufficient to report measured values."""


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StrategyMetricsError(f"Invalid JSONL record at line {number}") from exc
        if not isinstance(row, dict):
            raise StrategyMetricsError(f"Expected object at line {number}")
        rows.append(row)
    return rows


def _pid(row: dict, key: str = "pid") -> str:
    value = row.get(key)
    if isinstance(value, bool) or not (
        isinstance(value, int) and value >= 0
        or isinstance(value, str) and value.isascii() and value.isdecimal()
    ):
        raise StrategyMetricsError("Missing or invalid seat")
    return str(int(value))


def _common_metrics(decisions: list[dict]) -> dict[str, dict]:
    per_player: dict[str, dict] = {}
    for decision in decisions:
        metrics = per_player.setdefault(_pid(decision), {
            "decisions": 0, "loaded_skills": [],
            "rejected_attempts": 0, "validation_error_codes": {},
        })
        metrics["decisions"] += 1
        metrics["loaded_skills"] = sorted(
            set(metrics["loaded_skills"]) | set(decision.get("loaded_skills", []))
        )
        rejected = decision.get("rejected_attempts", [])
        if not isinstance(rejected, list) or any(not isinstance(item, dict) for item in rejected):
            raise StrategyMetricsError("Invalid rejected-attempt diagnostics")
        metrics["rejected_attempts"] += len(rejected)
        for attempt in rejected:
            code = str((attempt.get("error") or {}).get("code", "UNKNOWN"))
            errors = metrics["validation_error_codes"]
            errors[code] = errors.get(code, 0) + 1
    return per_player


def _market(state: dict) -> list[dict]:
    market = state.get("market", [])
    if market:
        return [card for card in market if isinstance(card, dict)]
    return [
        card for card in state.get("gamestorage", {}).get("cards", [])
        if isinstance(card, dict)
        and str(card.get("location", "")).startswith("market_")
    ]


def _bought_card(decision: dict) -> dict | None:
    action = decision.get(
        "canonical_action",
        decision.get("action", decision.get("selected_action", {})),
    ) or {}
    if not isinstance(action, dict) or not isinstance(action.get("type"), str):
        raise StrategyMetricsError("Missing or invalid recorded action")
    if action.get("type") not in {"buy_market", "buy_reserved"}:
        return None
    card_id: Any = action.get("card_id", action.get("cardId", action.get("id")))
    if card_id is None:
        card = action.get("card")
        if isinstance(card, dict):
            card_id = card.get("id")
    state = decision.get("state", {}) or {}
    cards = _market(state) if action.get("type") == "buy_market" else []
    players = state.get("playerstorage", [])
    pid = int(_pid(decision))
    if 0 <= pid < len(players):
        player = players[pid]
        reserved = player.get("reservedCards", []) or player.get("storedCards", [])
        if action.get("type") == "buy_reserved":
            cards = reserved
        card_index = action.get("cardIndex", action.get("card_index"))
        if action.get("type") == "buy_reserved" and isinstance(card_index, int):
            if 0 <= card_index < len(reserved):
                card_id = reserved[card_index].get("id")
    for card in cards:
        if str(card.get("id")) == str(card_id):
            database = state.get("carddb")
            if database is not None:
                if not isinstance(database, dict) or not isinstance(database.get(str(card_id)), dict):
                    raise StrategyMetricsError("Purchased card missing from card database")
                return database[str(card_id)]
            return card
    raise StrategyMetricsError("Purchased card not found in recorded state")


def compute_strategy_metrics(decisions: list[dict]) -> dict[str, dict]:
    per_player = _common_metrics(decisions)
    for metrics in per_player.values():
        metrics.update({
            "purchases": 0, "scoring_purchases": 0,
            "zero_point_level_one_purchases": 0, "purchased_points": 0,
            "first_scoring_turn": None, "explicit_discards": 0,
        })
    for decision in decisions:
        metrics = per_player[_pid(decision)]
        transaction = decision.get("transaction", {})
        metrics["explicit_discards"] += sum(
            int(step.get("count", 1) or 0)
            for step in transaction.get("steps", []) if step.get("op") == "discard_gem"
        ) if isinstance(transaction, dict) else 0
        card = _bought_card(decision)
        if card is None:
            continue
        metrics["purchases"] += 1
        points = card.get("points")
        level = card.get("lvl", card.get("level"))
        if (type(points) is not int or points < 0
                or type(level) is not int or level not in {1, 2, 3}):
            raise StrategyMetricsError("Purchased card has unknown points or level")
        metrics["purchased_points"] += points
        if points > 0:
            metrics["scoring_purchases"] += 1
            if metrics["first_scoring_turn"] is None:
                wrapper = (decision.get("state", {}) or {}).get("wrapper", {})
                metrics["first_scoring_turn"] = wrapper.get("turn", decision.get("turn_id"))
        elif level == 1:
            metrics["zero_point_level_one_purchases"] += 1
    for metrics in per_player.values():
        purchases = metrics["purchases"]
        metrics["points_per_purchase"] = round(metrics["purchased_points"] / purchases, 3) if purchases else 0.0
    return per_player


def compute_action_chain_metrics(
    decisions: list[dict], final_state: dict | None = None,
) -> dict[str, dict]:
    """Describe Azul action-chain outcomes without scoring actions."""
    per_player = _common_metrics(decisions)
    for metrics in per_player.values():
        metrics.update({
            "pattern_placements": 0, "floor_placements": 0,
            "factory_drafts": 0, "center_drafts": 0,
        })
    for decision in decisions:
        metrics = per_player[_pid(decision)]
        transaction = decision.get("transaction", {})
        steps = transaction.get("steps", []) if isinstance(transaction, dict) else []
        for step in steps:
            if step.get("op") == "select_source":
                key = "center_drafts" if step.get("source") == "center" else "factory_drafts"
                metrics[key] += 1
            elif step.get("op") == "place_tiles":
                key = "floor_placements" if step.get("destination") == "floor" else "pattern_placements"
                metrics[key] += 1

    state = final_state or {}
    game = state.get("game", {}) if isinstance(state.get("game"), dict) else {}
    for index, player in enumerate(game.get("players", [])):
        pid = str(player.get("id", index))
        if pid not in per_player:
            raise StrategyMetricsError("Final-state seat has no decision diagnostics")
        metrics = per_player[pid]
        score = player.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            metrics["final_score"] = int(score)
        wall = player.get("wall")
        if isinstance(wall, list):
            metrics["wall_tiles"] = sum(
                tile is not None for row in wall if isinstance(row, list) for tile in row
            )
            metrics["completed_wall_rows"] = sum(
                bool(row) and all(tile is not None for tile in row)
                for row in wall if isinstance(row, list)
            )
    return per_player


def compute_store_strategy_metrics(game_dir: Path) -> dict[str, dict]:
    decisions = _read_jsonl(game_dir / "turn_decisions.jsonl")
    manifest = json.loads((game_dir / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("engine"), str):
        raise StrategyMetricsError("Missing game engine")
    count = manifest.get("player_count")
    if type(count) is not int or count < 1:
        raise StrategyMetricsError("Missing player count")
    expected_seats = {str(pid) for pid in range(count)}
    if {_pid(row) for row in decisions} != expected_seats:
        raise StrategyMetricsError("Decision diagnostics do not cover manifest seats")
    replay_path = game_dir / "replay" / "turns.jsonl"
    if replay_path.exists():
        def turn_keys(rows: list[dict], turn_key: str, seat_key: str) -> set[tuple[str, str]]:
            keys = set()
            for row in rows:
                turn = row.get(turn_key)
                if not isinstance(turn, str) or not turn or turn in {key[0] for key in keys}:
                    raise StrategyMetricsError("Missing or duplicate turn diagnostics")
                keys.add((turn, _pid(row, seat_key)))
            return keys

        if turn_keys(decisions, "turn_id", "pid") != turn_keys(_read_jsonl(replay_path), "turnId", "seat"):
            raise StrategyMetricsError("Decision diagnostics do not match recorded replay turns")

    # Store events are the complete attempt log; decision rows only retained a
    # legacy subset. Do not add the two sources and double-count rejections.
    events = _read_jsonl(game_dir / "events.jsonl")
    decisions = [{**row, "rejected_attempts": []} for row in decisions]
    engine = manifest["engine"]
    if engine == "splendor":
        per_player = compute_strategy_metrics(decisions)
    elif engine == "azul":
        snapshot = json.loads((game_dir / "snapshot.json").read_text(encoding="utf-8"))
        state = snapshot.get("state", snapshot)
        per_player = compute_action_chain_metrics(decisions, state)
    else:
        per_player = _common_metrics(decisions)
    for event in events:
        if (event.get("type") != "tool_result" or event.get("tool") != "BgAct"
                or event.get("outcome_kind") != "rejected"):
            continue
        pid = _pid(event)
        if pid not in per_player:
            raise StrategyMetricsError("Rejection seat has no decision diagnostics")
        code = event.get("public_code")
        if not isinstance(code, str) or not code:
            match = re.match(r"^([A-Z][A-Z0-9_]*):", str(event.get("result", "")))
            code = match.group(1) if match else "UNKNOWN"
        metrics = per_player[pid]
        metrics["rejected_attempts"] += 1
        errors = metrics["validation_error_codes"]
        errors[code] = errors.get(code, 0) + 1
    return per_player
