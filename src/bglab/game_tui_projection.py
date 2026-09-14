"""Pure, per-game projection of durable AI turn reports for the TUI.

The game surface is intentionally downstream of persistence.  This module
does not inspect live events, snapshots, actions, tool output, chat, or any
legacy report files, and it never asks a model to manufacture a summary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class GamePresentationMetadata:
    game_id: str
    game_dir: Path
    manifest_path: Path
    report_path: Path
    title: str
    status: str
    http_port: int | None
    url: str | None
    player_types: tuple[str, ...]
    players: tuple[str, ...]


@dataclass(frozen=True)
class GameReportEntry:
    game_id: str
    seat_index: int | None
    pid: int
    turn_id: str | int | None
    report: str
    timestamp: str | None
    content_hash: str | None
    waiting: bool


@dataclass(frozen=True)
class GameReportProjection:
    game_id: str
    ai_seats: tuple[int, ...]
    entries: tuple[GameReportEntry, ...]
    diagnostics: tuple[str, ...]


def _turn_sort_key(value: str | int) -> tuple[int, int | str]:
    """Return a total-order key without comparing unlike JSON primitives."""
    if isinstance(value, bool):
        raise ValueError("bool turn id")
    if isinstance(value, int):
        return (0, value)
    if isinstance(value, str):
        return (1, value)
    raise ValueError("turn id must be int or str")


def _parse_timestamp(value: str) -> datetime:
    """Parse an ISO timestamp and require an explicit timezone offset."""
    if not isinstance(value, str) or not value:
        raise ValueError("_ts must be an ISO string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("_ts must be timezone-aware")
    return parsed


def _normalize_report(value: str) -> str:
    """Canonicalize a persisted report to at most three Unicode lines/512 chars."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("report must be a non-empty string")
    # splitlines() normalizes CRLF/CR and avoids exposing writer-specific line
    # endings.  No ellipsis is fabricated when content is bounded.
    lines = value.strip().splitlines()[:3]
    normalized = "\n".join(lines).strip()
    if not normalized:
        raise ValueError("report must be a non-empty string")
    return normalized[:512]


def _manifest_seats(store: Any, game_id: str, diagnostics: list[str]) -> tuple[int, ...] | None:
    """Validate the exact manifest identity and return authoritative AI seats."""
    store_game_id = getattr(store, "game_id", None)
    if store_game_id != game_id:
        diagnostics.append(
            f"manifest identity mismatch: store game_id {store_game_id!r} != {game_id!r}",
        )
        return None

    try:
        manifest = store.read_manifest()
    except (OSError, ValueError, TypeError) as exc:
        diagnostics.append(f"manifest read failed: {exc}")
        return None
    if not isinstance(manifest, dict):
        diagnostics.append("manifest must be an object")
        return None
    if manifest.get("game_id") != game_id:
        diagnostics.append("manifest game_id mismatch")
        return None

    player_types = manifest.get("player_types")
    players = manifest.get("players")
    if not isinstance(player_types, list) or not player_types:
        diagnostics.append("manifest player_types must be a non-empty list")
        return None
    if any(
        not isinstance(value, str) or value not in {"human", "ai"}
        for value in player_types
    ):
        diagnostics.append("manifest player_types must contain only human/ai")
        return None
    # A real writer may persist an empty human display name; type/length are
    # authoritative, not truthiness of the name.
    if (
        not isinstance(players, list)
        or len(players) != len(player_types)
        or any(not isinstance(value, str) for value in players)
    ):
        diagnostics.append("manifest players must be strings matching player_types")
        return None
    return tuple(index for index, value in enumerate(player_types) if value == "ai")


def _waiting_entry(game_id: str, seat: int) -> GameReportEntry:
    return GameReportEntry(
        game_id,
        seat,
        seat,
        None,
        "等待第一份战报",
        None,
        None,
        True,
    )


