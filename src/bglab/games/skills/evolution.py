"""Evidence-gated post-game strategy evolution on the normal fork harness."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from bglab.games.persistence.store import GameStore

logger = logging.getLogger("bglab.games.skills")


def _read_jsonl(path: Path) -> list[dict]:
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


def _sample_evenly(items: list[dict], limit: int) -> list[dict]:
    if len(items) <= limit:
        return list(items)
    if limit <= 1:
        return [items[-1]]
    indexes = {
        round(index * (len(items) - 1) / (limit - 1))
        for index in range(limit)
    }
    return [items[index] for index in sorted(indexes)]


def _build_evidence_packet(store: GameStore, manifest: dict, decisions: list[dict]) -> Path:
    """Write a bounded model-facing digest; raw decisions stay local-only."""
    from bglab.games.skills.loader import extract_features

    compact_decisions: list[dict] = []
    for decision in _sample_evenly(decisions, 16):
        state = decision.get("state", {}) if isinstance(decision.get("state"), dict) else {}
        compact_decisions.append({
            "turn_id": decision.get("turn_id", decision.get("turn")),
            "pid": decision.get("pid"),
            "transaction": decision.get("transaction"),
            "canonical_action": decision.get("canonical_action", decision.get("action")),
            "rejected_attempts": list(decision.get("rejected_attempts", []))[:3],
            "loaded_skills": list(decision.get("loaded_skills", []))[:8],
            "features": extract_features(state),
            "wrapper": state.get("wrapper", {}),
        })

    reports = _sample_evenly(_read_jsonl(store.dir / "turn_reports.jsonl"), 16)
    compact_reports = [{
        "turn_id": item.get("turn_id"),
        "pid": item.get("pid"),
        "report": str(item.get("report", item.get("text", "")))[:600],
    } for item in reports]
    relevant_events = [
        item for item in store.read_events()
        if item.get("type") in {
            "skill_loaded", "game_chat", "game_paused", "team_shutdown_error",
            "skill_review_failed", "game_memory_extraction_failed",
        }
    ][-20:]
    packet = {
        "schema_version": 1,
        "game_id": store.game_id,
        "manifest": {
            key: manifest.get(key) for key in (
                "engine", "player_count", "player_types", "mode", "winner",
                "final_scores", "current_turn", "last_confirmed_turn_id",
                "skillset_version", "missing_act_recoveries",
            )
        },
        "decision_count": len(decisions),
        "decision_samples": compact_decisions,
        "turn_report_samples": compact_reports,
        "relevant_events": relevant_events,
    }
    path = store.dir / "skill_evidence.json"
    store._atomic_json(path, packet)
    return path


async def evolve_game_skills(store: GameStore, engine: str = "splendor") -> str:
    """Stage proposals from one game, aggregate evidence, and release if eligible.

    The fork can write only into this game's staging directory. Promotion and
    release activation are deterministic and never delegated to the model.
    """
    from bglab.games.skills.evolution_store import (
        ProposalValidationError,
        atomic_json,
        create_release,
        evaluate_proposal,
        existing_skill_slugs,
        merge_proposal,
        normalize_proposal,
        read_json,
        release_scope_allows,
    )
    from bglab.games.skills.loader import (
        get_builtin_skill_dir,
        get_game_skill_listing,
        get_skill_evolution_root,
        get_skillset_version,
    )
    from bglab.utils.forked_agent import build_memory_extraction_fork, run_forked_agent

    root = get_skill_evolution_root(engine)
    records_dir = root / "proposals" / "records"
    staging = root / "proposals" / ".staging" / store.game_id
    archive = root / "proposals" / "archive" / store.game_id
    records_dir.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)
    archive.mkdir(parents=True, exist_ok=True)

    manifest = store.read_manifest()
    current_version = get_skillset_version(engine)
    decisions = _read_jsonl(store.dir / "turn_decisions.jsonl")
    evidence_packet = _build_evidence_packet(store, manifest, decisions)
    builtins = get_builtin_skill_dir(engine)
    prompt = f"""Review the completed {engine} game and propose conservative changes to its on-demand Skill library.

Evidence packet (read-only): {evidence_packet}
The packet is a bounded deterministic sample. Do not search for or read the raw
turn_decisions/events/reports files; the runtime validates cited turns against
the complete local records after you finish.

Built-in and active Skill sources (read-only):
- {builtins}
- Current catalog:\n{get_game_skill_listing(engine)}

Writable proposal staging directory: {staging}
Current skillset version: {current_version}
Current game ID: {store.game_id}
Existing candidate records (read-only): {records_dir}

You cannot modify live Skills. Write zero or more JSON proposal files into the staging directory. NO_CHANGE is preferred when one game gives weak evidence. Each file must be one JSON object with:
- operation: NEW, UPDATE, or RETIRE
- skill_slug: safe lowercase kebab-case
- base_skillset_version: exactly {current_version}
- claim: one reusable conditional strategic claim
- conditions: array of concrete board/timing conditions
- counterexamples: array of cases where it should not be followed
- risk: consequence if the claim is wrong
- source_turns: exact turn_id values present in the evidence packet
- proposed_skill_markdown: complete SKILL.md for NEW/UPDATE; empty for RETIRE

