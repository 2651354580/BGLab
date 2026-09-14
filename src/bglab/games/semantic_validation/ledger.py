from __future__ import annotations

import copy
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


_GENESIS_HASH = "0" * 64


class LedgerIntegrityError(RuntimeError):
    """Raised when an adaptive-loop ledger is malformed or was edited."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _entry_hash(entry_without_hash: Mapping[str, Any]) -> str:
    encoded = _canonical_json(entry_without_hash).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AppendOnlyLedger:
    """Small UTF-8 JSONL ledger with a deterministic SHA-256 hash chain."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def verify(self) -> tuple[dict[str, Any], ...]:
        if not self.path.exists():
            return ()
        rows: list[dict[str, Any]] = []
        previous_hash = _GENESIS_HASH
        for index, line in enumerate(
            self.path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if not line.strip():
                raise LedgerIntegrityError(f"blank ledger row at sequence {index}")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerIntegrityError(
                    f"invalid ledger JSON at sequence {index}: {exc}",
                ) from exc
            if not isinstance(row, dict):
                raise LedgerIntegrityError(
                    f"ledger row {index} must be an object",
                )
            if row.get("sequence") != index:
                raise LedgerIntegrityError(
                    f"ledger sequence mismatch at {index}",
                )
            if row.get("previousHash") != previous_hash:
                raise LedgerIntegrityError(
                    f"ledger previous hash mismatch at sequence {index}",
                )
            supplied_hash = row.get("entryHash")
            unsigned = {key: value for key, value in row.items() if key != "entryHash"}
            expected_hash = _entry_hash(unsigned)
            if supplied_hash != expected_hash:
                raise LedgerIntegrityError(
                    f"ledger entry hash mismatch at sequence {index}",
                )
            previous_hash = expected_hash
            rows.append(copy.deepcopy(row))
        return tuple(rows)

    @property
    def head_hash(self) -> str:
        rows = self.verify()
        return str(rows[-1]["entryHash"]) if rows else _GENESIS_HASH

    def append(
        self,
        event: str,
        payload: Mapping[str, Any],
        *,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(event, str) or not event.strip():
            raise ValueError("ledger event must be non-empty text")
        if not isinstance(payload, Mapping):
            raise ValueError("ledger payload must be an object")
        rows = self.verify()
        unsigned = {
            "sequence": len(rows) + 1,
            "previousHash": (
                str(rows[-1]["entryHash"]) if rows else _GENESIS_HASH
            ),
            "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
            "event": event.strip(),
            "payload": copy.deepcopy(dict(payload)),
        }
        row = {**unsigned, "entryHash": _entry_hash(unsigned)}
        encoded = (_canonical_json(row) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            self.path,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            offset = 0
            while offset < len(encoded):
                offset += os.write(descriptor, encoded[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return copy.deepcopy(row)


__all__ = ["AppendOnlyLedger", "LedgerIntegrityError"]
