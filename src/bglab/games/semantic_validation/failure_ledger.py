from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterator, Mapping, Sequence

import yaml

from .ledger import AppendOnlyLedger, LedgerIntegrityError


class FailureLedgerError(RuntimeError):
    """Raised when RC failure state is invalid, incomplete, or has drifted."""


class FailureStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class FailureCategory(StrEnum):
    MODEL_VISIBLE_FACTS = "MODEL_VISIBLE_FACTS"
    AUTHORITY_SEARCH = "AUTHORITY_SEARCH"
    LIFECYCLE = "LIFECYCLE"
    FRAME_FACTS = "FRAME_FACTS"
    TOOL_CONTRACT = "TOOL_CONTRACT"
    SCORE_TIMING = "SCORE_TIMING"
    JUDGE_CALIBRATION = "JUDGE_CALIBRATION"


class FailureProbeKind(StrEnum):
    SEMANTIC_NORMALIZATION = "semantic-normalization"
    RESULT_JUDGE = "result-judge"
    CHECKED_ROUTE_LIFECYCLE = "checked-route-lifecycle"
    COMPLETED_GAME = "completed-game"


_ERROR_ID = re.compile(r"^RC-[A-Z]+-[0-9]{3}$")
_OWNER_MODULE = re.compile(r"^M[0-6]$")
_FILESYSTEM_ERROR = "failure ledger filesystem operation failed"
_DEFINITION_FIELDS = frozenset(
    {
        "errorId",
        "category",
        "ownerModule",
        "sourceReport",
        "probeKind",
        "focusedTest",
        "externalSource",
    },
)


@dataclass(frozen=True)
class FailureDefinition:
    error_id: str
    category: FailureCategory
    owner_module: str
    source_report: str
    probe_kind: FailureProbeKind
    focused_test: str
    external_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "errorId": self.error_id,
            "category": self.category.value,
            "ownerModule": self.owner_module,
            "sourceReport": self.source_report,
            "probeKind": self.probe_kind.value,
            "focusedTest": self.focused_test,
        }
        if self.external_source is not None:
            value["externalSource"] = self.external_source
        return value


