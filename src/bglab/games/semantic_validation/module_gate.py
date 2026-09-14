from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

import yaml

from .ledger import AppendOnlyLedger, LedgerIntegrityError


MODULE_LEDGER_VERSION = "module-ledger/v2"
MODULE_SNAPSHOT_SCHEMA_VERSION = 2


class ModuleGateError(RuntimeError):
    """Raised when a semantic module gate would lose evidence or drift."""


class ModuleStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    PARTIAL = "PARTIAL"
    FROZEN_PASS = "FROZEN_PASS"
    FROZEN_BASELINE = "FROZEN_BASELINE"
    FAIL = "FAIL"
    BLOCKED_PROVIDER = "BLOCKED_PROVIDER"
    INVALIDATED = "INVALIDATED"
    SUPERSEDED = "SUPERSEDED"


_FROZEN_STATUSES = frozenset(
    {ModuleStatus.FROZEN_PASS, ModuleStatus.FROZEN_BASELINE},
)


@dataclass(frozen=True)
class ModuleDefinition:
    module_id: str
    contract_version: str
    dependencies: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "moduleId": self.module_id,
            "contractVersion": self.contract_version,
            "dependencies": list(self.dependencies),
        }


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
    def from_dict(cls, value: Any) -> EvidenceBinding:
        if (
            not isinstance(value, dict)
            or set(value) != {"path", "size", "sha256"}
            or not isinstance(value.get("path"), str)
            or not value["path"]
            or not isinstance(value.get("size"), int)
            or isinstance(value["size"], bool)
            or value["size"] < 0
            or not _is_sha256(value.get("sha256"))
        ):
            raise ModuleGateError("module evidence binding is malformed")
        return cls(
            path=value["path"],
            size=value["size"],
            sha256=value["sha256"],
        )


@dataclass(frozen=True)
class ModuleRecord:
    module_id: str
    contract_version: str
    status: ModuleStatus
    surface_hash: str | None
    dependency_hashes: Mapping[str, str]
    evidence: tuple[EvidenceBinding, ...]
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "moduleId": self.module_id,
            "contractVersion": self.contract_version,
            "status": self.status.value,
            "surfaceHash": self.surface_hash,
            "dependencyHashes": dict(self.dependency_hashes),
            "evidence": [binding.to_dict() for binding in self.evidence],
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ModuleRecord:
        if not isinstance(value, Mapping) or set(value) != {
            "moduleId",
            "contractVersion",
            "status",
            "surfaceHash",
            "dependencyHashes",
            "evidence",
            "reason",
        }:
            raise ModuleGateError("module record is malformed")
        try:
            status = ModuleStatus(value.get("status"))
        except ValueError as exc:
            raise ModuleGateError("module record status is invalid") from exc
        dependency_hashes = value.get("dependencyHashes", {})
        evidence = value.get("evidence", [])
        if (
            not isinstance(value.get("moduleId"), str)
            or not isinstance(value.get("contractVersion"), str)
            or value.get("surfaceHash") is not None
            and not _is_sha256(value.get("surfaceHash"))
            or not isinstance(dependency_hashes, dict)
            or any(
                not isinstance(key, str) or not _is_sha256(item)
                for key, item in dependency_hashes.items()
            )
            or not isinstance(evidence, list)
            or any(not isinstance(item, dict) for item in evidence)
            or value.get("reason") is not None
            and not isinstance(value.get("reason"), str)
        ):
            raise ModuleGateError("module record is malformed")
        return cls(
            module_id=value["moduleId"],
            contract_version=value["contractVersion"],
            status=status,
            surface_hash=value.get("surfaceHash"),
            dependency_hashes=MappingProxyType(copy.deepcopy(dependency_hashes)),
            evidence=tuple(EvidenceBinding.from_dict(item) for item in evidence),
            reason=value.get("reason"),
        )


@dataclass(frozen=True)
class ModuleManifest:
    manifest_id: str
    path: Path
    sha256: str
    definitions: Mapping[str, ModuleDefinition]
    initial_records: tuple[Mapping[str, Any], ...]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _ledger_owned_paths(state_dir: Path) -> frozenset[Path]:
    return frozenset(
        {
            (state_dir / "module-ledger.jsonl").resolve(),
            (state_dir / "module-ledger.json").resolve(),
            (state_dir / "module-ledger.json.tmp").resolve(),
        },
    )


