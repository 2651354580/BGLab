"""Bounded host recovery after generation failure; BgAct still owns submission."""
from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import Mapping
from typing import Any

from bglab.games.authority_worker import AuthorityWorker
from bglab.games.registry import GameDefinition
from bglab.games.replay import authority_hash
from bglab.games.semantic_validation import load_semantic_descriptor, project_authority_program
from bglab.games.tools.semantic_lifecycle import restore_semantic_lifecycle, semantic_identity_from_ctx, semantic_route_id


FALLBACK_FAILURE_REASONS = frozenset({"output_limit", "repeated_output", "timeout", "decision_exhausted", "invalid_tool_arguments", "context_capacity"})


def select_fallback_action(
    definition: GameDefinition, snapshot: dict, context: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    """Use a checked route, a package recovery preference, or the first legal route.

    This is not a strategic ranking. Unspecified choices in a checked route
    remain host choices, even when the route has an exact intent binding.
    """
    identity = semantic_identity_from_ctx(context)
    if (identity is None or snapshot.get("decisionId") != identity.decision_id
            or authority_hash(snapshot) != identity.state_hash):
        raise ValueError("FALLBACK_STALE_AUTHORITY")
    if context.get("_act_submitted") or context.get("_semantic_reconciliation_required"):
        raise ValueError("FALLBACK_REQUIRES_RECONCILIATION")
    raw = context.get("_semantic_lifecycle_v2")
    lifecycle = restore_semantic_lifecycle(raw, identity)
    if raw is not None and lifecycle is None:
        raise ValueError("FALLBACK_STALE_LIFECYCLE")
    if lifecycle is not None:
        if lifecycle.commit_fence is not None:
            raise ValueError("FALLBACK_EXISTING_COMMIT_FENCE")
        for candidate in lifecycle.candidates.values():
            if candidate.commit_ready and candidate.intent_exact:
                # Reuse its immutable binding; do not re-project and compile a
                # checked route into a potentially different direct chain.
                return {"operation": "commit", "id": semantic_route_id(identity, candidate)}, "current_checked_route"
    worker = AuthorityWorker(definition, copy.deepcopy(snapshot),
                             decision_id=identity.decision_id, request_timeout_s=30.0)
    try:
        request = {"decisionId": identity.decision_id, "snapshotFingerprint": identity.state_hash}
        preferred = dict(definition.host_recovery_preferred_step)
        source = "engine_first_legal"
        page = worker.enumerate_routes({**request, "limit": 1, "stepsContain": [preferred]}) if preferred else {}
        if page.get("programs"):
            source = "package_preferred_legal"
        else:
            page = worker.enumerate_routes({**request, "limit": 1})
        programs = page.get("programs", [])
        if not programs:
            raise ValueError("FALLBACK_NO_COMPLETE_ROUTE")
        program = programs[0]
        steps = program.get("steps", program.get("engineSteps"))
        validated = worker.validate_transaction({"steps": steps}, request)
        if not validated.get("ok") or not validated.get("complete"):
            raise ValueError("FALLBACK_ROUTE_REJECTED")
        projected = project_authority_program(load_semantic_descriptor(definition), {
            **program, "programId": "host-fallback", "complete": True, "steps": steps,
            "outcome": validated.get("outcome", validated.get("netOutcome", {})),
            "causalTrace": validated.get("causalTrace", []),
            "publicSummary": validated.get("publicSummary"),
        })
        return {"operation": "commit", "chains": [{"actions": [
            dict(action=item.action, **dict(item.args)) for item in projected.chain.actions
        ]}]}, source
    finally:
        worker.close()


async def try_host_fallback(agent: Any, failure: Any) -> bool:
    """Recover only while the original authority-bound attempt still owns commit."""
    reason = failure if isinstance(failure, str) else failure.reason
    if not agent.host_fallback_enabled or reason not in FALLBACK_FAILURE_REASONS:
        return False
    ctx = agent.tool_ctx
    guard = ctx.get("_attempt_guard")
    act = next((tool for tool in agent.tools if tool.name == "BgAct"), None)
    if (act is None or not callable(guard) or not guard() or ctx.get("_act_submitted")
            or ctx.get("_semantic_reconciliation_required")):
        return False
    identity = semantic_identity_from_ctx(ctx)
    if identity is None:
        return False
    if agent.host_fallbacks.get(identity.decision_id, {}).get("status") == "committed":
        # An uncertain earlier attempt needs reconciliation, not another move.
        return False
    snapshot_record = agent.store.read_snapshot()
    if not isinstance(snapshot_record, dict) or not isinstance(snapshot_record.get("state"), dict):
        return False
    selection = asyncio.create_task(asyncio.to_thread(
        select_fallback_action, agent.definition, snapshot_record["state"], dict(ctx),
    ))
    try:
        payload, source = await asyncio.shield(selection)
    except asyncio.CancelledError:
        # The read-only worker owns a subprocess. Drain it before the old
        # attempt can be reported stopped; its finally closes that process.
        while not selection.done():
            try:
                await asyncio.shield(selection)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not selection.cancelled():
            selection.exception()
        raise
    if not guard():
        return False
    record = {
        "schemaVersion": 1, "decisionId": identity.decision_id,
        "authorityHash": identity.state_hash, "seat": identity.seat,
        "reason": reason, "source": source, "status": "prepared",
        "inputSurfaceHash": identity.input_surface_hash,
    }
    agent.host_fallbacks[identity.decision_id] = record
    # Preserve origin before BgAct's prepared fence and authority dispatch.
    # A process failure must not turn a host decision into a model decision.
    agent.persist()
    if not guard():
        return False
    ctx["_host_fallback_dispatch"] = identity.decision_id
    try:
        result = act.call(payload)
        if inspect.isawaitable(result):
            result = await result
    finally:
        ctx.pop("_host_fallback_dispatch", None)
    if not ctx.get("_act_submitted"):
        raise RuntimeError("HOST_FALLBACK_COMMIT_NOT_CONFIRMED")
    return True


def fallback_notice(agent: Any) -> tuple[str, str] | None:
    record = getattr(agent, "host_fallbacks", {}).get(getattr(agent, "last_committed_turn_id", None))
    if not record or record.get("status") != "committed":
        return None
    return (
        f"host_fallback:{agent.game_id}:p{agent.pid}:{record['decisionId']}",
        "<game-recovery>\n上次模型生成未完成，宿主已通过 BgAct 提交一条引擎校验的兜底路线。"
        "这是宿主恢复动作，不代表你已选择它，也不保证策略最优。不要重放；后续决定以最新 Frame 为准。"
        "\n</game-recovery>",
    )
