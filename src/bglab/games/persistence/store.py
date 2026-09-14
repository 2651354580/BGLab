"""GameStore — 游戏存档和战报。

每个对局一个目录:
  ~/.bglab/games/{game_id}/
    ├── manifest.json    — 元数据
    ├── events.jsonl     — 事件日志
    ├── snapshot.json    — 最近一次前端确认快照
    ├── turn_reports.jsonl
    └── agents/p{pid}.json
"""

from __future__ import annotations

import copy, json, os, tempfile, threading, time
from datetime import datetime, timezone
from pathlib import Path

from bglab.owned_path import (
    OwnedPathError,
    exact_owned_child,
    validate_exact_owned_component,
)

GAMES_DIR = Path(os.environ.get("BGLAB_GAMES_DIR") or Path.home() / ".bglab" / "games").expanduser()


def _validate_game_id(game_id: str) -> str:
    """Validate one unchanged game directory component."""
    if not isinstance(game_id, str):
        raise TypeError("game_id must be a string")
    try:
        return validate_exact_owned_component(game_id)
    except OwnedPathError as exc:
        raise ValueError(f"invalid game_id: {exc}") from exc


def _owned_game_directory(game_id: str) -> Path:
    """Create and return one exact game directory below the owned root."""
    root = Path(GAMES_DIR).expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise ValueError("game store root is not a directory") from exc
    target = exact_owned_child(root, game_id)
    if not root.is_dir():
        raise ValueError("game store root is not a directory")
    try:
        target.mkdir(exist_ok=True)
    except FileExistsError as exc:
        raise ValueError("game target is not a directory") from exc
    target = exact_owned_child(root, game_id)
    if not target.is_dir():
        raise ValueError("game target is not a directory")
    return target



def _replace_recovery_file(temporary: str, destination: Path) -> None:
    """Retry a brief Windows sharing denial without abandoning atomic replace.

    Keep the same fully flushed temporary file and never unlink the destination.
    Permanent permission failures still surface after one second of backoff.
    """
    delays = (0.01, 0.02, 0.04, 0.08, 0.16, 0.25, 0.25, 0.19)
    for attempt in range(len(delays) + 1):
        try:
            os.replace(temporary, destination)
            return
        except OSError as error:
            if getattr(error, "winerror", None) not in {5, 32, 33} or attempt == len(delays):
                raise
            time.sleep(delays[attempt])