Before proposing, Read a matching candidate record when one exists and preserve its exact claim and conditions only when this game independently supports the same formulation. Otherwise propose a different skill slug. Do not invent other game IDs, seat histories, wins, or actions. Do not infer persistent identity from P0/P1. Do not use absolute claims such as always/every time/zero opportunity cost. The runtime supplies evidence identity and decides promotion after at least three distinct games and two seats or AI identities.

Finish with a concise NEW/UPDATE/RETIRE/NO_CHANGE review.
"""
    tools, handlers = build_memory_extraction_fork(prompt, memory_dir=str(staging))
    result = await run_forked_agent(
        prompt=prompt,
        system_prompt=(
            "You are a conservative proposal author for an on-demand Skill library. "
            "You may only write candidate JSON proposals to the supplied staging directory. "
            "You never publish, activate, or edit live strategy guides."
        ),
        tools=tools,
        handlers=handlers,
        model="deepseek-chat",
        max_turns=8,
    )

    accepted: list[dict] = []
    rejected = 0
    for proposal_file in sorted(staging.glob("*.json")):
        try:
            normalized = normalize_proposal(
                read_json(proposal_file),
                engine=engine,
                game_id=store.game_id,
                base_skillset_version=current_version,
                decisions=decisions,
                player_count=int(manifest.get("player_count", 0) or 0),
            )
            merged = merge_proposal(records_dir, normalized)
            accepted.append(merged)
            store.log_event({
                "type": "skill_proposal_accepted",
                "game_id": store.game_id,
                "proposal_id": merged["proposal_id"],
                "operation": merged["operation"],
                "evidence_games": len(merged["evidence_games"]),
            })
        except (OSError, ValueError, TypeError, ProposalValidationError) as exc:
            rejected += 1
            store.log_event({
                "type": "skill_proposal_rejected",
                "game_id": store.game_id,
                "file": proposal_file.name,
                "error": str(exc)[:300],
            })
        finally:
            destination = archive / proposal_file.name
            if destination.exists():
                destination = archive / f"{proposal_file.stem}-{proposal_file.stat().st_mtime_ns}.json"
            os.replace(proposal_file, destination)

    existing = existing_skill_slugs(engine)
    eligible: list[dict] = []
    for record_path in sorted(records_dir.glob("*.json")):
        try:
            proposal = read_json(record_path)
            if proposal.get("status") in {"promoted", "rejected"}:
                continue
            ok, errors = evaluate_proposal(
                proposal,
                current_skillset_version=current_version,
                existing_slugs=existing,
            )
            proposal["validation_errors"] = errors
            proposal["status"] = "eligible" if ok else "candidate"
            atomic_json(record_path, proposal)
            if ok:
                eligible.append(proposal)
        except (OSError, ValueError, TypeError, ProposalValidationError) as exc:
            store.log_event({
                "type": "skill_proposal_validation_failed",
                "game_id": store.game_id,
                "file": record_path.name,
                "error": str(exc)[:300],
            })

    release_version = ""
    releasable: list[dict] = []
    for proposal in eligible:
        allowed, reason = release_scope_allows(proposal)
        if allowed:
            releasable.append(proposal)
        else:
            proposal["rollout_blocked_reason"] = reason
            atomic_json(records_dir / f"{proposal['proposal_id']}.json", proposal)
    if releasable:
        release = create_release(root, engine, releasable, current_version)
        release_version = release["version"]
        for proposal in releasable:
            proposal["status"] = "promoted"
            proposal["release_version"] = release_version
            atomic_json(records_dir / f"{proposal['proposal_id']}.json", proposal)
        store.log_event({
            "type": "skill_release_activated",
            "game_id": store.game_id,
            "release_version": release_version,
            "previous_skillset_version": current_version,
            "skillset_version": get_skillset_version(engine),
            "proposal_ids": [item["proposal_id"] for item in releasable],
        })

    model_review = result.text.strip()
    review = (
        f"{model_review or 'NO_CHANGE'}\n\n"
        f"Validated proposals: accepted={len(accepted)}, rejected={rejected}, "
        f"eligible={len(eligible)}, promoted={len(releasable)}, "
        f"release={release_version or 'none'}."
    )
    store.write_skill_review(review)
    store.update_manifest(
        skillset_version_after_review=get_skillset_version(engine),
        skill_review_status="complete",
        skill_proposals_accepted=len(accepted),
        skill_proposals_rejected=rejected,
        skill_release_version=release_version or None,
    )
    store.log_event({
        "type": "skill_review_completed",
        "game_id": store.game_id,
        "accepted": len(accepted),
        "rejected": rejected,
        "promoted": len(releasable),
        "release_version": release_version or None,
        "review": review[:500],
    })
    logger.info("Game skill evolution complete for %s: %s", store.game_id, review[:120])
    return review
