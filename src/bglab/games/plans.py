"""Private, model-owned plans; their lifecycle never establishes game legality."""

from __future__ import annotations

import copy
import json
from typing import Any

from bglab.tools.base import Tool, ToolCallResult


def plan_state(ctx: dict) -> dict:
    state = ctx.setdefault("_plans", {"version": 2, "revision": 0, "turn": None, "stage": None})
    if not isinstance(state, dict) or state.get("version") != 2:
        raise ValueError("Unsupported private plan state")
    return state


def restore_plans(ctx: dict, saved: dict) -> None:
    if "plans" in saved:
        ctx["_plans"] = copy.deepcopy(saved["plans"])
        plan_state(ctx)
    else:
        # Emit one authoritative empty plan snapshot when opening old saves.
        # Otherwise historical write results could silently remain pending.
        ctx["_plans"] = {"version": 2, "revision": int(bool(saved.get("plan") or saved.get("scratchpad"))),
                         "turn": None, "stage": None}


def _close(state: dict, scope: str, status: str, reason: str, assessment: str) -> bool:
    current = state.get(scope)
    if not current or current["status"] != "active":
        return False
    state["revision"] += 1
    # Closed goals/calculations remain in the private transcript, not in the
    # active context or a growing archive that is repeated after compaction.
    state[scope] = {
        "status": status, "revision": state["revision"],
        "reason": reason, "assessment": assessment,
    }
    return True


def reconcile_turn_plan(ctx: dict, turn_group_id: str) -> None:
    state = plan_state(ctx)
    current = state.get("turn")
    if current and current["status"] == "active" and current["turnGroupId"] != turn_group_id:
        _close(state, "turn", "ended", "authoritative_action_changed", "host_boundary")


def end_action_plans(ctx: dict, boundary_reason: str | None) -> None:
    if not boundary_reason or boundary_reason == "new_information":
        return
    state = plan_state(ctx)
    _close(state, "turn", "ended", "authoritative_action_ended", "host_boundary")
    if boundary_reason == "game_finished":
        _close(state, "stage", "ended", "game_finished", "host_boundary")


def visible_plans(ctx: dict) -> dict[str, Any]:
    state = plan_state(ctx)
    return {
        "version": 2, "revision": state["revision"],
        "active": {scope: copy.deepcopy(state[scope]) for scope in ("turn", "stage")
                   if state.get(scope) and state[scope]["status"] == "active"},
        "closed": {scope: {"status": state[scope]["status"],
                           "assessment": state[scope]["assessment"]}
                   for scope in ("turn", "stage")
                   if state.get(scope) and state[scope]["status"] != "active"},
    }


def render_plans(snapshot: dict) -> str:
    return (
        "<game-plan>\n"
        "当前 BgPlan 记录快照，更新此前 BgPlan 条目的状态；仅 active 中的记录仍待执行。"
        "正文中未登记的目标和理由仍需结合最新局面判断，不能因本快照为空而视为取消。\n"
        + json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        + "\n</game-plan>"
    )