class GameStore:
    def __init__(self, game_id: str):
        self.game_id = _validate_game_id(game_id)
        self.dir = _owned_game_directory(self.game_id)
        self._lock = threading.RLock()

    @classmethod
    def from_existing_dir(
        cls,
        game_id: str,
        game_dir: Path,
    ) -> "GameStore":
        """Bind a store to an already-existing game directory.

        Presentation readers use this constructor after they have resolved and
        validated an exact per-game path.  Unlike :meth:`__init__`, it never
        consults ``GAMES_DIR`` and never creates directories or files.
        """
        if not isinstance(game_dir, Path):
            raise TypeError("game_dir must be a pathlib.Path")
        _validate_game_id(game_id)
        provided = game_dir.expanduser().absolute()
        try:
            directory = exact_owned_child(provided.parent, game_id)
        except OwnedPathError as exc:
            raise ValueError(f"invalid game directory: {exc}") from exc
        if provided != directory:
            raise ValueError("game directory name does not match game_id")
        if not directory.is_dir():
            raise FileNotFoundError(f"game directory is missing: {directory}")

        store = cls.__new__(cls)
        store.game_id = game_id
        store.dir = directory
        # Accessors use the same lock as normal stores, while construction is
        # deliberately free of mkdir/write side effects.
        store._lock = threading.RLock()
        return store

    def _atomic_json(self, path: Path, data: dict) -> None:
        """Write JSON without ever exposing a partially-written recovery file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            _replace_recovery_file(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _atomic_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            _replace_recovery_file(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def read_manifest(self) -> dict:
        path = self.dir / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def update_manifest(self, **changes) -> dict:
        with self._lock:
            data = self.read_manifest()
            data.update(changes)
            self._atomic_json(self.dir / "manifest.json", data)
            return data

    def write_manifest(self, engine: str = "splendor", pc: int = 2,
                       names: list[str] | None = None,
                       skillset_version: str | None = None):
        manifest = {
            "game_id": self.game_id,
            "engine": engine,
            "player_count": pc,
            "players": names or [f"P{i}" for i in range(pc)],
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "winner": None,
            "final_scores": None,
            "skillset_version": skillset_version or "unknown",
            "status": "starting",
            "current_turn": 0,
            "current_player": 0,
            "last_confirmed_turn_id": None,
            "player_types": ["ai"] * pc,
        }
        self._atomic_json(self.dir / "manifest.json", manifest)
        return manifest

    def write_snapshot(self, snapshot: dict, turn_id: str | int | None = None,
                       metadata: dict | None = None) -> None:
        with self._lock:
            envelope = {
                "turn_id": turn_id,
                "saved_at": datetime.now(timezone.utc).isoformat(),
                "state": snapshot,
            }
            if metadata:
                envelope["metadata"] = copy.deepcopy(metadata)
            self._atomic_json(self.dir / "snapshot.json", envelope)
            wrapper = snapshot.get("wrapper", snapshot.get("state", snapshot.get("st", {}))) if isinstance(snapshot, dict) else {}
            snapshot_status = "finished" if wrapper.get("phase") in {"finished", "gameover"} else "active"
            self.update_manifest(
                current_turn=wrapper.get("turn", 0),
                current_player=wrapper.get("currentPlayer", 0),
                last_confirmed_turn_id=turn_id,
                status=snapshot_status,
            )

    def read_snapshot(self) -> dict | None:
        path = self.dir / "snapshot.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def read_turn_reports_from(
        self,
        byte_offset: int = 0,
        *,
        line_number_start: int = 0,
    ) -> tuple[list[dict], int, int]:
        """Read complete JSONL rows beginning at one byte offset.

        An unterminated final row is treated as a writer's partial tail: it is
        omitted and the returned cursor remains immediately before it so the
        next poll can retry the same bytes.  Complete malformed rows retain
        the durable file/line diagnostic contract of ``read_turn_reports``.
        """
        if (
            isinstance(byte_offset, bool)
            or not isinstance(byte_offset, int)
            or byte_offset < 0
        ):
            raise ValueError("byte_offset must be a non-negative integer")
        if (
            isinstance(line_number_start, bool)
            or not isinstance(line_number_start, int)
            or line_number_start < 0
        ):
            raise ValueError("line_number_start must be a non-negative integer")

        path = (self.dir / "turn_reports.jsonl").resolve()
        if not path.is_file():
            return [], 0, 0
        with path.open("rb") as file:
            file.seek(0, os.SEEK_END)
            file_size = file.tell()
            if byte_offset > file_size:
                raise ValueError(
                    f"{path}: byte offset {byte_offset} exceeds file size {file_size}",
                )
            file.seek(byte_offset)
            payload = file.read()

        complete_end = payload.rfind(b"\n") + 1
        if complete_end == 0:
            return [], byte_offset, 0

        records: list[dict] = []
        complete_payload = payload[:complete_end]
        for line_number, raw_line in enumerate(
            complete_payload.splitlines(), start=line_number_start + 1,
        ):
            try:
                value = json.loads(raw_line.rstrip(b"\r\n"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    f"{path} line {line_number}: malformed JSON",
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(
                    f"{path} line {line_number}: expected object",
                )
            records.append(value)
        return records, byte_offset + complete_end, len(records)

    def read_turn_reports(self) -> list[dict]:
        """Read the authoritative per-game turn-report journal.

        This accessor deliberately has no recovery or fallback behavior.  The
        TUI projection must only consume records persisted for this game in
        ``turn_reports.jsonl``; malformed input is surfaced with its exact
        file and one-based line so callers can present a diagnostic state.
        """
        records, _cursor, _line_count = self.read_turn_reports_from(0)
        return records

    def write_replay_frame(self, index: int, state: dict) -> str:
        """Atomically persist one immutable authority frame."""
        from bglab.games.replay import ReplayAuditError, authority_hash

        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ReplayAuditError("REPLAY_STORE_CORRUPT", "frame index must be non-negative")
        if not isinstance(state, dict):
            raise ReplayAuditError("REPLAY_STORE_CORRUPT", "replay frame state must be an object")
        digest = authority_hash(state)
        envelope = {
            "schemaVersion": 1,
            "index": index,
            "authorityHash": digest,
            "state": copy.deepcopy(state),
        }
        path = self.dir / "replay" / "frames" / f"{index:04d}.json"
        with self._lock:
            if path.exists():
                try:
                    current = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ReplayAuditError(
                        "REPLAY_STORE_CORRUPT", f"frame {index} is unreadable",
                    ) from exc
                if current != envelope:
                    current_state = current.get("state")
                    current_digest = (
                        authority_hash(current_state)
                        if isinstance(current_state, dict)
                        else None
                    )
                    if (
                        current.get("schemaVersion") != 1
                        or current.get("index") != index
                        or current.get("authorityHash") != current_digest
                        or current_digest != digest
                    ):
                        raise ReplayAuditError(
                            "REPLAY_STORE_CORRUPT",
                            f"frame {index} conflicts with its immutable record",
                        )
                return digest
            self._atomic_json(path, envelope)
        return digest

    def append_replay_turn(self, turn: dict) -> None:
        """Append one immutable turn after verifying both adjacent frames."""
        from bglab.games.replay import ReplayAuditError

        if not isinstance(turn, dict):
            raise ReplayAuditError("REPLAY_STORE_CORRUPT", "replay turn must be an object")
        index = turn.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise ReplayAuditError("REPLAY_STORE_CORRUPT", "turn index must be non-negative")
        if turn.get("beforeFrame") != index or turn.get("afterFrame") != index + 1:
            raise ReplayAuditError(
                "REPLAY_STORE_CORRUPT", "turn frame references are not contiguous",
            )
        replay_dir = self.dir / "replay"
        turns_path = replay_dir / "turns.jsonl"
        with self._lock:
            turns: list[dict] = []
            if turns_path.exists():
                for line_number, line in enumerate(
                    turns_path.read_text(encoding="utf-8").splitlines(), start=1,
                ):
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ReplayAuditError(
                            "REPLAY_STORE_CORRUPT",
                            f"turns.jsonl line {line_number} is malformed",
                        ) from exc
                    if not isinstance(item, dict):
                        raise ReplayAuditError(
                            "REPLAY_STORE_CORRUPT",
                            f"turns.jsonl line {line_number} is not an object",
                        )
                    turns.append(item)
            existing = index < len(turns)
            if existing and turns[index] != turn:
                raise ReplayAuditError(
                    "REPLAY_STORE_CORRUPT",
                    f"turn {index} conflicts with its immutable record",
                )
            if not existing and index != len(turns):
                raise ReplayAuditError(
                    "REPLAY_STORE_CORRUPT",
                    f"turn {index} is not the next contiguous turn",
                )
            frames = []
            for frame_index, hash_key in (
                (index, "beforeHash"),
                (index + 1, "afterHash"),
            ):
                path = replay_dir / "frames" / f"{frame_index:04d}.json"
                if not path.is_file():
                    raise ReplayAuditError(
                        "REPLAY_STORE_CORRUPT", f"frame {frame_index} is missing",
                    )
                try:
                    frame = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ReplayAuditError(
                        "REPLAY_STORE_CORRUPT", f"frame {frame_index} is unreadable",
                    ) from exc
                if frame.get("authorityHash") != turn.get(hash_key):
                    raise ReplayAuditError(
                        "REPLAY_STORE_CORRUPT",
                        f"turn {index} does not match frame {frame_index}",
                    )
                frames.append(frame)
            if not existing:
                turns.append(copy.deepcopy(turn))
                self._atomic_text(
                    turns_path,
                    "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in turns),
                )
            self.write_replay_manifest({
                "schemaVersion": 1,
                "gameId": self.game_id,
                "turnCount": len(turns),
                "frameCount": len(turns) + 1,
            })

    def replay_turn_count(self) -> int:
        path = self.dir / "replay" / "turns.jsonl"
        if not path.is_file():
            return 0
        return len(path.read_text(encoding="utf-8").splitlines())

    def replay_frame_count(self) -> int:
        directory = self.dir / "replay" / "frames"
        return len(list(directory.glob("[0-9][0-9][0-9][0-9].json"))) if directory.is_dir() else 0

    def write_pending_replay_turn(self, turn: dict) -> None:
        if not isinstance(turn, dict):
            raise TypeError("pending replay turn must be an object")
        from bglab.games.replay import ReplayAuditError

        with self._lock:
            path = self.dir / "replay" / "pending.json"
            if path.exists():
                try:
                    current = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ReplayAuditError(
                        "REPLAY_STORE_CORRUPT", "pending replay turn is unreadable",
                    ) from exc
                if current != turn:
                    raise ReplayAuditError(
                        "REPLAY_STORE_CORRUPT",
                        "pending replay turn conflicts with its immutable record",
                    )
                return
            self._atomic_json(path, copy.deepcopy(turn))

    def read_pending_replay_turn(self) -> dict | None:
        path = self.dir / "replay" / "pending.json"
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            from bglab.games.replay import ReplayAuditError
            raise ReplayAuditError(
                "REPLAY_STORE_CORRUPT", "pending replay turn is not an object",
            )
        return value

    def clear_pending_replay_turn(self) -> None:
        path = self.dir / "replay" / "pending.json"
        with self._lock:
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def write_replay_manifest(self, manifest: dict) -> None:
        if not isinstance(manifest, dict):
            raise TypeError("replay manifest must be an object")
        with self._lock:
            self._atomic_json(
                self.dir / "replay" / "manifest.json",
                copy.deepcopy(manifest),
            )

    def write_agent_state(self, pid: int, state: dict) -> None:
        with self._lock:
            self._atomic_json(self.dir / "agents" / f"p{pid}.json", state)

    def read_agent_state(self, pid: int) -> dict | None:
        path = self.dir / "agents" / f"p{pid}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def log_turn_report(self, report: dict) -> None:
        report = dict(report)
        report["_ts"] = datetime.now(timezone.utc).isoformat()
        with self._lock, open(self.dir / "turn_reports.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(report, ensure_ascii=False) + "\n")

    def log_event(self, event: dict):
        event = dict(event)
        event["_ts"] = datetime.now(timezone.utc).isoformat()
        with self._lock, open(self.dir / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read_events(self) -> list[dict]:
        path = self.dir / "events.jsonl"
        if not path.is_file():
            return []
        events: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def write_skill_review(self, text: str) -> None:
        (self.dir / "skill_review.md").write_text(text, encoding="utf-8")

    @classmethod
    def unfinished(cls) -> list["GameStore"]:
        """Return all resumable stores, newest first.

        A manifest without a confirmed snapshot cannot be resumed safely.  It
        is therefore deliberately omitted from this selector, just as it was
        by the former ``latest_unfinished`` helper.  Reading the selector must
        not create or mutate any store, so malformed or unsafe entries are
        skipped rather than bound.
        """
        if not GAMES_DIR.exists():
            return []
        candidates: list[tuple[str, Path]] = []
        for manifest in GAMES_DIR.glob("*/manifest.json"):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            if (
                data.get("status") != "finished"
                and not data.get("finished_at")
                and (manifest.parent / "snapshot.json").is_file()
            ):
                started_at = data.get("started_at", "")
                candidates.append(
                    (started_at if isinstance(started_at, str) else "", manifest.parent),
                )
        candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        stores: list[GameStore] = []
        for _started_at, directory in candidates:
            try:
                stores.append(cls.from_existing_dir(directory.name, directory))
            except (TypeError, ValueError, OSError):
                continue
        return stores

    @classmethod
    def latest_unfinished(cls) -> "GameStore | None":
        """Compatibility helper returning the newest unfinished store."""
        stores = cls.unfinished()
        return stores[0] if stores else None

    @classmethod
    def pending_skill_reviews(cls, limit: int = 3) -> list["GameStore"]:
        """Completed games whose background strategy review was interrupted."""
        if not GAMES_DIR.exists():
            return []
        pending: list[tuple[str, GameStore]] = []
        for manifest_path in GAMES_DIR.glob("*/manifest.json"):
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if (
                data.get("status") == "finished"
                and data.get("skill_review_status") == "pending"
            ):
                pending.append((data.get("finished_at", ""), cls(manifest_path.parent.name)))
        pending.sort(key=lambda item: item[0])
        return [store for _date, store in pending[:max(0, limit)]]

    def log_turn_decision(self, decision: dict):
        """Persist a replayable turn sample for later experience extraction.

        Unlike the lightweight event stream, this contains the observed position,
        legal action set, activated guides and selected action at decision time.
        """
        decision["_ts"] = datetime.now(timezone.utc).isoformat()
        with open(self.dir / "turn_decisions.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(decision, ensure_ascii=False) + "\n")

    def write_report(self, **kwargs):
        """Write the battle report.

        Expected kwargs: winner, scores, turns, ai_stats, strategies, state_summary
        """
        w = kwargs.get("winner", "?")
        scores = kwargs.get("scores", [])
        turns = kwargs.get("turns", 0)
        ai_stats = kwargs.get("ai_stats", {})
        strategies = kwargs.get("strategies", [])
        state_summary = kwargs.get("state_summary", {})

        score_lines = "\n".join(
            f"| P{i} | {s.get('score', 0)} | {s.get('cards', 0)} | {s.get('nobles', 0)} |"
            for i, s in enumerate(scores)
        )

        strategy_text = "\n".join(f"- {s}" for s in strategies) if strategies else "(no strategic summary)"

        report = f"""# Splendor Battle Report

**Game ID**: {self.game_id}
**Date**: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
**Winner**: P{w}
**Total Turns**: {turns}

## Final Scores

| Player | Score | Cards | Nobles |
|--------|-------|-------|--------|
{score_lines}

## AI Performance

| Player | API Calls | Valid Actions | Avg Time |
|--------|-----------|---------------|----------|
"""

        for i in range(len(scores)):
            s = ai_stats.get(f"p{i}", {})
            ok = s.get("ok", 0)
            calls = s.get("calls", ok)
            total_ms = s.get("total_ms", 0)
            avg_ms = s.get("avg_ms", total_ms // max(ok, 1))
            report += f"| P{i} | {calls} | {ok} | {avg_ms}ms |\n"

        report += f"""
## Strategy Insights

{strategy_text}

## Final Board State

```
Supply gems: {state_summary.get('supply', {})}
```

---
Generated by bglab persistent game runtime
"""

        (self.dir / "report.md").write_text(report, encoding="utf-8")

        self.update_manifest(
            status="finished",
            finished_at=datetime.now(timezone.utc).isoformat(),
            winner=w,
            final_scores=[s.get("score", 0) for s in scores],
        )
