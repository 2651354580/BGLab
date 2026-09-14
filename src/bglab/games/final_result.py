"""Validation for package-owned terminal scoring results."""

from __future__ import annotations

import copy
import re
from typing import Any


_STABLE_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ValueError(f"{label} has unknown fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)}")


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    return value


def _seat(value: object, player_count: int, label: str) -> int:
    seat = _integer(value, label)
    if not 0 <= seat < player_count:
        raise ValueError(f"{label} is outside the player range")
    return seat


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or _STABLE_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must be a stable ID")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value


def validate_final_result(value: object, player_count: int) -> dict[str, Any]:
    """Return an isolated terminal result after validating its public shape."""
    _integer(player_count, "player_count")
    if player_count < 1:
        raise ValueError("player_count must be positive")
    if not isinstance(value, dict):
        raise ValueError("final result must be an object")
    _exact_keys(
        value,
        {"schemaVersion", "winner", "winners", "tieBreakers", "players"},
        "final result",
    )
    if value["schemaVersion"] != 1:
        raise ValueError("final result schemaVersion must be 1")

    winners = value["winners"]
    if not isinstance(winners, list) or not winners:
        raise ValueError("final result winners must be a non-empty list")
    winner_seats = [
        _seat(item, player_count, "winner seat")
        for item in winners
    ]
    if len(set(winner_seats)) != len(winner_seats):
        raise ValueError("final result winners must be unique")
    winner = value["winner"]
    if len(winner_seats) == 1:
        if winner != winner_seats[0]:
            raise ValueError("final result winner must match winners")
    elif winner is not None:
        raise ValueError("final result winner must be null for shared winners")

    players = value.get("players")
    if not isinstance(players, list) or len(players) != player_count:
        raise ValueError("final result must cover every player")
    seats: list[int] = []
    for player in players:
        if not isinstance(player, dict):
            raise ValueError("final result player must be an object")
        _exact_keys(player, {"seat", "total", "components"}, "final result player")
        seats.append(_seat(player["seat"], player_count, "player seat"))
        total = _integer(player["total"], "player total")
        components = player["components"]
        if not isinstance(components, list) or not components:
            raise ValueError("player components must be a non-empty list")
        component_ids: list[str] = []
        component_total = 0
        for component in components:
            if not isinstance(component, dict):
                raise ValueError("score component must be an object")
            _exact_keys(
                component,
                {"id", "label", "value", "formula"},
                "score component",
            )
            component_ids.append(_id(component["id"], "score component id"))
            _text(component["label"], "score component label")
            _text(component["formula"], "score component formula")
            component_total += _integer(component["value"], "score component value")
        if len(set(component_ids)) != len(component_ids):
            raise ValueError("score component IDs must be unique per player")
        if component_total != total:
            raise ValueError(
                f"component sum {component_total} does not equal player total {total}"
            )
    if sorted(seats) != list(range(player_count)):
        raise ValueError("final result must cover every player")

    tie_breakers = value["tieBreakers"]
    if not isinstance(tie_breakers, list):
        raise ValueError("final result tieBreakers must be a list")
    tie_ids: list[str] = []
    for tie_breaker in tie_breakers:
        if not isinstance(tie_breaker, dict):
            raise ValueError("tie breaker must be an object")
        _exact_keys(
            tie_breaker,
            {"id", "label", "values", "winner"},
            "tie breaker",
        )
        tie_ids.append(_id(tie_breaker["id"], "tie breaker id"))
        _text(tie_breaker["label"], "tie breaker label")
        values = tie_breaker["values"]
        if not isinstance(values, list) or len(values) != player_count:
            raise ValueError("tie breaker values must cover every player")
        if any(
            isinstance(item, (dict, list, bool))
            or not isinstance(item, (str, int, float, type(None)))
            for item in values
        ):
            raise ValueError("tie breaker values must be JSON scalars")
        tie_winner = tie_breaker["winner"]
        if tie_winner is not None:
            _seat(tie_winner, player_count, "tie breaker winner")
    if len(set(tie_ids)) != len(tie_ids):
        raise ValueError("tie breaker IDs must be unique")
    return copy.deepcopy(value)
