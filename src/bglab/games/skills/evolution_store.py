"""Evidence-gated proposal storage and atomic releases for game Skills."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
VALID_OPERATIONS = {"NEW", "UPDATE", "RETIRE"}
RESERVED_DIRS = {"proposals", "releases", "retired"}
ABSOLUTE_CLAIM_RE = re.compile(
    r"\b(always|every time|consistently|zero opportunity cost|never fails)\b|"
    r"(永远|每次都|始终|零机会成本|绝不会)",
    re.IGNORECASE,
)
SAFE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class ProposalValidationError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_proposal_id(engine: str, operation: str, skill_slug: str) -> str:
    raw = f"{engine.lower()}:{operation.upper()}:{skill_slug.lower()}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{skill_slug.lower()}-{operation.lower()}-{digest}"


def formulation_id(claim: str, conditions: list[str]) -> str:
    normalized = json.dumps(
        {"claim": " ".join(claim.lower().split()), "conditions": sorted(
            " ".join(item.lower().split()) for item in conditions
        )},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ProposalValidationError(f"{path.name} must contain one JSON object")
    return data


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(item).strip() for item in value if str(item).strip()))


def _ints(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        try:
            parsed = int(item)
        except (TypeError, ValueError):
            continue
        if parsed not in result:
            result.append(parsed)
    return result


def normalize_proposal(
    raw: dict,
    *,
    engine: str,
    game_id: str,
    base_skillset_version: str,
    decisions: list[dict],
    player_count: int,
) -> dict:
    operation = str(raw.get("operation", "")).upper().strip()
    slug = str(raw.get("skill_slug", "")).lower().strip()
    if operation not in VALID_OPERATIONS:
        raise ProposalValidationError("operation must be NEW, UPDATE, or RETIRE")
    if not SAFE_SLUG_RE.fullmatch(slug):
        raise ProposalValidationError("skill_slug must be a safe kebab-case slug")

    claim = str(raw.get("claim", "")).strip()
    markdown = str(raw.get("proposed_skill_markdown", "")).strip()
    if not claim:
        raise ProposalValidationError("claim is required")
    if operation != "RETIRE" and not markdown:
        raise ProposalValidationError("proposed_skill_markdown is required")
    if len(markdown.encode("utf-8")) > 20_000:
        raise ProposalValidationError("proposed_skill_markdown exceeds 20KB")

    source_turns = _strings(raw.get("source_turns"))
    decision_by_turn = {
        str(item.get("turn_id", item.get("turn", ""))): item for item in decisions
    }
    samples: list[dict] = []
    seats = _ints(raw.get("evidence_seats"))
    agents = _strings(raw.get("evidence_agents"))
    for turn_id in source_turns:
        decision = decision_by_turn.get(turn_id)
        if not decision:
            continue
        pid = int(decision.get("pid", 0) or 0)
        if pid not in seats:
            seats.append(pid)
        agent_id = f"ai-p{pid}"
        if agent_id not in agents:
            agents.append(agent_id)
        samples.append({"game_id": game_id, "turn_id": turn_id, "pid": pid})

    conditions = _strings(raw.get("conditions"))
    formulation = formulation_id(claim, conditions)
    for sample in samples:
        sample["formulation_id"] = formulation
        sample["agent_id"] = f"ai-p{sample['pid']}"
    now = utc_now()
    proposal_id = stable_proposal_id(engine, operation, slug)
    return {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": proposal_id,
        "engine": engine,
        "operation": operation,
        "skill_slug": slug,
        "base_skillset_version": str(raw.get("base_skillset_version") or base_skillset_version),
        "status": "candidate",
        "claim": claim,
        "conditions": conditions,
        "counterexamples": _strings(raw.get("counterexamples")),
        "risk": str(raw.get("risk", "")).strip(),
        "evidence_games": [game_id],
        "evidence_agents": agents,
        "evidence_seats": seats,
        "evidence_player_counts": [player_count] if player_count > 0 else [],
        "source_turns": source_turns,
        "evidence_samples": samples,
        "selected_formulation_id": formulation,
        "evidence_formulations": {formulation: [game_id]},
        "proposed_skill_markdown": markdown,
        "created_at": now,
        "updated_at": now,
        "validation_errors": [],
    }


def merge_proposal(records_dir: Path, incoming: dict) -> dict:
    path = records_dir / f"{incoming['proposal_id']}.json"
    if not path.exists():
        atomic_json(path, incoming)
        return incoming
    current = read_json(path)
    merged = dict(current)
    for field in (
        "evidence_games", "evidence_agents", "evidence_seats",
        "evidence_player_counts", "source_turns",
    ):
        merged[field] = list(dict.fromkeys(list(current.get(field, [])) + list(incoming.get(field, []))))
    existing_samples = {
        (item.get("game_id"), item.get("turn_id"), item.get("pid"))
        for item in current.get("evidence_samples", []) if isinstance(item, dict)
    }
    samples = list(current.get("evidence_samples", []))
    for sample in incoming.get("evidence_samples", []):
        key = (sample.get("game_id"), sample.get("turn_id"), sample.get("pid"))
        if key not in existing_samples:
            samples.append(sample)
            existing_samples.add(key)
    merged["evidence_samples"] = samples
    formulations = dict(current.get("evidence_formulations", {}))
    for key, games in incoming.get("evidence_formulations", {}).items():
        formulations[key] = list(dict.fromkeys(list(formulations.get(key, [])) + list(games)))
    merged["evidence_formulations"] = formulations
    current_formulation = str(current.get("selected_formulation_id", ""))
    incoming_formulation = str(incoming.get("selected_formulation_id", ""))
    if len(formulations.get(incoming_formulation, [])) > len(formulations.get(current_formulation, [])):
        merged["selected_formulation_id"] = incoming_formulation
        for field in (
            "claim", "conditions", "counterexamples", "risk",
            "proposed_skill_markdown",
        ):
            if incoming.get(field):
                merged[field] = incoming[field]
    merged["updated_at"] = utc_now()
    merged["status"] = "candidate"
    merged["validation_errors"] = []
    atomic_json(path, merged)
    return merged


def _load_decisions(game_id: str) -> list[dict]:
    from bglab.games.persistence.store import GameStore

    path = GameStore(game_id).dir / "turn_decisions.jsonl"
    if not path.is_file():
        return []
    result: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            result.append(item)
    return result


def _sample_is_legal(sample: dict) -> bool:
    decisions = _load_decisions(str(sample.get("game_id", "")))
    turn_id = str(sample.get("turn_id", ""))
    pid = int(sample.get("pid", 0) or 0)
    for decision in decisions:
        observed_turn = str(decision.get("turn_id", decision.get("turn", "")))
        if observed_turn != turn_id or int(decision.get("pid", 0) or 0) != pid:
            continue
        transaction = decision.get("transaction")
        state = decision.get("state")
        if isinstance(transaction, dict) and isinstance(state, dict):
            from bglab.games.runtime import hydrate_browser_state
            from bglab.games.transactions import validate_transaction

            result = validate_transaction(hydrate_browser_state(state), pid, transaction)
            if result.status != "committed":
                return False
            recorded = decision.get("canonical_action")
            return recorded is None or recorded == result.canonical_action
        # Evidence written before transactional BgAct remains readable but is
        # not eligible for a new release because it cannot prove atomic replay.
        return False
    return False


def evaluate_proposal(proposal: dict, *, current_skillset_version: str, existing_slugs: set[str]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    selected_formulation = str(proposal.get("selected_formulation_id", ""))
    formulations = proposal.get("evidence_formulations", {})
    games = set(_strings(formulations.get(selected_formulation, []))) if isinstance(formulations, dict) else set()
    samples = [
        item for item in proposal.get("evidence_samples", [])
        if isinstance(item, dict) and item.get("formulation_id") == selected_formulation
    ]
    seats = {int(item.get("pid", 0) or 0) for item in samples}
    agents = {str(item.get("agent_id", "")) for item in samples if item.get("agent_id")}
    if len(games) < 3:
        errors.append("requires evidence from at least 3 distinct games")
    if max(len(seats), len(agents)) < 2:
        errors.append("requires evidence from at least 2 seats or persistent AI identities")
    if not samples:
        errors.append("requires replayable source turns")
    elif {str(item.get("game_id", "")) for item in samples} != games:
        errors.append("every evidence game requires at least one replayable source turn")
    elif not all(_sample_is_legal(sample) for sample in samples):
        errors.append("one or more source actions are missing or illegal")
    if not proposal.get("conditions"):
        errors.append("requires explicit board or timing conditions")
    if not proposal.get("counterexamples"):
        errors.append("requires counterexamples")
    if not proposal.get("risk"):
        errors.append("requires a stated risk")
    if len(games) < 3 and ABSOLUTE_CLAIM_RE.search(str(proposal.get("claim", ""))):
        errors.append("absolute claim is unsupported")
    operation = proposal.get("operation")
    slug = str(proposal.get("skill_slug", ""))
    if operation == "NEW" and slug in existing_slugs:
        errors.append("NEW duplicates an existing skill slug")
    if operation in {"UPDATE", "RETIRE"} and slug not in existing_slugs:
        errors.append(f"{operation} requires an existing skill slug")
    if operation in {"UPDATE", "RETIRE"} and proposal.get("base_skillset_version") != current_skillset_version:
        errors.append("base skillset version is stale")
    markdown = str(proposal.get("proposed_skill_markdown", ""))
    if operation != "RETIRE":
        if not markdown.startswith("---\n") or "displayName:" not in markdown or "description:" not in markdown:
            errors.append("proposed Skill has invalid frontmatter")
    return not errors, errors


def existing_skill_slugs(engine: str) -> set[str]:
    from bglab.games.skills.loader import _iter_skill_files

    return {slug for slug, _path in _iter_skill_files(engine)}


def release_scope_allows(proposal: dict) -> tuple[bool, str]:
    """Default rollout is AI-only; human-game activation requires an explicit flag."""
    scope = os.environ.get("BGLAB_SKILL_RELEASE_SCOPE", "ai_only").strip().lower()
    if scope == "all":
        return True, "all"
    selected = str(proposal.get("selected_formulation_id", ""))
    formulations = proposal.get("evidence_formulations", {})
    game_ids = _strings(formulations.get(selected, [])) if isinstance(formulations, dict) else []
    from bglab.games.persistence.store import GameStore

    ai_games = 0
    for game_id in game_ids:
        manifest = GameStore(game_id).read_manifest()
        player_types = manifest.get("player_types", [])
        if manifest.get("mode") == "ai_vs_ai" or (
            player_types and all(kind == "ai" for kind in player_types)
        ):
            ai_games += 1
    if ai_games >= 3:
        return True, "ai_only"
    return False, "default ai_only rollout requires 3 supporting AI-only games"


def _copy_current_overlay(engine: str, destination: Path) -> None:
    from bglab.games.skills.loader import get_evolved_skill_dir

    source = get_evolved_skill_dir(engine)
    if not source.exists():
        return
    for child in source.iterdir():
        if child.name in RESERVED_DIRS or child.name == "active.json":
            continue
        if child.is_dir() and (child / "SKILL.md").is_file():
            shutil.copytree(child, destination / child.name)


def _retired_markdown(proposal: dict) -> str:
    slug = proposal["skill_slug"]
    reason = proposal.get("claim") or "Retired by evidence review."
    return (
        "---\n"
        f"displayName: {slug}\n"
        f"description: Retired by evidence-gated evolution\n"
        "status: retired\n"
        f"game: {proposal['engine']}\n"
        "---\n\n"
        f"{reason}\n"
    )


def create_release(root: Path, engine: str, proposals: list[dict], previous_version: str) -> dict:
    releases = root / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    temp_dir = Path(tempfile.mkdtemp(prefix=".release-", dir=str(releases)))
    try:
        _copy_current_overlay(engine, temp_dir)
        for proposal in proposals:
            skill_dir = temp_dir / proposal["skill_slug"]
            skill_dir.mkdir(parents=True, exist_ok=True)
            markdown = (
                _retired_markdown(proposal)
                if proposal["operation"] == "RETIRE"
                else proposal["proposed_skill_markdown"].rstrip() + "\n"
            )
            (skill_dir / "SKILL.md").write_text(markdown, encoding="utf-8")

        digest = hashlib.sha256()
        for skill_file in sorted(temp_dir.glob("*/SKILL.md")):
            digest.update(skill_file.parent.name.encode("utf-8"))
            digest.update(skill_file.read_bytes())
        fingerprint = digest.hexdigest()[:12]
        version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{fingerprint}"
        manifest = {
            "schema_version": 1,
            "engine": engine,
            "version": version,
            "fingerprint": fingerprint,
            "previous_version": previous_version,
            "proposal_ids": [item["proposal_id"] for item in proposals],
            "created_at": utc_now(),
            "verified": True,
        }
        atomic_json(temp_dir / "manifest.json", manifest)
        final_dir = releases / version
        os.replace(temp_dir, final_dir)
        active_path = root / "active.json"
        prior_active = read_json(active_path).get("version", "") if active_path.is_file() else ""
        atomic_json(active_path, {
            "schema_version": 1,
            "engine": engine,
            "version": version,
            "previous_version": prior_active,
            "fingerprint": fingerprint,
            "activated_at": utc_now(),
        })
        return manifest
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def rollback_release(root: Path, engine: str) -> str:
    active_path = root / "active.json"
    active = read_json(active_path)
    previous = str(active.get("previous_version", ""))
    manifest_path = root / "releases" / previous / "manifest.json"
    if not previous or not manifest_path.is_file() or not read_json(manifest_path).get("verified"):
        raise ProposalValidationError("no verified previous release is available")
    previous_manifest = read_json(manifest_path)
    atomic_json(active_path, {
        "schema_version": 1,
        "engine": engine,
        "version": previous,
        "previous_version": active.get("version", ""),
        "fingerprint": previous_manifest.get("fingerprint", ""),
        "activated_at": utc_now(),
        "rollback": True,
    })
    return previous
