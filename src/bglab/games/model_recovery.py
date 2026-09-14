"""Decision-local model recovery; no action is selected by this policy."""
from __future__ import annotations

import copy
from typing import Any

from bglab.engine.query_profiles import RequestRecoveryDecision

NORMAL_REQUEST_ALLOWANCE = 14
RECOVERY_REQUEST_ALLOWANCE = 6


def restore_recovery(value: Any) -> dict:
    if value is None:
        return {"version": 1, "requests": 0, "recoveryRequests": 0,
                "reason": "", "exhausted": False}
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("invalid model recovery record")
    for key in ("requests", "recoveryRequests"):
        count = value.get(key)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("invalid model recovery counter")
    if (value["recoveryRequests"] > RECOVERY_REQUEST_ALLOWANCE
            or value["requests"] < value["recoveryRequests"]
            or not isinstance(value.get("reason"), str)
            or not isinstance(value.get("exhausted"), bool)):
        raise ValueError("invalid model recovery state")
    excluded = value.get("excludedCheckIds", [])
    if (not isinstance(excluded, list)
            or any(not isinstance(item, str) or not item for item in excluded)
            or len(excluded) != len(set(excluded))):
        raise ValueError("invalid recovery Check projection")
    return copy.deepcopy(value)


def recovery_state(deps: Any) -> dict:
    value = getattr(deps, "_game_model_recovery", None)
    if value is None:
        value = restore_recovery(None)
        deps._game_model_recovery = value
    return value


def model_recovery_enabled(deps: Any) -> bool:
    profile = getattr(getattr(deps, "query_profile", None), "recovery", None)
    return callable(getattr(profile, "prepare_request", None))


def activate_recovery(deps: Any, reason: str) -> None:
    recovery_state(deps)["reason"] = reason


def project_game_recovery_messages(deps: Any, messages: list[dict]) -> list[dict]:
    """Once per decision, stop replaying a chain of unproductive Check attempts.

    Only complete, read-only BgAct Check pairs from the current Frame may be
    excluded. Previous turns, user input, Skills, chat and Commit calls remain.
    New Check results after this projection remain visible for model Commit.
    The canonical history, bindings and shared recovery allowance are unchanged.
    """
    ctx = getattr(deps, "_game_tool_ctx", {})
    record = recovery_state(deps)
    if (not record["reason"] or record["recoveryRequests"] < 3
            or ctx.get("_act_submitted")
            or ctx.get("_semantic_reconciliation_required")):
        return messages
    from bglab.games.compaction_profile import _is_authoritative_game_input

    start = next((i for i in range(len(messages) - 1, -1, -1)
                  if _is_authoritative_game_input(messages[i])), None)
    if start is None:
        return messages
    suffix = messages[start + 1:]
    completed = {m.get("tool_use_id") for m in suffix
                 if m.get("type") == "tool_result"}
    checks = []
    for message in suffix:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        calls = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if calls and all(b.get("name") == "BgAct"
                         and isinstance(b.get("input"), dict)
                         and b["input"].get("operation") == "check"
                         and isinstance(b.get("id"), str) and b["id"]
                         and b.get("id") in completed for b in calls):
            checks.append((message, [b["id"] for b in calls]))
    if "excludedCheckIds" not in record:
        if len(checks) < 2:
            return messages
        record["excludedCheckIds"] = list(dict.fromkeys(
            call_id for _, ids in checks for call_id in ids
        ))
        persist = getattr(deps, "_game_model_recovery_persist", None)
        if callable(persist):
            persist()
    excluded = set(record["excludedCheckIds"])
    omitted_messages = {id(m) for m, ids in checks if set(ids).issubset(excluded)}
    # Do not orphan a result if compaction/restore no longer retained its call.
    omitted_ids = {call_id for m, ids in checks if id(m) in omitted_messages for call_id in ids}
    return [*messages[:start + 1], *[
        m for m in suffix
        if id(m) not in omitted_messages
        and not (m.get("type") == "tool_result" and m.get("tool_use_id") in omitted_ids)
    ]]


def prepare_game_request(deps: Any, state: Any, *, disable_thinking: bool) -> RequestRecoveryDecision:
    """Reserve before dispatch; mixed failure paths spend the same allowance.

    A failed output starts recovery for the remaining decision. A successful
    Check provides facts, but is not a committed action and cannot re-enable
    the generation mode that just exhausted its budget. The decision identity
    owns the ledger; tool success and compaction never refill it.
    """
    ctx = getattr(deps, '_game_tool_ctx', {})
    if ctx.get('_chat_enabled') and ctx.get('_act_submitted'):
        from bglab.games.chat import CHAT_REPLY_REQUEST_ALLOWANCE, pending_reply_ids, finish_unanswered

        if pending_reply_ids(ctx):
            used = ctx.get('_chat_reply_requests', 0)
            if used >= CHAT_REPLY_REQUEST_ALLOWANCE:
                finish_unanswered(ctx)
                return RequestRecoveryDecision(stop_reason='chat_reply_unavailable')
            ctx['_chat_reply_requests'] = used + 1
            return RequestRecoveryDecision(tool_name='BgChat', disable_thinking=disable_thinking)
    record = recovery_state(deps)
    output_attempt = state.max_output_tokens_recovery_count
    if output_attempt and not record["reason"]:
        activate_recovery(deps, "generation_failure")
    if record["requests"] >= NORMAL_REQUEST_ALLOWANCE and not record["reason"]:
        activate_recovery(deps, "decision_still_uncommitted")
    forced = bool(output_attempt or record["reason"])
    if record["exhausted"] or (forced and record["recoveryRequests"] >= RECOVERY_REQUEST_ALLOWANCE):
        record["exhausted"] = True
        decision = RequestRecoveryDecision(stop_reason="model_nonconvergence")
    else:
        record["requests"] += 1
        if forced:
            record["recoveryRequests"] += 1
        decision = RequestRecoveryDecision(
            tool_name="BgAct" if forced else None,
            disable_thinking=bool(forced and disable_thinking),
            # Greedy non-thinking recovery can reproduce the same invalid
            # tool arguments despite new feedback. Sample only on this
            # explicitly supported recovery path; normal play is unchanged.
            temperature=0.7 if forced and disable_thinking else None,
        )
    persist = getattr(deps, "_game_model_recovery_persist", None)
    if callable(persist):
        persist()
    return decision