@dataclass(frozen=True)
class EvidenceBinding:
    path: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "EvidenceBinding":
        if (
            not isinstance(value, dict)
            or set(value) != {"path", "size", "sha256"}
            or not isinstance(value.get("path"), str)
            or not value["path"]
            or not isinstance(value.get("size"), int)
            or isinstance(value["size"], bool)
            or value["size"] < 0
            or not isinstance(value.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None
        ):
            raise FailureLedgerError(
                "failure ledger integrity evidence binding is malformed",
            )
        return cls(
            path=value["path"],
            size=value["size"],
            sha256=value["sha256"],
        )


@dataclass(frozen=True)
class FailureRecord:
    definition: FailureDefinition
    status: FailureStatus
    reopen_count: int
    evidence: tuple[EvidenceBinding, ...]
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "errorId": self.definition.error_id,
            "status": self.status.value,
            "reopenCount": self.reopen_count,
            "evidence": [binding.to_dict() for binding in self.evidence],
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RcFailureManifest:
    manifest_id: str
    path: Path
    sha256: str
    definitions: Mapping[str, FailureDefinition]

    @classmethod
    def load(cls, path: str | Path) -> "RcFailureManifest":
        resolved = Path(path).resolve()
        try:
            raw = resolved.read_bytes()
            value = yaml.safe_load(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            raise FailureLedgerError("failure manifest cannot be read") from exc
        if not isinstance(value, dict) or value.get("schemaVersion") != 1:
            raise FailureLedgerError("failure manifest schemaVersion must be 1")
        manifest_id = value.get("manifestId")
        errors = value.get("errors")
        if (
            not isinstance(manifest_id, str)
            or not manifest_id.strip()
            or not isinstance(errors, list)
            or not errors
        ):
            raise FailureLedgerError("failure manifest is malformed")

        definitions: dict[str, FailureDefinition] = {}
        for raw_definition in errors:
            definition = _load_definition(raw_definition)
            if definition.error_id in definitions:
                raise FailureLedgerError(
                    f"duplicate failure definition: {definition.error_id}",
                )
            definitions[definition.error_id] = definition
        return cls(
            manifest_id=manifest_id.strip(),
            path=resolved,
            sha256=hashlib.sha256(raw).hexdigest(),
            definitions=MappingProxyType(definitions),
        )

    def validate_sources(self, repo_root: str | Path) -> None:
        # Task 1 validates portable locations and test selectors only. Task 7's
        # ReplaySummary binds the source contents and runner inputs by hash.
        root = Path(repo_root).resolve()
        if not root.is_dir():
            raise FailureLedgerError("failure source repository root does not exist")
        for definition in self.definitions.values():
            _require_existing_relative_file(
                definition.source_report,
                root=root,
                field="sourceReport",
            )
            test_path, test_name = definition.focused_test.split("::", 1)
            resolved = _require_existing_relative_file(
                test_path,
                root=root,
                field="focusedTest",
            )
            try:
                source = resolved.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise FailureLedgerError(
                    "failure focusedTest cannot be read",
                ) from exc
            if re.search(
                rf"^(?:async\s+)?def\s+{re.escape(test_name)}\s*\(",
                source,
                re.MULTILINE,
            ) is None:
                raise FailureLedgerError(
                    f"failure focusedTest does not exist: {definition.focused_test}",
                )


class RcFailureLedger:
    """Manifest-bound RC error state replayed from an append-only event chain."""

    def __init__(
        self,
        manifest: RcFailureManifest,
        state_dir: Path,
        records: Mapping[str, FailureRecord],
        ledger_head: str,
    ) -> None:
        self.manifest = manifest
        self.state_dir = state_dir
        self.state_path = state_dir / "failure-ledger.json"
        self.ledger = AppendOnlyLedger(state_dir / "events.jsonl")
        self.records = dict(records)
        self._ledger_head = ledger_head

    @classmethod
    def initialize(
        cls,
        manifest: str | Path,
        state_dir: str | Path,
    ) -> "RcFailureLedger":
        loaded = RcFailureManifest.load(manifest)
        resolved_state = Path(state_dir).resolve()
        existing = False
        with _writer_lock(resolved_state):
            state_path = resolved_state / "failure-ledger.json"
            events_path = resolved_state / "events.jsonl"
            if state_path.exists() or events_path.exists():
                existing = True
            else:
                records = {
                    error_id: FailureRecord(
                        definition=definition,
                        status=FailureStatus.OPEN,
                        reopen_count=0,
                        evidence=(),
                        reason=None,
                    )
                    for error_id, definition in loaded.definitions.items()
                }
                ledger = AppendOnlyLedger(events_path)
                try:
                    row = ledger.append(
                        "failure_ledger_initialized",
                        {
                            "manifestId": loaded.manifest_id,
                            "manifestHash": loaded.sha256,
                            "records": {
                                error_id: record.to_dict()
                                for error_id, record in records.items()
                            },
                        },
                    )
                except OSError as exc:
                    raise FailureLedgerError(_FILESYSTEM_ERROR) from exc
                instance = cls(loaded, resolved_state, records, str(row["entryHash"]))
                try:
                    instance._write_state()
                except OSError as exc:
                    raise FailureLedgerError(_FILESYSTEM_ERROR) from exc
                return instance
        if existing:
            return cls.open(loaded.path, resolved_state)
        raise AssertionError("unreachable failure ledger initialization state")

    @classmethod
    def open(
        cls,
        manifest: str | Path,
        state_dir: str | Path,
    ) -> "RcFailureLedger":
        loaded = RcFailureManifest.load(manifest)
        resolved_state = Path(state_dir).resolve()
        ledger = AppendOnlyLedger(resolved_state / "events.jsonl")
        try:
            rows = ledger.verify()
        except LedgerIntegrityError as exc:
            raise FailureLedgerError(
                f"failure ledger integrity check failed: {exc}",
            ) from exc
        except OSError as exc:
            raise FailureLedgerError(_FILESYSTEM_ERROR) from exc
        records = _replay(loaded, rows, state_dir=resolved_state)
        head = str(rows[-1]["entryHash"])
        instance = cls(loaded, resolved_state, records, head)
        try:
            snapshot = json.loads(instance.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            snapshot = None
        if snapshot == instance.to_dict():
            return instance

        with _writer_lock(resolved_state):
            try:
                current_rows = ledger.verify()
            except LedgerIntegrityError as exc:
                raise FailureLedgerError(
                    f"failure ledger integrity check failed: {exc}",
                ) from exc
            except OSError as exc:
                raise FailureLedgerError(_FILESYSTEM_ERROR) from exc
            current_records = _replay(
                loaded,
                current_rows,
                state_dir=resolved_state,
            )
            current_head = str(current_rows[-1]["entryHash"])
            recovered = cls(loaded, resolved_state, current_records, current_head)
            try:
                recovered._write_state()
            except OSError as exc:
                raise FailureLedgerError(_FILESYSTEM_ERROR) from exc
            return recovered

    @property
    def head_hash(self) -> str:
        try:
            current = self.ledger.head_hash
        except LedgerIntegrityError as exc:
            raise FailureLedgerError(
                f"failure ledger integrity check failed: {exc}",
            ) from exc
        except OSError as exc:
            raise FailureLedgerError(_FILESYSTEM_ERROR) from exc
        if current != self._ledger_head:
            raise FailureLedgerError("failure ledger integrity head mismatch")
        return current

    def status(self, error_id: str) -> FailureRecord:
        try:
            return self.records[error_id]
        except KeyError as exc:
            raise FailureLedgerError(f"unknown failure: {error_id}") from exc

    def close(
        self,
        error_id: str,
        *,
        evidence: Sequence[str],
    ) -> FailureRecord:
        current = self.status(error_id)
        if current.status is FailureStatus.CLOSED:
            raise FailureLedgerError(f"failure {error_id} is already CLOSED")
        bindings = self._bind_evidence(evidence)
        record = FailureRecord(
            definition=current.definition,
            status=FailureStatus.CLOSED,
            reopen_count=current.reopen_count,
            evidence=bindings,
            reason=None,
        )
        self._transition("failure_closed", record)
        return record

    def reopen(self, error_id: str, *, reason: str) -> FailureRecord:
        current = self.status(error_id)
        if current.status is not FailureStatus.CLOSED:
            raise FailureLedgerError(f"failure {error_id} is not CLOSED")
        if not isinstance(reason, str) or not reason.strip():
            raise FailureLedgerError("reopen reason must be non-empty")
        record = FailureRecord(
            definition=current.definition,
            status=FailureStatus.OPEN,
            reopen_count=current.reopen_count + 1,
            evidence=(),
            reason=reason.strip(),
        )
        self._transition("failure_reopened", record)
        return record

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "manifestId": self.manifest.manifest_id,
            "manifestHash": self.manifest.sha256,
            "ledgerHead": self._ledger_head,
            "records": {
                error_id: self.records[error_id].to_dict()
                for error_id in self.manifest.definitions
            },
        }

    def _bind_evidence(self, evidence: Sequence[str]) -> tuple[EvidenceBinding, ...]:
        if isinstance(evidence, (str, bytes)):
            raise FailureLedgerError("closing an error requires evidence paths")
        normalized = tuple(evidence)
        if not normalized or any(
            not isinstance(item, str) or not item.strip()
            for item in normalized
        ):
            raise FailureLedgerError("closing an error requires evidence")
        bindings: list[EvidenceBinding] = []
        for item in normalized:
            selected = item.strip()
            path = Path(selected)
            if path.is_absolute():
                resolved = path.resolve()
            else:
                if path.drive:
                    raise FailureLedgerError(
                        f"closing evidence escapes state_dir: {selected}",
                    )
                resolved = (self.state_dir / path).resolve()
                try:
                    resolved.relative_to(self.state_dir)
                except ValueError as exc:
                    raise FailureLedgerError(
                        f"closing evidence escapes state_dir: {selected}",
                    ) from exc
            if resolved in _ledger_owned_paths(self.state_dir):
                raise FailureLedgerError(
                    f"closing evidence cannot bind ledger-owned path: {selected}",
                )
            if not resolved.is_file():
                raise FailureLedgerError(f"closing evidence does not exist: {selected}")
            try:
                content = resolved.read_bytes()
            except OSError as exc:
                raise FailureLedgerError("closing evidence cannot be read") from exc
            bindings.append(
                EvidenceBinding(
                    path=selected,
                    size=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                ),
            )
        return tuple(bindings)

    def _transition(self, event: str, record: FailureRecord) -> None:
        with _writer_lock(self.state_dir):
            try:
                disk_head = self.ledger.head_hash
            except LedgerIntegrityError as exc:
                raise FailureLedgerError(
                    f"failure ledger integrity check failed: {exc}",
                ) from exc
            except OSError as exc:
                raise FailureLedgerError(_FILESYSTEM_ERROR) from exc
            if disk_head != self._ledger_head:
                raise FailureLedgerError(
                    "failure ledger stale or concurrent writer advanced the head",
                )
            try:
                row = self.ledger.append(event, {"record": record.to_dict()})
            except OSError as exc:
                raise FailureLedgerError(_FILESYSTEM_ERROR) from exc
            self.records[record.definition.error_id] = record
            self._ledger_head = str(row["entryHash"])
            try:
                self._write_state()
            except OSError as exc:
                raise FailureLedgerError(
                    "failure ledger durable event committed but snapshot write failed; "
                    "reopen the ledger to recover",
                ) from exc

    def _write_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            _canonical_json(self.to_dict()) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)


@contextmanager
def _writer_lock(state_dir: Path) -> Iterator[None]:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FailureLedgerError(_FILESYSTEM_ERROR) from exc
    lock_path = state_dir / ".writer.lock"
    try:
        handle = lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
    except OSError as exc:
        raise FailureLedgerError(_FILESYSTEM_ERROR) from exc
    try:
        _lock_file_nonblocking(handle)
    except OSError as exc:
        handle.close()
        raise FailureLedgerError("failure ledger writer lock is already held") from exc
    try:
        yield
    finally:
        try:
            handle.seek(0)
            _unlock_file(handle)
            handle.close()
        except OSError as exc:
            raise FailureLedgerError(_FILESYSTEM_ERROR) from exc


def _lock_file_nonblocking(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _ledger_owned_paths(state_dir: Path) -> frozenset[Path]:
    return frozenset(
        {
            (state_dir / "events.jsonl").resolve(),
            (state_dir / "failure-ledger.json").resolve(),
            (state_dir / "failure-ledger.json.tmp").resolve(),
            (state_dir / ".writer.lock").resolve(),
        },
    )


def _load_definition(value: Any) -> FailureDefinition:
    if not isinstance(value, dict) or set(value) - _DEFINITION_FIELDS:
        raise FailureLedgerError("failure definition is malformed")
    error_id = value.get("errorId")
    owner = value.get("ownerModule")
    source_report = value.get("sourceReport")
    focused_test = value.get("focusedTest")
    external_source = value.get("externalSource")
    if not isinstance(error_id, str) or not _ERROR_ID.fullmatch(error_id):
        raise FailureLedgerError("failure errorId must match RC-[A-Z]+-[0-9]{3}")
    if not isinstance(owner, str) or not _OWNER_MODULE.fullmatch(owner):
        raise FailureLedgerError("failure ownerModule must be M0-M6")
    try:
        category = FailureCategory(value.get("category"))
    except ValueError as exc:
        raise FailureLedgerError("failure category is invalid") from exc
    try:
        probe_kind = FailureProbeKind(value.get("probeKind"))
    except ValueError as exc:
        raise FailureLedgerError("failure probeKind is invalid") from exc
    if external_source is not None and (
        not isinstance(external_source, str) or not external_source.strip()
    ):
        raise FailureLedgerError("failure externalSource must be non-empty text")
    _validate_repo_relative_path(source_report, field="sourceReport")
    _validate_focused_test_reference(focused_test)
    return FailureDefinition(
        error_id=error_id,
        category=category,
        owner_module=owner,
        source_report=source_report,
        probe_kind=probe_kind,
        focused_test=focused_test,
        external_source=external_source,
    )


def _validate_repo_relative_path(value: Any, *, field: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise FailureLedgerError(f"failure {field} must be a repository-relative path")
    relative = Path(value)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        if ".." in relative.parts:
            raise FailureLedgerError(f"failure {field} escapes the repository")
        raise FailureLedgerError(f"failure {field} must be a repository-relative path")
    return relative


def _validate_focused_test_reference(value: Any) -> None:
    if not isinstance(value, str) or value.count("::") != 1:
        raise FailureLedgerError(
            "failure focusedTest must be path::test_name",
        )
    test_path, test_name = value.split("::", 1)
    _validate_repo_relative_path(test_path, field="focusedTest")
    if not re.fullmatch(r"test_[A-Za-z0-9_]+", test_name):
        raise FailureLedgerError("failure focusedTest test name is invalid")


def _require_existing_relative_file(value: str, *, root: Path, field: str) -> Path:
    relative = _validate_repo_relative_path(value, field=field)
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise FailureLedgerError(f"failure {field} escapes the repository") from exc
    if not resolved.is_file():
        raise FailureLedgerError(f"failure {field} does not exist: {value}")
    return resolved


def _replay(
    manifest: RcFailureManifest,
    rows: Sequence[Mapping[str, Any]],
    *,
    state_dir: Path,
) -> dict[str, FailureRecord]:
    if not rows or rows[0].get("event") != "failure_ledger_initialized":
        raise FailureLedgerError("failure ledger integrity initialization is missing")
    initial_payload = rows[0].get("payload")
    if not isinstance(initial_payload, dict):
        raise FailureLedgerError("failure ledger integrity payload is malformed")
    if (
        initial_payload.get("manifestId") != manifest.manifest_id
        or initial_payload.get("manifestHash") != manifest.sha256
    ):
        raise FailureLedgerError("failure ledger integrity manifest binding mismatch")
    records = {
        error_id: FailureRecord(
            definition=definition,
            status=FailureStatus.OPEN,
            reopen_count=0,
            evidence=(),
            reason=None,
        )
        for error_id, definition in manifest.definitions.items()
    }
    if initial_payload.get("records") != {
        error_id: record.to_dict()
        for error_id, record in records.items()
    }:
        raise FailureLedgerError("failure ledger integrity initial records mismatch")

    for row in rows[1:]:
        event = row.get("event")
        payload = row.get("payload")
        raw_record = payload.get("record") if isinstance(payload, dict) else None
        record = _record_from_dict(raw_record, manifest)
        current = records[record.definition.error_id]
        if event == "failure_closed":
            if current.status is not FailureStatus.OPEN or record.status is not FailureStatus.CLOSED:
                raise FailureLedgerError("failure ledger integrity close transition is invalid")
            if record.reopen_count != current.reopen_count or not record.evidence or record.reason is not None:
                raise FailureLedgerError("failure ledger integrity close record is invalid")
            _verify_evidence_bindings(record.evidence, state_dir=state_dir)
        elif event == "failure_reopened":
            if current.status is not FailureStatus.CLOSED or record.status is not FailureStatus.OPEN:
                raise FailureLedgerError("failure ledger integrity reopen transition is invalid")
            if (
                record.reopen_count != current.reopen_count + 1
                or record.evidence
                or not record.reason
            ):
                raise FailureLedgerError("failure ledger integrity reopen record is invalid")
        else:
            raise FailureLedgerError(f"failure ledger integrity event is unknown: {event}")
        records[record.definition.error_id] = record
    return records


def _record_from_dict(
    value: Any,
    manifest: RcFailureManifest,
) -> FailureRecord:
    if not isinstance(value, dict) or set(value) != {
        "errorId", "status", "reopenCount", "evidence", "reason",
    }:
        raise FailureLedgerError("failure ledger integrity record is malformed")
    error_id = value.get("errorId")
    if not isinstance(error_id, str) or error_id not in manifest.definitions:
        raise FailureLedgerError("failure ledger integrity record has unknown error")
    try:
        status = FailureStatus(value.get("status"))
    except ValueError as exc:
        raise FailureLedgerError("failure ledger integrity status is invalid") from exc
    reopen_count = value.get("reopenCount")
    evidence = value.get("evidence")
    reason = value.get("reason")
    if (
        not isinstance(reopen_count, int)
        or isinstance(reopen_count, bool)
        or reopen_count < 0
        or not isinstance(evidence, list)
        or reason is not None
        and (not isinstance(reason, str) or not reason)
    ):
        raise FailureLedgerError("failure ledger integrity record is malformed")
    return FailureRecord(
        definition=manifest.definitions[error_id],
        status=status,
        reopen_count=reopen_count,
        evidence=tuple(EvidenceBinding.from_dict(item) for item in evidence),
        reason=reason,
    )


def _verify_evidence_bindings(
    bindings: Sequence[EvidenceBinding],
    *,
    state_dir: Path,
) -> None:
    for binding in bindings:
        path = Path(binding.path)
        if path.is_absolute():
            resolved = path.resolve()
        else:
            if path.drive:
                raise FailureLedgerError(
                    f"failure ledger integrity evidence escapes state_dir: {binding.path}",
                )
            resolved = (state_dir / path).resolve()
            try:
                resolved.relative_to(state_dir)
            except ValueError as exc:
                raise FailureLedgerError(
                    f"failure ledger integrity evidence escapes state_dir: {binding.path}",
                ) from exc
        if resolved in _ledger_owned_paths(state_dir):
            raise FailureLedgerError(
                "failure ledger integrity evidence binds a ledger-owned path: "
                f"{binding.path}",
            )
        if not resolved.is_file():
            raise FailureLedgerError(
                f"failure ledger integrity evidence is missing: {binding.path}",
            )
        try:
            content = resolved.read_bytes()
        except OSError as exc:
            raise FailureLedgerError(
                "failure ledger integrity evidence cannot be read",
            ) from exc
        if len(content) != binding.size:
            raise FailureLedgerError(
                f"failure ledger integrity evidence size mismatch: {binding.path}",
            )
        if hashlib.sha256(content).hexdigest() != binding.sha256:
            raise FailureLedgerError(
                f"failure ledger integrity evidence hash mismatch: {binding.path}",
            )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "EvidenceBinding",
    "FailureCategory",
    "FailureDefinition",
    "FailureLedgerError",
    "FailureProbeKind",
    "FailureRecord",
    "FailureStatus",
    "RcFailureLedger",
    "RcFailureManifest",
]