def create_plan_tool(ctx: dict) -> Tool:
    def call(args: dict) -> str:
        def reply(**fields: Any) -> str:
            return ToolCallResult(json.dumps(fields, ensure_ascii=False),
                                  is_error=fields.get("status") == "rejected")

        operation = args.get("operation")
        scope = args.get("scope")
        if operation not in {"read", "set", "complete", "abandon"}:
            return reply(status="rejected", error="Use read, set, complete or abandon.", stateChanged=False)
        if scope not in {None, "turn", "stage"} or (operation != "read" and scope is None):
            return reply(status="rejected", error="Choose scope turn or stage.", stateChanged=False)
        state = plan_state(ctx)
        if operation == "read":
            snapshot = visible_plans(ctx)
            if scope:
                snapshot["active"] = {k: v for k, v in snapshot["active"].items() if k == scope}
                snapshot["closed"] = {k: v for k, v in snapshot["closed"].items() if k == scope}
            return reply(status="ok", plans=snapshot, stateChanged=False)
        if ctx.get("_act_submitted"):
            return reply(status="rejected", error="PLAN_WRITE_AFTER_COMMIT", stateChanged=False)
        if set(args) - {"operation", "scope", "plan", "reason"}:
            return reply(status="rejected", error="Unknown plan fields.", stateChanged=False)
        if operation == "set":
            supplied = args.get("plan")
            allowed = {"goal", "next_step", "complete_when", "abandon_when", "unresolved"}
            required = {"goal", "next_step"}
            if scope == "stage":
                required |= {"complete_when", "abandon_when"}
            if (not isinstance(supplied, dict) or set(supplied) - allowed
                    or any(not isinstance(v, str) or not v.strip() for v in supplied.values())
                    or not required <= supplied.keys()):
                return reply(status="rejected", error="Plan needs goal and next_step; stage also needs complete_when and abandon_when. Values must be nonempty text.", stateChanged=False)
            content = {k: v.strip() for k, v in supplied.items()}
            if len(json.dumps(content, ensure_ascii=False).encode("utf-8")) > 6000:
                return reply(status="rejected", error="Plan exceeds 6000 UTF-8 bytes.", stateChanged=False)
            group = ctx.get("_turn_group_id")
            if scope == "turn" and not group:
                return reply(status="rejected", error="No authoritative action group.", stateChanged=False)
            current = state.get(scope)
            identity = {"turnGroupId": group} if scope == "turn" else {}
            if (current and current["status"] == "active" and current["plan"] == content
                    and all(current.get(k) == v for k, v in identity.items())):
                return reply(status="unchanged", scope=scope, revision=state["revision"], stateChanged=False)
            state["revision"] += 1
            state[scope] = {"status": "active", "revision": state["revision"], "plan": content, **identity}
        else:
            reason = args.get("reason")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
                return reply(status="rejected", error="Give a brief reason for closing the plan.", stateChanged=False)
            if not _close(state, scope, "completed" if operation == "complete" else "abandoned", reason.strip(), "model_assessment"):
                return reply(status="unchanged", scope=scope, revision=state["revision"], stateChanged=False)
        persist = ctx.get("_persist_state")
        if persist:
            persist()
        return reply(status="ok", scope=scope, planStatus=state[scope]["status"],
                     revision=state["revision"], stateChanged=True)

    return Tool(
        name="BgPlan",
        description="Optionally save a current-action plan or a cross-turn goal; complete or abandon it when finished. This never submits an action.",
        prompt=(
            "Use only when saving intent will help; simple actions need no plan. "
            "scope turn holds the current action's goal, next_step and optional unresolved question; "
            "it survives newly revealed information within that action and ends automatically at the action boundary. "
            "scope stage holds a goal across turns, next_step, observable complete_when and abandon_when conditions. "
            "Conditions are your assessment, not executable rules. Do not claim an uncommitted outcome has happened. "
            "Use complete when the goal is already achieved in the current Frame, or abandon when no longer worth pursuing. "
            "Closed plans stop being active. Update only for a material change; do not rewrite for routine progress. "
            "A saved choice remains provisional if Check or the latest Frame contradicts it. "
            "Do not copy the board or long calculations. Do not preserve Check IDs as future commitments."
        ),
        parameters={
            "type": "object", "additionalProperties": False,
            "properties": {
                "operation": {"type": "string", "enum": ["read", "set", "complete", "abandon"]},
                "scope": {"type": "string", "enum": ["turn", "stage"]},
                "plan": {
                    "type": "object", "additionalProperties": False,
                    "properties": {key: {"type": "string", "minLength": 1} for key in
                                   ("goal", "next_step", "complete_when", "abandon_when", "unresolved")},
                    "required": ["goal", "next_step"],
                },
                "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
            },
            "required": ["operation"],
        },
        call=call, is_read_only=False, always_load=True,
    )