def project_game_report_records(
    records: Any,
    *,
    game_id: str,
    ai_seats: tuple[int, ...],
    diagnostics: tuple[str, ...] = (),
) -> GameReportProjection:
    """Purely project already-parsed durable records for one game."""
    diagnostics = list(diagnostics)
    if not isinstance(game_id, str) or not game_id:
        diagnostics.append("game_id must be a non-empty string")
        return GameReportProjection(str(game_id), (), (), tuple(diagnostics))

    if not isinstance(records, list):
        diagnostics.append("turn_reports.jsonl accessor must return a list")
        records = []

    # Keep the parsed timestamp alongside each entry for ordering.  The public
    # dataclass intentionally retains the writer's original ISO spelling.
    deduped: dict[
        tuple[str, str | int, int, str],
        tuple[datetime, tuple[int, int | str], GameReportEntry],
    ] = {}
    for line_number, raw in enumerate(records, start=1):
        if not isinstance(raw, dict):
            diagnostics.append(f"turn_reports row {line_number}: expected object")
            continue
        try:
            turn_id = raw.get("turn_id")
            turn_key = _turn_sort_key(turn_id)
            pid = raw.get("pid")
            if isinstance(pid, bool) or not isinstance(pid, int):
                raise ValueError("pid must be an integer")
            if pid not in ai_seats:
                raise ValueError("pid is not an AI seat")
            report = _normalize_report(raw.get("report"))
            timestamp_value = raw.get("_ts")
            parsed_timestamp = _parse_timestamp(timestamp_value)
            content_hash = hashlib.sha256(report.encode("utf-8")).hexdigest()
        except (TypeError, ValueError, OverflowError) as exc:
            diagnostics.append(f"turn_reports row {line_number}: {exc}")
            continue

        entry = GameReportEntry(
            game_id,
            pid,
            pid,
            turn_id,
            report,
            timestamp_value,
            content_hash,
            False,
        )
        key = (game_id, turn_id, pid, content_hash)
        candidate = (parsed_timestamp, turn_key, entry)
        previous = deduped.get(key)
        # Dedupe after comparing durable timestamps, retaining the earliest
        # occurrence even when equivalent instants use different offsets.
        if previous is None or parsed_timestamp < previous[0]:
            deduped[key] = candidate

    ordered = sorted(
        deduped.values(),
        key=lambda item: (item[0], item[1], item[2].pid, item[2].content_hash or ""),
    )
    entries = [item[2] for item in ordered]
    reported_pids = {entry.pid for entry in entries}
    entries.extend(
        _waiting_entry(game_id, seat)
        for seat in ai_seats
        if seat not in reported_pids
    )
    return GameReportProjection(game_id, ai_seats, tuple(entries), tuple(diagnostics))


def project_game_reports(store: Any, game_id: str) -> GameReportProjection:
    """Project only valid durable reports for ``game_id``.

    Invalid individual records are omitted with diagnostics.  A malformed
    journal is an observable diagnostic and produces authoritative waiting
    rows, never a fallback to another source.
    """
    diagnostics: list[str] = []
    if not isinstance(game_id, str) or not game_id:
        diagnostics.append("game_id must be a non-empty string")
        return GameReportProjection(str(game_id), (), (), tuple(diagnostics))

    ai_seats = _manifest_seats(store, game_id, diagnostics)
    if ai_seats is None:
        return GameReportProjection(game_id, (), (), tuple(diagnostics))

    try:
        records = store.read_turn_reports()
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        diagnostics.append(f"turn_reports.jsonl read failed: {exc}")
        records = []
    return project_game_report_records(
        records,
        game_id=game_id,
        ai_seats=ai_seats,
        diagnostics=tuple(diagnostics),
    )


__all__ = [
    "GamePresentationMetadata",
    "GameReportEntry",
    "GameReportProjection",
    "_parse_timestamp",
    "_turn_sort_key",
    "project_game_report_records",
    "project_game_reports",
]
