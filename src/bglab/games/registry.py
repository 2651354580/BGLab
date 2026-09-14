"""Manifest-driven discovery for in-repository browser game packages."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from bglab.runtime_paths import runtime_path


GAME_PACKAGES_DIR = runtime_path("games")
_GAME_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class GameRegistryError(RuntimeError):
    pass


class GameManifestError(GameRegistryError):
    pass


class GameNotFoundError(GameRegistryError):
    pass


class AmbiguousGameAliasError(GameRegistryError):
    pass


@dataclass(frozen=True)
class AdapterCapabilities:
    atomic_action_chain: bool
    decision_programs: Literal["finite", "bounded", "none"]
    information_model: Literal["perfect", "seat_private"]
    turn_order: Literal["sequential"]
    chance_before_boundary: Literal["none", "declared_boundary"]
    complete_prefix_subgraph: bool = False


@dataclass(frozen=True)
class AuthorityLimits:
    max_nodes: int
    max_time_ms: int
    page_size: int


@dataclass(frozen=True)
class GameDefinition:
    id: str
    title: str
    aliases: tuple[str, ...]
    root: Path
    min_players: int
    max_players: int
    frontend_entry: Path
    rules_path: Path
    session_head_path: Path
    skills_path: Path | None
    adapter_script: Path
    adapter_protocol: int
    snapshot_version: int
    action_profile: str
    restore_versions: tuple[int, ...] = ()
    language: str | None = None
    action_schema: Path | None = None
    model_actions_path: Path | None = None
    engine_mapping_path: Path | None = None
    test_paths: tuple[Path, ...] = ()
    manifest_schema: int = 1
    capabilities: AdapterCapabilities | None = None
    authority_limits: AuthorityLimits | None = None
    coverage_action_families: tuple[str, ...] = ()
    coverage_effect_families: tuple[str, ...] = ()
    coverage_boundary_reasons: tuple[str, ...] = ()
    # Immutable package-owned factual preference for unattended host recovery.
    # It never ranks normal model candidates or overrides engine legality.
    host_recovery_preferred_step: tuple[tuple[str, str], ...] = ()

    @property
    def layer1_certifiable(self) -> bool:
        return (
            self.manifest_schema == 2
            and self.adapter_protocol == 2
            and self.language in {"javascript", "typescript"}
            and self.capabilities is not None
            and self.capabilities.atomic_action_chain
            and self.capabilities.decision_programs != "none"
            and self.model_actions_path is not None
            and self.engine_mapping_path is not None
        )


def _required_mapping(data: dict[str, Any], key: str, source: Path) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise GameManifestError(f"{source}: {key} must be an object")
    return value


def _required_text(data: dict[str, Any], key: str, source: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise GameManifestError(f"{source}: {key} must be a non-empty string")
    return value.strip()


def _required_enum(
    data: dict[str, Any], key: str, allowed: set[str], source: Path,
) -> str:
    value = _required_text(data, key, source)
    if value not in allowed:
        raise GameManifestError(f"{source}: {key} must be one of {sorted(allowed)}")
    return value


def _required_positive_int(data: dict[str, Any], key: str, source: Path) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise GameManifestError(f"{source}: {key} must be a positive integer")
    return value


def _required_catalog(data: dict[str, Any], key: str, source: Path) -> tuple[str, ...]:
    value = data.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise GameManifestError(f"{source}: coverage.{key} must be an array of non-empty strings")
    if len(set(value)) != len(value):
        raise GameManifestError(f"{source}: coverage.{key} must not contain duplicates")
    return tuple(value)


def safe_game_path(package: Path, relative: str, *, must_exist: bool = True) -> Path:
    package = package.resolve()
    candidate = (package / relative).resolve()
    if not candidate.is_relative_to(package):
        raise GameManifestError(f"{relative!r} escapes package {package}")
    if must_exist and not candidate.exists():
        raise GameManifestError(f"declared path does not exist: {candidate}")
    return candidate


def _load_manifest(path: Path) -> GameDefinition:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GameManifestError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise GameManifestError(f"{path}: manifest root must be an object")
    schema_version = raw.get("schemaVersion")
    if schema_version not in {1, 2}:
        raise GameManifestError(f"{path}: schemaVersion must be 1 or 2")

    game_id = _required_text(raw, "id", path)
    if not _GAME_ID_RE.fullmatch(game_id):
        raise GameManifestError(f"{path}: invalid game id {game_id!r}")
    if path.parent.name != game_id:
        raise GameManifestError(f"{path}: id must match package directory")
    title = _required_text(raw, "title", path)

    raw_aliases = raw.get("aliases", [])
    if not isinstance(raw_aliases, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw_aliases
    ):
        raise GameManifestError(f"{path}: aliases must be non-empty strings")
    aliases = tuple(dict.fromkeys([title, game_id, *(item.strip() for item in raw_aliases)]))

    players = _required_mapping(raw, "players", path)
    minimum, maximum = players.get("min"), players.get("max")
    if (
        not isinstance(minimum, int)
        or isinstance(minimum, bool)
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or not 1 <= minimum <= maximum <= 12
    ):
        raise GameManifestError(f"{path}: players must satisfy 1 <= min <= max <= 12")

    frontend = _required_mapping(raw, "frontend", path)
    rules = _required_mapping(raw, "rules", path)
    adapter = _required_mapping(raw, "adapter", path)
    skills = raw.get("skills")
    if skills is not None and not isinstance(skills, dict):
        raise GameManifestError(f"{path}: skills must be an object")
    commands = raw.get("commands", {})
    if not isinstance(commands, dict):
        raise GameManifestError(f"{path}: commands must be an object")
    build_command = commands.get("build")
    if build_command is not None and (
        not isinstance(build_command, str) or not build_command.strip()
    ):
        raise GameManifestError(f"{path}: commands.build must be a non-empty string")

    package = path.parent.resolve()
    frontend_entry = safe_game_path(
        package,
        _required_text(frontend, "entry", path),
        must_exist=build_command is None,
    )
    rules_path = safe_game_path(package, _required_text(rules, "path", path))
    session_head_path = (
        safe_game_path(
            package,
            _required_text(rules, "sessionHeadPath", path),
        )
        if schema_version == 2
        else rules_path
    )
    adapter_script = safe_game_path(package, _required_text(adapter, "script", path))
    skills_path = None
    if skills is not None:
        skills_path = safe_game_path(package, _required_text(skills, "path", path))
    action_schema = None
    if schema_version == 2 or adapter.get("actionSchema") is not None:
        action_schema = safe_game_path(package, _required_text(adapter, "actionSchema", path))
    model_actions_path = None
    engine_mapping_path = None
    if schema_version == 2:
        semantic = _required_mapping(raw, "semantic", path)
        model_actions_path = safe_game_path(
            package,
            _required_text(semantic, "modelActions", path),
        )
        engine_mapping_path = safe_game_path(
            package,
            _required_text(semantic, "engineMapping", path),
        )
    tests = raw.get("tests")
    test_paths: tuple[Path, ...] = ()
    if tests is not None:
        if not isinstance(tests, dict) or not isinstance(tests.get("paths"), list):
            raise GameManifestError(f"{path}: tests.paths must be an array")
        test_paths = tuple(
            safe_game_path(package, str(relative)) for relative in tests["paths"]
        )
    protocol = adapter.get("protocolVersion")
    snapshot = adapter.get("snapshotVersion")
    if protocol != schema_version:
        raise GameManifestError(
            f"{path}: adapter.protocolVersion must match schemaVersion {schema_version}",
        )
    if not isinstance(snapshot, int) or isinstance(snapshot, bool) or snapshot < 1:
        raise GameManifestError(f"{path}: adapter.snapshotVersion must be a positive integer")
    raw_restore_versions = adapter.get("restoreVersions", [snapshot])
    if (
        not isinstance(raw_restore_versions, list)
        or not raw_restore_versions
        or any(not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in raw_restore_versions)
        or snapshot not in raw_restore_versions
    ):
        raise GameManifestError(
            f"{path}: adapter.restoreVersions must include snapshotVersion",
        )

    capabilities = None
    authority_limits = None
    coverage_action_families: tuple[str, ...] = ()
    coverage_effect_families: tuple[str, ...] = ()
    coverage_boundary_reasons: tuple[str, ...] = ()
    language = raw.get("language") if isinstance(raw.get("language"), str) else None
    if schema_version == 2:
        if language not in {"javascript", "typescript"}:
            raise GameManifestError(f"{path}: language must be javascript or typescript for protocol v2")
        raw_capabilities = _required_mapping(adapter, "capabilities", path)
        atomic = raw_capabilities.get("atomicActionChain")
        if not isinstance(atomic, bool):
            raise GameManifestError(f"{path}: capabilities.atomicActionChain must be boolean")
        complete_prefix_subgraph = raw_capabilities.get("completePrefixSubgraph", False)
        if not isinstance(complete_prefix_subgraph, bool):
            raise GameManifestError(f"{path}: capabilities.completePrefixSubgraph must be boolean")
        decision_programs = _required_enum(
            raw_capabilities, "decisionPrograms", {"finite", "bounded", "none"}, path,
        )
        information_model = _required_enum(
            raw_capabilities, "informationModel", {"perfect", "seat_private"}, path,
        )
        turn_order = _required_enum(raw_capabilities, "turnOrder", {"sequential"}, path)
        chance_before_boundary = _required_enum(
            raw_capabilities, "chanceBeforeBoundary", {"none", "declared_boundary"}, path,
        )
        raw_authority = _required_mapping(adapter, "authorityEnumeration", path)
        max_nodes = _required_positive_int(raw_authority, "maxNodes", path)
        max_time_ms = _required_positive_int(raw_authority, "maxTimeMs", path)
        page_size = _required_positive_int(raw_authority, "pageSize", path)
        if max_nodes > 20000 or max_time_ms > 5000 or page_size > 12:
            raise GameManifestError(
                f"{path}: internal authority-enumeration limits exceed Layer-1 caps",
            )
        if decision_programs == "none":
            raise GameManifestError(
                f"{path}: decisionPrograms=none cannot declare authority limits",
            )
        raw_coverage = _required_mapping(raw, "coverage", path)
        coverage_action_families = _required_catalog(raw_coverage, "actionFamilies", path)
        coverage_effect_families = _required_catalog(raw_coverage, "effectFamilies", path)
        coverage_boundary_reasons = _required_catalog(raw_coverage, "boundaryReasons", path)
        capabilities = AdapterCapabilities(
            atomic_action_chain=atomic,
            decision_programs=decision_programs,
            information_model=information_model,
            turn_order=turn_order,
            chance_before_boundary=chance_before_boundary,
            complete_prefix_subgraph=complete_prefix_subgraph,
        )
        authority_limits = AuthorityLimits(max_nodes, max_time_ms, page_size)

    recovery_step: tuple[tuple[str, str], ...] = ()
    if "hostRecovery" in raw:
        recovery = raw["hostRecovery"]
        if schema_version != 2 or not isinstance(recovery, dict) or set(recovery) != {"preferredStep"}:
            raise GameManifestError(f"{path}: hostRecovery requires v2 and only preferredStep")
        step = recovery["preferredStep"]
        if (not isinstance(step, dict) or not step
                or any(not isinstance(key, str) or not key.strip()
                       or not isinstance(value, str) or not value.strip()
                       for key, value in step.items())
                or step.get("op") not in coverage_action_families):
            raise GameManifestError(f"{path}: hostRecovery.preferredStep requires string facts and a covered op")
        recovery_step = tuple(sorted(step.items()))

    return GameDefinition(
        id=game_id,
        title=title,
        aliases=aliases,
        root=package,
        min_players=minimum,
        max_players=maximum,
        frontend_entry=frontend_entry,
        rules_path=rules_path,
        session_head_path=session_head_path,
        skills_path=skills_path,
        adapter_script=adapter_script,
        adapter_protocol=protocol,
        snapshot_version=snapshot,
        action_profile=_required_text(adapter, "actionProfile", path),
        restore_versions=tuple(raw_restore_versions),
        language=language,
        action_schema=action_schema,
        model_actions_path=model_actions_path,
        engine_mapping_path=engine_mapping_path,
        test_paths=test_paths,
        manifest_schema=schema_version,
        capabilities=capabilities,
        authority_limits=authority_limits,
        coverage_action_families=coverage_action_families,
        coverage_effect_families=coverage_effect_families,
        coverage_boundary_reasons=coverage_boundary_reasons,
        host_recovery_preferred_step=recovery_step,
    )


def discover_games(root: Path | None = None) -> dict[str, GameDefinition]:
    packages = (root or GAME_PACKAGES_DIR).resolve()
    definitions: dict[str, GameDefinition] = {}
    alias_owners: dict[str, str] = {}
    if not packages.is_dir():
        return definitions
    for path in sorted(packages.glob("*/game.manifest.json")):
        if path.parent.name.startswith("_"):
            continue
        definition = _load_manifest(path)
        if definition.id in definitions:
            raise GameManifestError(f"duplicate game id: {definition.id}")
        for alias in definition.aliases:
            normalized = alias.casefold()
            owner = alias_owners.get(normalized)
            if owner is not None and owner != definition.id:
                raise GameManifestError(
                    f"duplicate alias {alias!r}: {owner} and {definition.id}",
                )
            alias_owners[normalized] = definition.id
        definitions[definition.id] = definition
    return definitions


def get_game(game_id: str) -> GameDefinition:
    definitions = discover_games()
    try:
        return definitions[game_id.casefold()]
    except KeyError as exc:
        raise GameNotFoundError(f"unknown game: {game_id}") from exc


def resolve_game(text: str) -> GameDefinition:
    query = text.casefold()
    matches: list[tuple[int, GameDefinition, str]] = []
    for definition in discover_games().values():
        for alias in definition.aliases:
            if alias.casefold() in query:
                matches.append((len(alias), definition, alias))
    if not matches:
        raise GameNotFoundError(f"no game alias found in: {text}")
    longest = max(item[0] for item in matches)
    winners = {item[1] for item in matches if item[0] == longest}
    if len(winners) != 1:
        ids = ", ".join(sorted(item.id for item in winners))
        raise AmbiguousGameAliasError(f"ambiguous game alias: {ids}")
    return winners.pop()
