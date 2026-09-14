from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Callable, Mapping

from .checked_route_fact import CheckedRouteFact, thaw
from .closest import ClosestCandidate, ClosestSearchResult, SemanticEditKind
from .compiler import AuthorityCompilation, CompilationStatus
from .descriptor import SemanticDescriptor, _project_rule
from .divergence import DivergenceCode, first_divergence
from .model import SemanticAction, SemanticChain
from .outcome import render_public_outcome
from .schema import strict_json_equal


DISCLAIMER = "校验完成。候选按语义接近度排列，不代表策略优劣。"
UNCOMMITTED_STATUS = "当前行动尚未提交。"
_REASON_TEXT = {
    "SEMANTIC_MAPPING_MISSING": "当前语义动作缺少可用的引擎映射。",
    "COMPILATION_BUDGET_EXHAUSTED": "只读编译预算已耗尽，不能证明该路线完整。",
    "MANDATORY_CHOICE_OMITTED": "路线遗漏了当前必须由玩家选择的分支。",
    "SUBMITTED_ACTION_ILLEGAL": "提交的下一语义动作不在当前合法前沿。",
    "DECISION_NOT_COMPLETE": "路线尚未完成当前 DecisionFrame。",
}


def _render_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_semantic_action(
    action: SemanticAction,
    *,
    choice_value_labels: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    arguments = ", ".join(
        f"{key}={_render_value(value)}" for key, value in sorted(action.args)
    )
    rendered = f"{action.action}({arguments})" if arguments else action.action
    values = dict(action.args)
    kind = values.get("kind")
    choice = values.get("choice")
    label = (
        choice_value_labels.get(str(kind), {}).get(str(choice))
        if choice_value_labels is not None and kind is not None and choice is not None
        else None
    )
    return f"{rendered} [含义={label}]" if label else rendered


def render_semantic_chain(
    chain: SemanticChain,
    *,
    choice_value_labels: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    return " -> ".join(
        render_semantic_action(
            action,
            choice_value_labels=choice_value_labels,
        )
        for action in chain.actions
    )


def _render_compact_semantic_action(
    action: SemanticAction,
    *,
    choice_value_labels: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    values = dict(action.args)
    kind = values.get("kind")
    choice = values.get("choice")
    if (
        action.action == "choose_reward"
        and set(values) == {"kind", "choice"}
        and isinstance(kind, str)
    ):
        label = (
            choice_value_labels.get(kind, {}).get(str(choice))
            if choice_value_labels is not None
            else None
        )
        suffix = f":{label}" if label else ""
        return f"choose_reward({kind}={_render_value(choice)}{suffix})"
    arguments = ",".join(
        f"{key}={_render_value(value)}" for key, value in sorted(action.args)
    )
    return f"{action.action}({arguments})" if arguments else action.action


def _render_compact_semantic_chain(
    chain: SemanticChain,
    *,
    choice_value_labels: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    return " → ".join(
        _render_compact_semantic_action(
            action,
            choice_value_labels=choice_value_labels,
        )
        for action in chain.actions
    )


_CHECKED_ROUTE_STATUS_ZH = {
    "exact": "完全一致",
    "authority-completed": "权威自动补全",
    "needs-choice": "完整候选路线（含补出的玩家选择，需自行确认）",
    "distant": "较远参考",
    "nearby": "同根近似路线",
}
_CHECKED_ROUTE_REASON_ZH = {
    "PLAYER_CHOICE_REQUIRED": "当前路线还需要玩家完成一个选择。",
    "SUBMITTED_ROUTE_DIVERGED": "直接编译路径未完整证明本次提交。",
    "ROUTE_INCOMPLETE": "当前路线尚未完成本次决策。",
    "AUTHORITY_CHECK_INCONCLUSIVE": "本次只读权威校验尚未得出完整结论。",
    "AUTHORITY_CHECK_UNAVAILABLE": "当前语义路线缺少可用的权威映射。",
}


def _checked_route_scope(facts: Sequence[CheckedRouteFact]) -> str:
    """State what the returned facts establish, without inferring legality causes."""
    if not facts:
        return DISCLAIMER
    if not any(fact.intent_exact for fact in facts):
        return "原提交的意图没有被下列候选完整保留。以下是改变了原选择的完整合法路线；差异逐项列出，不代表建议你采用。"
    if any(fact.status == "needs-choice" and fact.intent_exact for fact in facts):
        return "已返回保留原意图的完整合法路线，其中补出的玩家选择需要你确认。其他候选的修正另列；顺序不代表策略优劣。"
    return "已返回与原意图一致的完整合法路线。其他候选如有修正会另列；顺序不代表策略优劣。"


def _fact_chain(value: Mapping[str, Any]) -> SemanticChain:
    actions = value.get("actions")
    if not isinstance(actions, (list, tuple)):
        raise ValueError("checked-route semanticChain actions must be an array")
    normalized: list[SemanticAction] = []
    for action in actions:
        if not isinstance(action, Mapping) or not isinstance(action.get("action"), str):
            raise ValueError("checked-route semanticChain action is invalid")
        args = action.get("args", {})
        if not isinstance(args, Mapping):
            raise ValueError("checked-route semanticChain args must be an object")
        normalized.append(SemanticAction.from_mapping(str(action["action"]), args))
    name = value.get("name", "")
    return SemanticChain(
        name=str(name) if isinstance(name, str) else "",
        actions=tuple(normalized),
    )


def render_intent_change_summary(
    fact: CheckedRouteFact,
    choice_value_labels: Mapping[str, Mapping[str, str]] | None = None,
) -> str | None:
    """Describe every visible edit; do not infer strategic value or game effects."""
    delta = fact.intent_changes
    if delta is None:
        return None
    parts = [f"按原顺序对齐：保留{len(delta['preserved'])}/{delta['submittedCount']}步"]
    for change in delta["changes"]:
        def action_text(side: str) -> str:
            return _render_compact_semantic_action(
                _fact_chain({"actions": [change[side]]}).actions[0],
                choice_value_labels=choice_value_labels,
            )
        kind = change["kind"]
        absent = (
            "（实际链未包含该动作）"
            if change["submitted"] is not None and not any(
                strict_json_equal(thaw(change["submitted"]), action)
                for action in fact.semantic_chain["actions"]
            ) else ""
        )
        if kind == SemanticEditKind.DELETE:
            parts.append(f"重要修正：删除原第{change['submittedIndex']}步 {action_text('submitted')}{absent}")
        elif kind == SemanticEditKind.ACTION_CHANGE:
            parts.append(
                f"重要修正：替换原第{change['submittedIndex']}步 {action_text('submitted')}{absent}，"
                f"实际第{change['candidateIndex']}步改为 {action_text('candidate')}"
            )
        elif kind == SemanticEditKind.ARGUMENT_CHANGE:
            parts.append(
                f"字段修正：原第{change['submittedIndex']}步 {action_text('submitted')}"
                f" → {action_text('candidate')}"
            )
        else:
            suffix = (
                "（本完整链内，此后无额外玩家动作）"
                if change["candidateIndex"] == len(fact.semantic_chain["actions"])
                else ""
            )
            parts.append(f"候选补全：实际第{change['candidateIndex']}步新增 {action_text('candidate')}{suffix}")
    return "；".join(parts) + "。"


def _intent_change_lines(
    fact: CheckedRouteFact,
    choice_value_labels: Mapping[str, Mapping[str, str]] | None,
    source_facts: Mapping[str, Sequence[tuple[int, CheckedRouteFact]]] | None,
) -> list[str]:
    sources = source_facts.get(fact.label, ()) if source_facts is not None else ()
    if len(sources) > 1:
        return [
            f"输入路线 {index}（{_CHECKED_ROUTE_STATUS_ZH[source.status]}）："
            + (render_intent_change_summary(source, choice_value_labels)
               or _fact_difference(source, choice_value_labels))
            for index, source in sources
        ]
    summary = render_intent_change_summary(fact, choice_value_labels)
    return [summary] if summary else []


def _fact_difference(
    fact: CheckedRouteFact,
    choice_value_labels: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    divergence = fact.first_divergence
    if divergence is None:
        return "差异：无；提交语义与合法路线一致。"
    code = str(divergence.get("code", ""))
    if code == DivergenceCode.EXACT.value:
        return "差异：无；提交语义与合法路线一致。"
    submitted = divergence.get("submittedNext")
    candidate = divergence.get("candidateNext")
    submitted_text = (
        render_semantic_action(
            _fact_chain({"actions": [submitted]}).actions[0],
            choice_value_labels=choice_value_labels,
        )
        if isinstance(submitted, Mapping)
        else "结束"
    )
    candidate_text = (
        render_semantic_action(
            _fact_chain({"actions": [candidate]}).actions[0],
            choice_value_labels=choice_value_labels,
        )
        if isinstance(candidate, Mapping)
        else "结束"
    )
    if code == DivergenceCode.CANDIDATE_INSERTED_ACTION.value:
        return (
            f"差异：保留前 {fact.matched_prefix} 个动作；"
            f"该候选随后选择 {candidate_text}。"
            "这只是一个可提交分支，不代表该选择必须执行或更优。"
        )
    if code == DivergenceCode.SUBMITTED_ACTION_OMITTED.value:
        return (
            f"差异：保留前 {fact.matched_prefix} 个动作；"
            f"提交路线额外包含 {submitted_text}。"
        )
    return (
        f"差异：保留前 {fact.matched_prefix} 个动作；"
        f"提交路线下一步为 {submitted_text}，合法路线下一步为 {candidate_text}。"
    )


def _fact_authority_boundary(fact: CheckedRouteFact) -> str | None:
    divergence = fact.first_divergence
    if not isinstance(divergence, Mapping):
        return None
    reason = divergence.get("authorityReason")
    if not isinstance(reason, Mapping):
        return None
    code = reason.get("code")
    if code == "SUBMITTED_ROUTE_DIVERGED" and fact.status == "exact":
        return (
            "权威说明：直接编译路径未完整证明该路线，"
            f"但 {fact.label} 已由完整权威路线确认且与提交语义完全一致；"
            f"以 {fact.label} 的路线和结果为准。"
        )
    text = _CHECKED_ROUTE_REASON_ZH.get(str(code))
    if text is None:
        return None
    if fact.status in {"nearby", "distant"}:
        return (
            f"权威说明：{text}这只描述原提交；"
            f"{fact.label} 本身是完整合法路线，可直接依据本轮短 ID 提交。"
        )
    return f"权威边界：{text}"


def render_checked_route_facts(
    facts: Sequence[CheckedRouteFact],
    *,
    locale: str = "zh-CN",
    outcome_renderer: Callable[[Mapping[str, object]], str] | None = None,
    choice_value_labels: Mapping[str, Mapping[str, str]] | None = None,
    source_facts: Mapping[str, Sequence[tuple[int, CheckedRouteFact]]] | None = None,
) -> str:
    """Render immutable checked-route facts without re-deriving their fields."""

    if locale != "zh-CN":
        raise ValueError("unsupported checked-route locale")
    if any(not isinstance(fact, CheckedRouteFact) for fact in facts):
        raise TypeError("checked-route renderer accepts CheckedRouteFact values only")
    render_outcome = outcome_renderer or render_public_outcome
    sections: list[str] = []
    for fact in facts:
        status = _CHECKED_ROUTE_STATUS_ZH.get(fact.status, fact.status)
        outcome = (
            render_outcome(fact.outcome) if outcome_renderer is not None
            else fact.public_summary if fact.public_summary is not None
            else render_public_outcome(fact.outcome)
        )
        if not isinstance(outcome, str):
            raise ValueError("outcome renderer must return text")
        lines = [
            f"[{fact.label}] {status}",
            "路线：" + render_semantic_chain(
                _fact_chain(fact.semantic_chain),
                choice_value_labels=choice_value_labels,
            ),
            *(_intent_change_lines(fact, choice_value_labels, source_facts)
              or [_fact_difference(fact, choice_value_labels)]),
        ]
        boundary = _fact_authority_boundary(fact)
        if boundary is not None:
            lines.append(boundary)
        lines.append(f"结果：{' '.join(outcome.split())[:600] or '无公开变化。'}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def render_compact_checked_route_facts(
    facts: Sequence[CheckedRouteFact],
    *,
    locale: str = "zh-CN",
    outcome_renderer: Callable[[Mapping[str, object]], str] | None = None,
    choice_value_labels: Mapping[str, Mapping[str, str]] | None = None,
    source_facts: Mapping[str, Sequence[tuple[int, CheckedRouteFact]]] | None = None,
) -> str:
    """Render complete checked routes for the model without duplicating audit facts."""

    if locale != "zh-CN":
        raise ValueError("unsupported checked-route locale")
    if any(not isinstance(fact, CheckedRouteFact) for fact in facts):
        raise TypeError("checked-route renderer accepts CheckedRouteFact values only")
    render_outcome = outcome_renderer or render_public_outcome
    sections = [_checked_route_scope(facts)]
    for fact in facts:
        status = _CHECKED_ROUTE_STATUS_ZH.get(fact.status, fact.status)
        prefix = (
            f"；前{fact.matched_prefix}步一致"
            if fact.first_divergence is not None
            else ""
        )
        lines = [
            f"[{fact.label}] {status}{prefix}",
            "路线：" + _render_compact_semantic_chain(
                _fact_chain(fact.semantic_chain),
                choice_value_labels=choice_value_labels,
            ),
        ]
        divergence = fact.first_divergence
        intent_lines = _intent_change_lines(fact, choice_value_labels, source_facts)
        if intent_lines:
            lines.extend(intent_lines)
        elif isinstance(divergence, Mapping):
            divergence_code = str(divergence.get("code", ""))
            submitted = divergence.get("submittedNext")
            candidate = divergence.get("candidateNext")
            submitted_text = (
                _render_compact_semantic_action(
                    _fact_chain({"actions": [submitted]}).actions[0],
                    choice_value_labels=choice_value_labels,
                )
                if isinstance(submitted, Mapping)
                else "结束"
            )
            candidate_text = (
                _render_compact_semantic_action(
                    _fact_chain({"actions": [candidate]}).actions[0],
                    choice_value_labels=choice_value_labels,
                )
                if isinstance(candidate, Mapping)
                else "结束"
            )
            if divergence_code == DivergenceCode.CANDIDATE_INSERTED_ACTION.value:
                lines.append(
                    f"候选补全：前{fact.matched_prefix}步一致；该候选随后选择 {candidate_text}。"
                    "这只是一个可提交分支，不代表该选择必须执行或更优。"
                )
            elif divergence_code == DivergenceCode.SUBMITTED_ACTION_OMITTED.value:
                lines.append(
                    f"重要修正：原输入要求 {submitted_text}，但合法路线不执行这一步；"
                    "候选也不会获得该动作预期的选择或收益。"
                )
            elif (
                divergence_code == DivergenceCode.ACTION_CHANGED.value
                and candidate_text == "finish_action"
            ):
                lines.append(
                    f"重要修正：原输入要求 {submitted_text}，但当前路线没有这个选择并在此结束；"
                    "候选不会获得该选择预期的收益。"
                )
            elif divergence_code == DivergenceCode.ARGUMENT_CHANGED.value:
                lines.append(
                    f"字段修正：原输入 {submitted_text}；合法路线使用 {candidate_text}。"
                )
            else:
                lines.append(
                    f"路线修正：保留前{fact.matched_prefix}步；原输入 {submitted_text}；"
                    f"合法路线改为 {candidate_text}。"
                )
        outcome = (
            render_outcome(fact.outcome) if outcome_renderer is not None
            else fact.public_summary if fact.public_summary is not None
            else render_public_outcome(fact.outcome)
        )
        if not isinstance(outcome, str):
            raise ValueError("outcome renderer must return text")
        if fact.outcome.get("unexecutedActionAbandoned") is True:
            lines.append(
                "重要：finish_action 会立即放弃本路线已解锁但未执行的行动；"
                "下方收益不包含该行动。"
            )
        lines.append(f"收益：{' '.join(outcome.split())[:600] or '无公开变化。'}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _model_owned_chain(
    chain: SemanticChain,
    action_roles: Mapping[str, str],
) -> SemanticChain:
    return SemanticChain(
        name=chain.name,
        actions=tuple(
            action
            for action in chain.actions
            if action_roles.get(action.action, "intent") != "derived"
        ),
    )


def _semantic_frontier_hints(
    compilation: AuthorityCompilation,
    descriptor: SemanticDescriptor,
) -> tuple[str, ...]:
    hints: list[str] = []
    mode_actions = {
        "recruit":"recruit_courtier",
        "promote":"promote_courtier",
        "gardener":"deploy_gardener",
        "warrior":"deploy_warrior",
    }
    for frozen_step in compilation.legal_frontier:
        step = dict(frozen_step)
        effect_types = (
            {0:str(step["choiceType"])}
            if isinstance(step.get("choiceType"), str)
            else {}
        )
        projected = next(
            (
                (rule, result)
                for rule in descriptor.rules
                if (result := _project_rule([step], 0, rule, effect_types))
                is not None
                and result[0] == 1
                and descriptor.action_roles.get(rule.semantic_action) != "derived"
            ),
            None,
        )
        if projected is not None:
            rule, (_, arguments) = projected
            try:
                rendered = render_semantic_action(
                    SemanticAction.from_mapping(rule.semantic_action, arguments),
                )
            except ValueError:
                rendered = rule.semantic_action
            if rendered not in hints:
                hints.append(rendered)
            continue
        if step.get("op") == "beginMajorAction":
            action = mode_actions.get(str(step.get("mode", "")))
            if action and action not in hints:
                hints.append(f"{action}(补全当前目标参数)")
    return tuple(hints)


def render_authority_diagnostic(
    compilation: AuthorityCompilation | None,
    descriptor: SemanticDescriptor | None = None,
) -> str:
    if compilation is None:
        return ""
    reason = compilation.authority_reason
    reason_code = str(reason.get("code", "")) if isinstance(reason, Mapping) else ""
    if (
        compilation.status is CompilationStatus.COMPLETE
        and not reason_code
        and compilation.first_unmatched_action is None
        and not compilation.legal_frontier
    ):
        return ""
    lines = [
        "权威诊断：" + _REASON_TEXT.get(
            reason_code,
            "当前路线未能完整证明。",
        ),
    ]
    if compilation.first_unmatched_action is not None:
        lines.append(
            "首个未匹配语义动作："
            + render_semantic_action(compilation.first_unmatched_action),
        )
    if compilation.validated_prefix_summary is not None:
        lines.extend([
            "已验证前缀及引擎自动推进后的公开结果（尚未提交；"
            "未包含被拒绝动作及其奖励）：" + compilation.validated_prefix_summary,
            "已验证的前缀合法，不代表你想要的后续行动仍然可行。"
            "若此前选择造成资源不足，修改后面的奖励选项无法解决；"
            "请回看并改变造成不足的较早选择，再检查新的完整路线。",
        ])
    if compilation.legal_frontier:
        hints = (
            _semantic_frontier_hints(compilation, descriptor)
            if descriptor is not None
            else ()
        )
        if hints:
            lines.append("当前可追加的语义动作：" + "；".join(hints) + "。")
        else:
            lines.append(
                "当前权威仍有后续分支；"
                "引擎步骤保持内部，请从首个未匹配语义动作修改动作或参数。"
            )
    return "\n".join(lines)


def _candidate_rejection_recovery(
    result: ClosestSearchResult,
    action_roles: Mapping[str, str],
) -> str:
    primary = result.candidates[0]
    visible = _model_owned_chain(primary.program.chain, action_roles)
    divergence = first_divergence(result.submitted, visible)
    if divergence.code is DivergenceCode.EXACT:
        boundary = (
            "C1 已完全复现本次提交；若权威结果不是你想要的，"
            "必须改变根行动或某个语义动作/参数。"
        )
    else:
        boundary = (
            f"最接近路线 C1 保留前 {divergence.matched_prefix} 个语义动作；"
            "以其上方列出的首个分叉为修改起点。"
        )
    return (
        "候选全部不采用时：不要 commit，也不要原样重发。"
        + boundary
        + "重新生成分叉后的完整后缀；需要再次核验时用 operation=check，"
        "已经决定并写出完整路线时也可直接 Commit。"
    )


def _checked_route_rejection_recovery(
    facts: Sequence[CheckedRouteFact],
) -> str:
    primary = facts[0]
    divergence = primary.first_divergence
    if divergence is None or divergence.get("code") == DivergenceCode.EXACT.value:
        boundary = (
            f"{primary.label} 已完全复现本次提交；若权威结果不是你想要的，"
            "必须改变根行动或某个语义动作/参数。"
        )
    else:
        boundary = (
            f"最接近路线 {primary.label} 保留前 {primary.matched_prefix} 个语义动作；"
            "以其上方列出的首个分叉为修改起点。"
        )
    return (
        "候选全部不采用时：不要 commit，也不要原样重发。"
        + boundary
        + "重新生成分叉后的完整后缀；需要再次核验时用 operation=check，"
        "已经决定并写出完整路线时也可直接 Commit。"
    )


def render_no_candidate_recovery(
    result: ClosestSearchResult,
    compilation: AuthorityCompilation | None,
    *,
    coverage_status: str | None,
    enumeration_complete: bool | None,
    search_mode: str | None,
    descriptor: SemanticDescriptor | None = None,
) -> str:
    del result
    inconclusive = (
        enumeration_complete is not True
        or coverage_status in {None, "unknown", "bounded", "not_explored"}
        or (
            compilation is not None
            and compilation.status is CompilationStatus.BUDGET_EXHAUSTED
        )
    )
    code = "CHECK_INCONCLUSIVE" if inconclusive else "NO_LEGAL_MATCH"
    lines = [
        DISCLAIMER,
        "",
        f"恢复结论：{code}；当前没有可提交候选，不要 commit。",
    ]
    diagnostic = render_authority_diagnostic(compilation, descriptor)
    if diagnostic:
        lines.extend(["", diagnostic])
    if inconclusive:
        lines.extend(
            [
                "",
                "检索覆盖尚未穷尽；这不等于你的路线被证明不存在。",
            ]
        )
    if search_mode in {"submitted_prefix_incomplete", "locked_prefix_no_match"}:
        recovery = (
            "修改方法：已提交前缀全部合法，但它在当前玩家选择完成前停止。"
            "保留全部已提交动作，并根据最新 DecisionFrame 追加尚未表达的分支、目标和"
            "其他玩家选择，直到当前行动边界；不要改成全局最短的另一条根路线。"
        )
    else:
        recovery = (
            "修改方法：保留首个未匹配动作之前已经确认的语义选择；"
            "从首个未匹配动作开始修改动作或参数，并重新生成其后的完整后缀。"
            "若当前合法前沿仍不符合你的策略意图，可以改变更早动作或整条根路线。"
        )
    lines.extend(
        [
            "",
            UNCOMMITTED_STATUS,
            recovery,
            "重新调用：需要核验修改后路线时使用 operation=check；"
            "已经决定并写出完整路线时也可直接 Commit。不要原样重发本次输入，"
            "也不要复制结果字段或机械步骤。",
        ]
    )
    return "\n".join(lines)


def _candidate_heading(
    candidate: ClosestCandidate,
    action_roles: Mapping[str, str],
) -> str:
    if candidate.alignment.total_cost == 0:
        return "完全一致"
    if candidate.alignment.edits and all(
        edit.kind is SemanticEditKind.INSERT
        for edit in candidate.alignment.edits
    ):
        inserted_roles = [
            action_roles.get(edit.candidate.action, "intent")
            for edit in candidate.alignment.edits
            if edit.candidate is not None
        ]
        if inserted_roles and all(role == "derived" for role in inserted_roles):
            return "权威自动补全"
        if inserted_roles and all(role in {"derived", "choice"} for role in inserted_roles):
            return "完整候选路线（含补出的玩家选择，需自行确认）"
    if candidate.alignment.root_changed:
        return "较远参考"
    return "同根近似路线"


def _difference_text(
    result: ClosestSearchResult,
    candidate: ClosestCandidate,
    action_roles: Mapping[str, str],
) -> str:
    if candidate.alignment.edits and all(
        edit.kind is SemanticEditKind.INSERT
        and edit.candidate is not None
        and action_roles.get(edit.candidate.action, "intent") == "derived"
        for edit in candidate.alignment.edits
    ):
        return (
            "差异：仅权威自动补全派生步骤；"
            "提交时省略权威自动补全的派生步骤。"
        )
    if (
        candidate.alignment.edits
        and all(
            edit.kind is SemanticEditKind.INSERT
            and edit.candidate is not None
            and action_roles.get(edit.candidate.action, "intent") in {"derived", "choice"}
            for edit in candidate.alignment.edits
        )
        and any(
            edit.candidate is not None
            and action_roles.get(edit.candidate.action, "intent") == "choice"
            for edit in candidate.alignment.edits
        )
    ):
        divergence = first_divergence(
            result.submitted,
            _model_owned_chain(candidate.program.chain, action_roles),
        )
        candidate_next = (
            render_semantic_action(divergence.candidate_next)
            if divergence.candidate_next is not None
            else "无"
        )
        return (
            f"保留：前 {divergence.matched_prefix} 个语义动作。\n"
            f"候选补全：该候选随后选择 {candidate_next}；"
            "这只是一个可提交分支，不代表该选择必须执行或更优。"
        )
    divergence = first_divergence(
        result.submitted,
        _model_owned_chain(candidate.program.chain, action_roles),
    )
    if divergence.code is DivergenceCode.EXACT:
        return "差异：无；提交语义与合法路线一致。"
    submitted_next = (
        render_semantic_action(divergence.submitted_next)
        if divergence.submitted_next is not None
        else "无"
    )
    candidate_next = (
        render_semantic_action(divergence.candidate_next)
        if divergence.candidate_next is not None
        else "无"
    )
    return (
        f"保留：前 {divergence.matched_prefix} 个语义动作。\n"
        f"差异：首个分叉={divergence.code.value}；"
        f"提交下一步={submitted_next}；候选下一步={candidate_next}。"
    )


def render_closest_result(
    result: ClosestSearchResult,
    *,
    outcome_renderer: Callable[[Mapping[str, object]], str] | None = None,
    action_roles: Mapping[str, str] | None = None,
    checked_routes: Sequence[CheckedRouteFact] | None = None,
    choice_value_labels: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    roles = action_roles or {}
    lines = [DISCLAIMER]
    if not result.candidates:
        lines.extend(
            [
                "",
                "未找到可返回的合法完整路线；没有生成占位候选。",
                "",
                UNCOMMITTED_STATUS,
                "下一步：请形成一条实质不同的完整 chains；需要校验时使用 Check，"
                "已经决定并写出完整路线时也可直接 Commit。只回复文字不会提交。",
            ]
        )
        return "\n".join(lines)

    if checked_routes is not None:
        if len(checked_routes) != len(result.candidates):
            raise ValueError("checked-route facts must match Authority candidates")
        expected_labels = tuple(candidate.label for candidate in result.candidates)
        if tuple(fact.label for fact in checked_routes) != expected_labels:
            raise ValueError("checked-route labels drifted from Authority candidates")
        lines[0] = _checked_route_scope(checked_routes)
        lines.extend(
            [
                "",
                render_checked_route_facts(
                    checked_routes,
                    outcome_renderer=outcome_renderer,
                    choice_value_labels=choice_value_labels,
                ),
            ]
        )
    else:
        for candidate in result.candidates:
            visible_chain = _model_owned_chain(candidate.program.chain, roles)
            lines.extend(
                [
                    "",
                    f"[{candidate.label}] {_candidate_heading(candidate, roles)}",
                    "路线：" + render_semantic_chain(
                        visible_chain,
                        choice_value_labels=choice_value_labels,
                    ),
                    _difference_text(result, candidate, roles),
                ]
            )
            if outcome_renderer is None:
                lines.append("结果：权威结果已绑定，未提供公开结果摘要。")
            else:
                outcome = outcome_renderer(candidate.program.outcome)
                if not isinstance(outcome, str):
                    raise ValueError("outcome renderer must return text")
                compact = " ".join(outcome.split())[:600]
                lines.append(f"结果：{compact or '无公开变化。'}")
    lines.extend(["", UNCOMMITTED_STATUS])
    return "\n".join(lines)