def _resolve_evidence_path(
    value: str | Path,
    *,
    evidence_root: Path,
    state_dir: Path,
) -> tuple[str, Path]:
    selected = Path(value)
    if selected.is_absolute():
        resolved = selected.resolve()
    else:
        if selected.drive or ".." in selected.parts:
            raise ModuleGateError("module evidence path escapes evidence root")
        resolved = (evidence_root / selected).resolve()
    try:
        relative = resolved.relative_to(evidence_root)
    except ValueError as exc:
        raise ModuleGateError("module evidence path escapes evidence root") from exc
    if resolved in _ledger_owned_paths(state_dir):
        raise ModuleGateError("module evidence cannot bind ledger-owned state")
    return relative.as_posix(), resolved


def _bind_evidence(
    evidence: Sequence[str | Path],
    *,
    evidence_root: Path,
    state_dir: Path,
) -> tuple[EvidenceBinding, ...]:
    if isinstance(evidence, (str, bytes, Path)):
        raise ModuleGateError("module evidence must be a sequence of paths")
    normalized = tuple(evidence)
    if not normalized:
        raise ModuleGateError("module evidence must not be empty")
    bindings: list[EvidenceBinding] = []
    for value in normalized:
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise ModuleGateError("module evidence path is invalid")
        relative, resolved = _resolve_evidence_path(
            value,
            evidence_root=evidence_root,
            state_dir=state_dir,
        )
        if not resolved.is_file():
            raise ModuleGateError(f"module evidence is missing: {relative}")
        try:
            content = resolved.read_bytes()
        except OSError as exc:
            raise ModuleGateError("module evidence cannot be read") from exc
        bindings.append(
            EvidenceBinding(
                path=relative,
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            ),
        )
    return tuple(bindings)


def _verify_evidence_bindings(
    bindings: Sequence[EvidenceBinding],
    *,
    evidence_root: Path,
    state_dir: Path,
) -> None:
    for binding in bindings:
        relative, resolved = _resolve_evidence_path(
            binding.path,
            evidence_root=evidence_root,
            state_dir=state_dir,
        )
        if relative != binding.path:
            raise ModuleGateError("module evidence path is not canonical")
        if not resolved.is_file():
            raise ModuleGateError(f"module evidence is missing: {binding.path}")
        try:
            content = resolved.read_bytes()
        except OSError as exc:
            raise ModuleGateError("module evidence cannot be read") from exc
        if len(content) != binding.size:
            raise ModuleGateError(
                f"module evidence size mismatch: {binding.path}",
            )
        if hashlib.sha256(content).hexdigest() != binding.sha256:
            raise ModuleGateError(
                f"module evidence hash mismatch: {binding.path}",
            )


