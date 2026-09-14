"""Read-only historical game replay contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any


class ReplayAuditError(RuntimeError):
    """A stable, user-visible replay integrity failure."""

    def __init__(
        self,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.details = copy.deepcopy(details or {})


def canonical_authority_bytes(value: dict[str, Any]) -> bytes:
    """Serialize authority state deterministically for integrity comparison."""
    if not isinstance(value, dict):
        raise TypeError("authority state must be an object")
    authority = {
        key: item
        for key, item in value.items()
        if key != "adapterView"
    }
    return json.dumps(
        authority,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def authority_hash(value: dict[str, Any]) -> str:
    """Return a full SHA-256 digest for canonical authority state."""
    return hashlib.sha256(canonical_authority_bytes(value)).hexdigest()


_GAME_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _game_directory(game_id: str) -> Path:
    if not isinstance(game_id, str) or _GAME_ID.fullmatch(game_id) is None:
        raise ReplayAuditError("INVALID_GAME_ID", "gameId contains invalid characters")
    from bglab.games.persistence import store as store_module

    root = store_module.GAMES_DIR.resolve()
    directory = (root / game_id).resolve()
    if directory.parent != root:
        raise ReplayAuditError("INVALID_GAME_ID", "gameId escapes the game store")
    return directory


def _read_json(path: Path, *, code: str = "REPLAY_STORE_CORRUPT") -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReplayAuditError(code, f"{path.name} is missing") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayAuditError(code, f"{path.name} is unreadable") from exc
    if not isinstance(value, dict):
        raise ReplayAuditError(code, f"{path.name} must contain an object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise ReplayAuditError(
            "REPLAY_STORE_CORRUPT", f"{path.name} is missing",
        ) from exc
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReplayAuditError(
                "REPLAY_STORE_CORRUPT",
                f"{path.name} line {line_number} is malformed",
            ) from exc
        if not isinstance(value, dict):
            raise ReplayAuditError(
                "REPLAY_STORE_CORRUPT",
                f"{path.name} line {line_number} is not an object",
            )
        records.append(value)
    return records


def _events(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ReplayAuditError(
                "EFFECT_MISMATCH", "recorded effects must contain objects",
            )
        cleaned.append({
            key: copy.deepcopy(field)
            for key, field in item.items()
            if key not in {"_ts", "timestamp", "saved_at"}
        })
    return cleaned


def _deduplicate_decisions(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    by_turn: dict[str, dict[str, Any]] = {}
    for record in records:
        turn_id = record.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            raise ReplayAuditError("TURN_ID_MISMATCH", "decision has no turn_id")
        prior = by_turn.get(turn_id)
        if prior is not None:
            if prior != record:
                raise ReplayAuditError(
                    "TURN_ID_MISMATCH",
                    f"turn {turn_id} has conflicting duplicate records",
                )
            continue
        by_turn[turn_id] = record
        result.append(record)
    return result


def materialize_replay(game_id: str, *, verify: bool = True) -> dict[str, Any]:
    """Build a versioned replay archive from an immutable completed game store."""
    del verify  # Replay archives are never materialized without integrity checks.
    directory = _game_directory(game_id)
    if not directory.is_dir():
        raise ReplayAuditError("REPLAY_NOT_FOUND", f"game {game_id} was not found")
    manifest = _read_json(directory / "manifest.json")
    if (
        manifest.get("status") != "finished"
        or not manifest.get("finished_at")
    ):
        raise ReplayAuditError(
            "REPLAY_NOT_FINISHED", f"game {game_id} is not finished",
        )
    engine = manifest.get("engine")
    if not isinstance(engine, str):
        raise ReplayAuditError("REPLAY_STORE_CORRUPT", "manifest has no engine")
    from bglab.games.registry import get_game
    from bglab.games.package_identity import (
        PackageIdentityError,
        package_runtime_fingerprint,
    )

    try:
        definition = get_game(engine)
    except Exception as exc:
        raise ReplayAuditError(
            "REPLAY_STORE_CORRUPT", f"unsupported engine {engine}",
        ) from exc
    recorded_fingerprint = manifest.get("game_package_fingerprint")
    try:
        current_fingerprint = package_runtime_fingerprint(definition)
    except PackageIdentityError as exc:
        raise ReplayAuditError(
            exc.code,
            "game package identity cannot be verified",
            {"path": exc.relative_path},
        ) from None
    if (
        isinstance(recorded_fingerprint, str)
        and recorded_fingerprint != current_fingerprint
    ):
        raise ReplayAuditError(
            "PACKAGE_FINGERPRINT_MISMATCH",
            "current game package differs from the recorded package",
            {"expected": recorded_fingerprint, "actual": current_fingerprint},
        )
    recorded_snapshot_version = manifest.get("snapshot_version")
    if (
        recorded_snapshot_version is not None
        and recorded_snapshot_version != definition.snapshot_version
    ):
        raise ReplayAuditError(
            "SNAPSHOT_VERSION_MISMATCH",
            "recorded snapshot version is unsupported",
            {
                "expected": recorded_snapshot_version,
                "actual": definition.snapshot_version,
            },
        )
    decisions = _deduplicate_decisions(
        _read_jsonl(directory / "turn_decisions.jsonl"),
    )
    if not decisions:
        raise ReplayAuditError("REPLAY_STORE_CORRUPT", "game has no decisions")
    terminal_envelope = _read_json(directory / "snapshot.json")
    terminal_state = terminal_envelope.get("state")
    if not isinstance(terminal_state, dict):
        raise ReplayAuditError(
            "REPLAY_STORE_CORRUPT", "terminal snapshot has no state",
        )

    from bglab.games.adapter_process import AdapterProcess
    from bglab.games.final_result import validate_final_result

    adapter = AdapterProcess(definition, request_timeout_s=30.0)
    frames: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    try:
        for index, decision in enumerate(decisions):
            raw_before = decision.get("state")
            if not isinstance(raw_before, dict):
                raise ReplayAuditError(
                    "REPLAY_STORE_CORRUPT", f"turn {index} has no state",
                )
            try:
                adapter.restore(raw_before)
                before = adapter.snapshot()
            except Exception as exc:
                raise ReplayAuditError(
                    "SNAPSHOT_VERSION_MISMATCH",
                    f"turn {index} state cannot be restored",
                ) from exc
            turn_id = decision["turn_id"]
            if before.get("decisionId") != turn_id:
                raise ReplayAuditError(
                    "TURN_ID_MISMATCH",
                    f"turn {index} decisionId differs from its record",
                    {"expected": turn_id, "actual": before.get("decisionId")},
                )
            if frames and authority_hash(frames[-1]) != authority_hash(before):
                raise ReplayAuditError(
                    "STATE_HASH_MISMATCH",
                    f"turn {index} before-state differs from the prior after-state",
                    {
                        "turnId": turn_id,
                        "expected": authority_hash(frames[-1]),
                        "actual": authority_hash(before),
                    },
                )
            if not frames:
                frames.append(before)
            transaction = decision.get("transaction")
            if not isinstance(transaction, dict):
                raise ReplayAuditError(
                    "REPLAY_STORE_CORRUPT", f"turn {index} has no transaction",
                )
            result = adapter.dispatch(turn_id, transaction)
            if not isinstance(result, dict) or not result.get("ok"):
                raise ReplayAuditError(
                    "ACTION_REPLAY_REJECTED",
                    f"turn {index} action was rejected",
                    {
                        "turnId": turn_id,
                        "code": result.get("code") if isinstance(result, dict) else None,
                    },
                )
            if _events(result.get("events")) != _events(decision.get("effects")):
                raise ReplayAuditError(
                    "EFFECT_MISMATCH",
                    f"turn {index} effects differ",
                    {"turnId": turn_id},
                )
            actual_after = adapter.snapshot()
            raw_after = (
                decisions[index + 1].get("state")
                if index + 1 < len(decisions)
                else terminal_state
            )
            if not isinstance(raw_after, dict):
                raise ReplayAuditError(
                    "REPLAY_STORE_CORRUPT", f"turn {index} has no successor state",
                )
            try:
                adapter.restore(raw_after)
                expected_after = adapter.snapshot()
            except Exception as exc:
                raise ReplayAuditError(
                    "SNAPSHOT_VERSION_MISMATCH",
                    f"turn {index} successor cannot be restored",
                ) from exc
            actual_hash = authority_hash(actual_after)
            expected_hash = authority_hash(expected_after)
            if actual_hash != expected_hash:
                raise ReplayAuditError(
                    "STATE_HASH_MISMATCH",
                    f"turn {index} successor differs from clone replay",
                    {
                        "turnId": turn_id,
                        "expected": expected_hash,
                        "actual": actual_hash,
                    },
                )
            frames.append(expected_after)
            turns.append({
                "schemaVersion": 1,
                "index": index,
                "turnId": turn_id,
                "seat": int(decision.get("pid", 0)),
                "transaction": copy.deepcopy(transaction),
                "canonicalAction": copy.deepcopy(
                    decision.get("canonical_action", result.get("action")),
                ),
                "effects": _events(decision.get("effects")),
                "beforeFrame": index,
                "afterFrame": index + 1,
                "beforeHash": authority_hash(before),
                "afterHash": expected_hash,
            })
        adapter.restore(frames[-1])
        try:
            final_result = validate_final_result(
                adapter.final_result(),
                int(manifest.get("player_count", len(manifest.get("players", [])))),
            )
        except (TypeError, ValueError) as exc:
            raise ReplayAuditError(
                "FINAL_RESULT_INVALID", "package final result is invalid",
            ) from exc
    finally:
        adapter.close()
    final_scores = [
        player["total"]
        for player in sorted(final_result["players"], key=lambda player: player["seat"])
    ]
    if manifest.get("final_scores") != final_scores:
        raise ReplayAuditError(
            "FINAL_SCORE_MISMATCH",
            "package totals differ from the historical manifest",
            {"expected": manifest.get("final_scores"), "actual": final_scores},
        )
    recorded_winner = manifest.get("winner")
    if (
        final_result["winner"] is not None
        and recorded_winner != final_result["winner"]
    ):
        raise ReplayAuditError(
            "FINAL_SCORE_MISMATCH",
            "package winner differs from the historical manifest",
            {"expected": recorded_winner, "actual": final_result["winner"]},
        )

    from bglab.games.persistence.store import GameStore

    store = GameStore(game_id)
    for index, frame in enumerate(frames):
        store.write_replay_frame(index, frame)
    for turn in turns:
        store.append_replay_turn(turn)
    archive_manifest = {
        "schemaVersion": 1,
        "gameId": game_id,
        "engine": engine,
        "turnCount": len(turns),
        "frameCount": len(frames),
        "packageFingerprint": recorded_fingerprint or current_fingerprint,
        "snapshotVersion": definition.snapshot_version,
        "players": copy.deepcopy(manifest.get("players", [])),
        "verified": True,
        "finalResult": final_result,
    }
    store.write_replay_manifest(archive_manifest)
    return copy.deepcopy(archive_manifest)


def load_replay_manifest(game_id: str) -> dict[str, Any]:
    directory = _game_directory(game_id)
    if not directory.is_dir():
        raise ReplayAuditError("REPLAY_NOT_FOUND", f"game {game_id} was not found")
    return _read_json(directory / "replay" / "manifest.json")


def load_replay_turn(game_id: str, index: int) -> dict[str, Any]:
    manifest = load_replay_manifest(game_id)
    if (
        not isinstance(index, int)
        or isinstance(index, bool)
        or not 0 <= index < int(manifest.get("turnCount", 0))
    ):
        raise ReplayAuditError("REPLAY_STORE_CORRUPT", "turn index is outside the archive")
    turns = _read_jsonl(_game_directory(game_id) / "replay" / "turns.jsonl")
    if len(turns) != int(manifest["turnCount"]):
        raise ReplayAuditError("REPLAY_STORE_CORRUPT", "turn count does not match manifest")
    return copy.deepcopy(turns[index])


def load_replay_frame(game_id: str, index: int) -> dict[str, Any]:
    manifest = load_replay_manifest(game_id)
    if (
        not isinstance(index, int)
        or isinstance(index, bool)
        or not 0 <= index < int(manifest.get("frameCount", 0))
    ):
        raise ReplayAuditError("REPLAY_STORE_CORRUPT", "frame index is outside the archive")
    frame = _read_json(
        _game_directory(game_id) / "replay" / "frames" / f"{index:04d}.json",
    )
    if frame.get("index") != index:
        raise ReplayAuditError("REPLAY_STORE_CORRUPT", "frame index does not match file")
    state = frame.get("state")
    if not isinstance(state, dict) or frame.get("authorityHash") != authority_hash(state):
        raise ReplayAuditError("REPLAY_STORE_CORRUPT", "frame hash is invalid")
    return frame


def verify_replay_turn(game_id: str, index: int) -> dict[str, Any]:
    """Clone-replay one archived turn and return it only after exact verification."""
    manifest = load_replay_manifest(game_id)
    turn = load_replay_turn(game_id, index)
    before = load_replay_frame(game_id, int(turn["beforeFrame"]))
    after = load_replay_frame(game_id, int(turn["afterFrame"]))
    if (
        turn.get("beforeHash") != before.get("authorityHash")
        or turn.get("afterHash") != after.get("authorityHash")
    ):
        raise ReplayAuditError(
            "STATE_HASH_MISMATCH",
            f"turn {index} frame references no longer match the archive",
        )
    from bglab.games.adapter_process import AdapterProcess
    from bglab.games.registry import get_game

    adapter = AdapterProcess(get_game(str(manifest["engine"])), request_timeout_s=30.0)
    try:
        adapter.restore(before["state"])
        if adapter.snapshot().get("decisionId") != turn.get("turnId"):
            raise ReplayAuditError(
                "TURN_ID_MISMATCH", f"turn {index} decisionId differs",
            )
        result = adapter.dispatch(
            str(turn["turnId"]),
            copy.deepcopy(turn.get("transaction")),
        )
        if not isinstance(result, dict) or not result.get("ok"):
            raise ReplayAuditError(
                "ACTION_REPLAY_REJECTED",
                f"turn {index} action was rejected",
                {"code": result.get("code") if isinstance(result, dict) else None},
            )
        if _events(result.get("events")) != _events(turn.get("effects")):
            raise ReplayAuditError(
                "EFFECT_MISMATCH", f"turn {index} effects differ",
            )
        actual_hash = authority_hash(adapter.snapshot())
        if actual_hash != after["authorityHash"]:
            raise ReplayAuditError(
                "STATE_HASH_MISMATCH",
                f"turn {index} successor differs",
                {"expected": after["authorityHash"], "actual": actual_hash},
            )
    finally:
        adapter.close()
    return copy.deepcopy(turn)