def load_module_manifest(path: str | Path) -> ModuleManifest:
    resolved = Path(path).resolve()
    try:
        raw = resolved.read_bytes()
        value = yaml.safe_load(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ModuleGateError("module manifest cannot be read") from exc
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise ModuleGateError("module manifest schemaVersion must be 1")
    manifest_id = value.get("manifestId")
    modules = value.get("modules")
    initial_records = value.get("initialRecords", [])
    if (
        not isinstance(manifest_id, str)
        or not manifest_id
        or not isinstance(modules, list)
        or not modules
        or not isinstance(initial_records, list)
        or any(not isinstance(item, dict) for item in initial_records)
    ):
        raise ModuleGateError("module manifest is malformed")
    definitions: dict[str, ModuleDefinition] = {}
    for item in modules:
        if not isinstance(item, dict):
            raise ModuleGateError("module definition must be an object")
        module_id = item.get("moduleId")
        contract_version = item.get("contractVersion")
        dependencies = item.get("dependencies", [])
        if (
            not isinstance(module_id, str)
            or not module_id
            or not isinstance(contract_version, str)
            or not contract_version
            or not isinstance(dependencies, list)
            or any(not isinstance(dep, str) or not dep for dep in dependencies)
        ):
            raise ModuleGateError("module definition is malformed")
        if module_id in definitions:
            raise ModuleGateError(f"duplicate module definition: {module_id}")
        definitions[module_id] = ModuleDefinition(
            module_id=module_id,
            contract_version=contract_version,
            dependencies=tuple(dependencies),
        )
    for definition in definitions.values():
        for dependency in definition.dependencies:
            if dependency not in definitions:
                raise ModuleGateError(
                    f"module {definition.module_id} has unknown dependency {dependency}",
                )
            if dependency == definition.module_id:
                raise ModuleGateError("module cannot depend on itself")
    _validate_acyclic(definitions)
    return ModuleManifest(
        manifest_id=manifest_id,
        path=resolved,
        sha256=hashlib.sha256(raw).hexdigest(),
        definitions=MappingProxyType(definitions),
        initial_records=tuple(copy.deepcopy(initial_records)),
    )


def _validate_acyclic(definitions: Mapping[str, ModuleDefinition]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(module_id: str) -> None:
        if module_id in visiting:
            raise ModuleGateError("module dependencies contain a cycle")
        if module_id in visited:
            return
        visiting.add(module_id)
        for dependency in definitions[module_id].dependencies:
            visit(dependency)
        visiting.remove(module_id)
        visited.add(module_id)

    for module_id in definitions:
        visit(module_id)


def _default_records(manifest: ModuleManifest) -> dict[str, ModuleRecord]:
    records = {
        module_id: ModuleRecord(
            module_id=module_id,
            contract_version=definition.contract_version,
            status=ModuleStatus.NOT_RUN,
            surface_hash=None,
            dependency_hashes=MappingProxyType({}),
            evidence=(),
            reason=None,
        )
        for module_id, definition in manifest.definitions.items()
    }
    for raw in manifest.initial_records:
        module_id = raw.get("moduleId")
        if not isinstance(module_id, str) or module_id not in records:
            raise ModuleGateError("initial record references unknown module")
        definition = manifest.definitions[module_id]
        supplied = {
            "moduleId": module_id,
            "contractVersion": definition.contract_version,
            "status": raw.get("status"),
            "surfaceHash": raw.get("surfaceHash"),
            "dependencyHashes": raw.get("dependencyHashes", {}),
            "evidence": raw.get("evidence", []),
            "reason": raw.get("reason"),
        }
        record = ModuleRecord.from_dict(supplied)
        if record.status in _FROZEN_STATUSES:
            if record.surface_hash is None or not record.evidence:
                raise ModuleGateError("frozen initial record needs hash and evidence")
            if (
                record.status is ModuleStatus.FROZEN_PASS
                and record.reason is not None
            ) or (
                record.status is ModuleStatus.FROZEN_BASELINE
                and not record.reason
            ):
                raise ModuleGateError("frozen initial record reason is invalid")
            expected_dependencies = {
                dependency: records[dependency].surface_hash
                for dependency in definition.dependencies
                if records[dependency].status in _FROZEN_STATUSES
            }
            if len(expected_dependencies) != len(definition.dependencies):
                raise ModuleGateError(
                    f"frozen initial module {module_id} has unfrozen dependency",
                )
            supplied_dependencies = dict(record.dependency_hashes)
            if supplied_dependencies and supplied_dependencies != expected_dependencies:
                raise ModuleGateError("initial dependency hashes do not match")
            record = ModuleRecord(
                **{
                    **record.__dict__,
                    "dependency_hashes": MappingProxyType(expected_dependencies),
                },
            )
        records[module_id] = record
    return records


def _dependent_closure(
    definitions: Mapping[str, ModuleDefinition],
    root: str,
) -> tuple[str, ...]:
    affected: set[str] = {root}
    changed = True
    while changed:
        changed = False
        for candidate, definition in definitions.items():
            if candidate not in affected and any(
                dependency in affected for dependency in definition.dependencies
            ):
                affected.add(candidate)
                changed = True
    return tuple(candidate for candidate in definitions if candidate in affected)


def _validate_record_for_manifest(
    record: ModuleRecord,
    manifest: ModuleManifest,
) -> ModuleDefinition:
    try:
        definition = manifest.definitions[record.module_id]
    except KeyError as exc:
        raise ModuleGateError("module ledger event references unknown module") from exc
    if record.contract_version != definition.contract_version:
        raise ModuleGateError("module contract version mismatch")
    return definition


def _replay_module_ledger(
    manifest: ModuleManifest,
    rows: Sequence[Mapping[str, Any]],
    *,
    evidence_root: Path,
    state_dir: Path,
) -> dict[str, ModuleRecord]:
    if not rows or rows[0].get("event") != "module_ledger_initialized":
        raise ModuleGateError("module ledger initialization is missing")
    initial_payload = rows[0].get("payload")
    if not isinstance(initial_payload, dict):
        raise ModuleGateError("module ledger initialization payload is malformed")
    if initial_payload.get("ledgerVersion") != MODULE_LEDGER_VERSION:
        raise ModuleGateError("module ledger version is unsupported")
    if initial_payload.get("manifestId") != manifest.manifest_id:
        raise ModuleGateError("manifest id mismatch")
    if initial_payload.get("manifestHash") != manifest.sha256:
        raise ModuleGateError("manifest hash mismatch")
    expected_records = _default_records(manifest)
    for record in expected_records.values():
        _verify_evidence_bindings(
            record.evidence,
            evidence_root=evidence_root,
            state_dir=state_dir,
        )
    expected_initial_payload = {
        "ledgerVersion": MODULE_LEDGER_VERSION,
        "manifestId": manifest.manifest_id,
        "manifestHash": manifest.sha256,
        "records": {
            module_id: expected_records[module_id].to_dict()
            for module_id in manifest.definitions
        },
    }
    if initial_payload != expected_initial_payload:
        raise ModuleGateError("module ledger initialization does not match manifest")
    records = dict(expected_records)

    for row in rows[1:]:
        event = row.get("event")
        payload = row.get("payload")
        if not isinstance(payload, dict):
            raise ModuleGateError("module ledger event payload is malformed")
        if event in {"module_frozen", "module_baseline_frozen", "module_marked"}:
            raw_record = payload.get("record")
            if set(payload) != {"record"} or not isinstance(raw_record, dict):
                raise ModuleGateError("module ledger record event is malformed")
            record = ModuleRecord.from_dict(raw_record)
            _verify_evidence_bindings(
                record.evidence,
                evidence_root=evidence_root,
                state_dir=state_dir,
            )
            definition = _validate_record_for_manifest(record, manifest)
            current = records[record.module_id]
            if event == "module_frozen":
                if (
                    record.status is not ModuleStatus.FROZEN_PASS
                    or current.status in _FROZEN_STATUSES
                    or record.surface_hash is None
                    or not record.evidence
                    or record.reason is not None
                ):
                    raise ModuleGateError("module ledger freeze transition is invalid")
            elif event == "module_baseline_frozen":
                if (
                    record.status is not ModuleStatus.FROZEN_BASELINE
                    or current.status in _FROZEN_STATUSES
                    or record.surface_hash is None
                    or not record.evidence
                    or not record.reason
                ):
                    raise ModuleGateError("module ledger baseline transition is invalid")
            elif (
                record.status in _FROZEN_STATUSES
                or current.status in _FROZEN_STATUSES
                or record.surface_hash is not None
                or record.dependency_hashes
                or not record.evidence
                or not record.reason
            ):
                raise ModuleGateError("module ledger mark transition is invalid")
            if record.status in _FROZEN_STATUSES:
                expected_dependencies: dict[str, str] = {}
                for dependency in definition.dependencies:
                    dependency_record = records[dependency]
                    if (
                        dependency_record.status not in _FROZEN_STATUSES
                        or dependency_record.surface_hash is None
                    ):
                        raise ModuleGateError(
                            "module ledger frozen dependency is not frozen",
                        )
                    expected_dependencies[dependency] = dependency_record.surface_hash
                if dict(record.dependency_hashes) != expected_dependencies:
                    raise ModuleGateError(
                        "module ledger dependency hashes do not match replay",
                    )
            records[record.module_id] = record
            continue
        if event == "modules_invalidated":
            if set(payload) != {"root", "reason", "affected"}:
                raise ModuleGateError("module ledger invalidation event is malformed")
            root = payload.get("root")
            reason = payload.get("reason")
            if (
                not isinstance(root, str)
                or root not in manifest.definitions
                or not isinstance(reason, str)
                or not reason
            ):
                raise ModuleGateError("module ledger invalidation event is malformed")
            ordered = _dependent_closure(manifest.definitions, root)
            if payload.get("affected") != list(ordered):
                raise ModuleGateError("module ledger invalidation closure mismatch")
            for candidate in ordered:
                current = records[candidate]
                records[candidate] = ModuleRecord(
                    module_id=candidate,
                    contract_version=current.contract_version,
                    status=ModuleStatus.INVALIDATED,
                    surface_hash=current.surface_hash,
                    dependency_hashes=current.dependency_hashes,
                    evidence=current.evidence,
                    reason=(
                        reason
                        if candidate == root
                        else f"dependency {root} invalidated"
                    ),
                )
            continue
        raise ModuleGateError(f"unknown module ledger event: {event}")
    return records


class ModuleLedger:
    """Dependency-aware, append-only module freeze state."""

    def __init__(
        self,
        manifest: ModuleManifest,
        state_dir: Path,
        records: Mapping[str, ModuleRecord],
        ledger_head: str,
    ) -> None:
        self.manifest = manifest
        self.state_dir = state_dir
        self.state_path = state_dir / "module-ledger.json"
        self.ledger = AppendOnlyLedger(state_dir / "module-ledger.jsonl")
        self.evidence_root = state_dir.parent
        self.records = dict(records)
        self.ledger_head = ledger_head

    @classmethod
    def initialize(
        cls,
        manifest_path: str | Path,
        state_dir: str | Path,
    ) -> ModuleLedger:
        manifest = load_module_manifest(manifest_path)
        resolved_state = Path(state_dir).resolve()
        state_path = resolved_state / "module-ledger.json"
        ledger_path = resolved_state / "module-ledger.jsonl"
        if state_path.exists() or ledger_path.exists():
            return cls.open(manifest.path, resolved_state)
        records = _default_records(manifest)
        for record in records.values():
            _verify_evidence_bindings(
                record.evidence,
                evidence_root=resolved_state.parent,
                state_dir=resolved_state,
            )
        resolved_state.mkdir(parents=True, exist_ok=True)
        ledger = AppendOnlyLedger(resolved_state / "module-ledger.jsonl")
        row = ledger.append(
            "module_ledger_initialized",
            {
                "ledgerVersion": MODULE_LEDGER_VERSION,
                "manifestId": manifest.manifest_id,
                "manifestHash": manifest.sha256,
                "records": {
                    module_id: record.to_dict()
                    for module_id, record in records.items()
                },
            },
        )
        instance = cls(
            manifest,
            resolved_state,
            records,
            str(row["entryHash"]),
        )
        instance._write_state()
        return instance

    @classmethod
    def open(
        cls,
        manifest_path: str | Path,
        state_dir: str | Path,
    ) -> ModuleLedger:
        manifest = load_module_manifest(manifest_path)
        resolved_state = Path(state_dir).resolve()
        state_path = resolved_state / "module-ledger.json"
        ledger = AppendOnlyLedger(resolved_state / "module-ledger.jsonl")
        try:
            rows = ledger.verify()
        except LedgerIntegrityError as exc:
            raise ModuleGateError(str(exc)) from exc
        records = _replay_module_ledger(
            manifest,
            rows,
            evidence_root=resolved_state.parent,
            state_dir=resolved_state,
        )
        head = str(rows[-1]["entryHash"])
        instance = cls(manifest, resolved_state, records, head)
        instance._verify_all_evidence()
        if not state_path.exists():
            instance._write_state()
            return instance
        try:
            value = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ModuleGateError("module ledger state cannot be read") from exc
        if not isinstance(value, dict):
            raise ModuleGateError("module ledger state is malformed")
        snapshot_head = value.get("ledgerHead")
        matching_index = next(
            (
                index
                for index, row in enumerate(rows)
                if row.get("entryHash") == snapshot_head
            ),
            None,
        )
        if matching_index is None:
            raise ModuleGateError("module ledger snapshot head is not in JSONL")
        snapshot_records = _replay_module_ledger(
            manifest,
            rows[: matching_index + 1],
            evidence_root=resolved_state.parent,
            state_dir=resolved_state,
        )
        expected_snapshot = cls(
            manifest,
            resolved_state,
            snapshot_records,
            str(snapshot_head),
        ).to_dict()
        if value != expected_snapshot:
            raise ModuleGateError("module ledger snapshot mismatch")
        if snapshot_head != head:
            instance._write_state()
        return instance

    def status(self, module_id: str) -> ModuleRecord:
        try:
            record = self.records[module_id]
        except KeyError as exc:
            raise ModuleGateError(f"unknown module: {module_id}") from exc
        self._verify_record_evidence(record)
        return record

    def freeze(
        self,
        module_id: str,
        *,
        surface_hash: str,
        evidence: Sequence[str],
    ) -> ModuleRecord:
        definition = self._definition(module_id)
        if not _is_sha256(surface_hash):
            raise ModuleGateError("surface hash must be lowercase SHA-256")
        normalized_evidence = _bind_evidence(
            evidence,
            evidence_root=self.evidence_root,
            state_dir=self.state_dir,
        )
        dependency_hashes: dict[str, str] = {}
        for dependency in definition.dependencies:
            record = self.status(dependency)
            if record.status not in _FROZEN_STATUSES or record.surface_hash is None:
                raise ModuleGateError(f"dependency {dependency} is not frozen")
            dependency_hashes[dependency] = record.surface_hash
        current = self.records[module_id]
        desired = ModuleRecord(
            module_id=module_id,
            contract_version=definition.contract_version,
            status=ModuleStatus.FROZEN_PASS,
            surface_hash=surface_hash,
            dependency_hashes=MappingProxyType(dependency_hashes),
            evidence=normalized_evidence,
            reason=None,
        )
        if current.status in _FROZEN_STATUSES:
            if current == desired:
                return current
            raise ModuleGateError(f"module {module_id} is already frozen")
        next_records = {**self.records, module_id: desired}
        self._record(
            "module_frozen",
            {"record": desired.to_dict()},
            next_records=next_records,
        )
        return desired

    def freeze_baseline(
        self,
        module_id: str,
        *,
        surface_hash: str,
        evidence: Sequence[str],
        reason: str,
    ) -> ModuleRecord:
        definition = self._definition(module_id)
        if not _is_sha256(surface_hash):
            raise ModuleGateError("surface hash must be lowercase SHA-256")
        normalized_evidence = _bind_evidence(
            evidence,
            evidence_root=self.evidence_root,
            state_dir=self.state_dir,
        )
        if (
            not isinstance(reason, str)
            or not reason
        ):
            raise ModuleGateError("frozen baseline needs evidence and reason")
        dependency_hashes: dict[str, str] = {}
        for dependency in definition.dependencies:
            record = self.status(dependency)
            if record.status not in _FROZEN_STATUSES or record.surface_hash is None:
                raise ModuleGateError(f"dependency {dependency} is not frozen")
            dependency_hashes[dependency] = record.surface_hash
        desired = ModuleRecord(
            module_id=module_id,
            contract_version=definition.contract_version,
            status=ModuleStatus.FROZEN_BASELINE,
            surface_hash=surface_hash,
            dependency_hashes=MappingProxyType(dependency_hashes),
            evidence=normalized_evidence,
            reason=reason,
        )
        current = self.records[module_id]
        if current.status in _FROZEN_STATUSES:
            if current == desired:
                return current
            raise ModuleGateError(f"module {module_id} is already frozen")
        next_records = {**self.records, module_id: desired}
        self._record(
            "module_baseline_frozen",
            {"record": desired.to_dict()},
            next_records=next_records,
        )
        return desired

    def mark(
        self,
        module_id: str,
        *,
        status: ModuleStatus | str,
        evidence: Sequence[str],
        reason: str,
    ) -> ModuleRecord:
        selected = ModuleStatus(status)
        if selected in _FROZEN_STATUSES:
            raise ModuleGateError("use a freeze operation for frozen status")
        definition = self._definition(module_id)
        normalized_evidence = _bind_evidence(
            evidence,
            evidence_root=self.evidence_root,
            state_dir=self.state_dir,
        )
        if not isinstance(reason, str) or not reason:
            raise ModuleGateError("non-frozen verdict needs reason and evidence")
        if self.records[module_id].status in _FROZEN_STATUSES:
            raise ModuleGateError(
                f"module {module_id} is already frozen; invalidate it explicitly",
            )
        record = ModuleRecord(
            module_id=module_id,
            contract_version=definition.contract_version,
            status=selected,
            surface_hash=None,
            dependency_hashes=MappingProxyType({}),
            evidence=normalized_evidence,
            reason=reason,
        )
        next_records = {**self.records, module_id: record}
        self._record(
            "module_marked",
            {"record": record.to_dict()},
            next_records=next_records,
        )
        return record

    def invalidate(self, module_id: str, *, reason: str) -> tuple[str, ...]:
        self._definition(module_id)
        if not isinstance(reason, str) or not reason:
            raise ModuleGateError("invalidation reason must be non-empty")
        ordered = _dependent_closure(self.manifest.definitions, module_id)
        next_records = dict(self.records)
        for candidate in ordered:
            current = next_records[candidate]
            next_records[candidate] = ModuleRecord(
                module_id=candidate,
                contract_version=current.contract_version,
                status=ModuleStatus.INVALIDATED,
                surface_hash=current.surface_hash,
                dependency_hashes=current.dependency_hashes,
                evidence=current.evidence,
                reason=reason if candidate == module_id else f"dependency {module_id} invalidated",
            )
        self._record(
            "modules_invalidated",
            {"root": module_id, "reason": reason, "affected": list(ordered)},
            next_records=next_records,
        )
        return ordered

    def to_dict(self) -> dict[str, Any]:
        self._verify_all_evidence()
        return {
            "schemaVersion": MODULE_SNAPSHOT_SCHEMA_VERSION,
            "ledgerVersion": MODULE_LEDGER_VERSION,
            "manifestId": self.manifest.manifest_id,
            "manifestPath": str(self.manifest.path),
            "manifestHash": self.manifest.sha256,
            "ledgerHead": self.ledger_head,
            "records": {
                module_id: self.records[module_id].to_dict()
                for module_id in self.manifest.definitions
            },
        }

    def _definition(self, module_id: str) -> ModuleDefinition:
        try:
            return self.manifest.definitions[module_id]
        except KeyError as exc:
            raise ModuleGateError(f"unknown module: {module_id}") from exc

    def _verify_record_evidence(self, record: ModuleRecord) -> None:
        _verify_evidence_bindings(
            record.evidence,
            evidence_root=self.evidence_root,
            state_dir=self.state_dir,
        )

    def _verify_all_evidence(self) -> None:
        for record in self.records.values():
            self._verify_record_evidence(record)

    def _record(
        self,
        event: str,
        payload: Mapping[str, Any],
        *,
        next_records: Mapping[str, ModuleRecord],
    ) -> None:
        self._verify_all_evidence()
        try:
            disk_head = self.ledger.head_hash
        except LedgerIntegrityError as exc:
            raise ModuleGateError(str(exc)) from exc
        if disk_head != self.ledger_head:
            raise ModuleGateError("module ledger stale writer detected")
        try:
            row = self.ledger.append(event, payload)
        except (OSError, LedgerIntegrityError) as exc:
            raise ModuleGateError("module ledger durable append failed") from exc
        self.records = dict(next_records)
        self.ledger_head = str(row["entryHash"])
        try:
            self._write_state()
        except (OSError, ModuleGateError) as exc:
            raise ModuleGateError(
                "module ledger event committed but snapshot write failed",
            ) from exc

    def _write_state(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            _canonical_json(self.to_dict()) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)


__all__ = [
    "EvidenceBinding",
    "MODULE_LEDGER_VERSION",
    "MODULE_SNAPSHOT_SCHEMA_VERSION",
    "ModuleDefinition",
    "ModuleGateError",
    "ModuleLedger",
    "ModuleManifest",
    "ModuleRecord",
    "ModuleStatus",
    "load_module_manifest",
]
