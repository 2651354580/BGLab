"""Stable model-visible BgAct errors separated from internal diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class PublicBgActError:
    code: str
    message: str
    next_action: str
    detail: str = ""

    def render(self) -> str:
        parts = [f"{self.code}: {self.message}"]
        if self.detail:
            parts.append(self.detail)
        if self.next_action:
            parts.append(f"下一步：{self.next_action}")
        return "\n".join(parts)


_ILLEGAL_ROUTE_CODES = frozenset({
    "NO_SEMANTIC_CANDIDATES",
    "SEMANTIC_CHAIN_NOT_EXACT",
    "BATCH_CHECK_FAILED",
})
_STALE_FRAME_CODES = frozenset({
    "SEMANTIC_IDENTITY_UNAVAILABLE",
    "STALE_SEMANTIC_BINDING",
})
_SEMANTIC_RECHECK_CODES = frozenset({
    "SEMANTIC_VALIDATION_REQUIRED",
    "UNKNOWN_SEMANTIC_CANDIDATE",
})
_CHECK_UNAVAILABLE_CODES = frozenset({
    "AUTHORITY_WORKER_FAILURE",
    "SEMANTIC_WORKER_UNAVAILABLE",
    "SEMANTIC_VALIDATION_FAILED",
    "SEMANTIC_CHECK_UNAVAILABLE",
})
_UNCERTAIN_CODES = frozenset({
    "COMMIT_OUTCOME_INDETERMINATE",
    "COMMIT_CONFIRMATION_REQUIRED",
    "SINK_UNAVAILABLE",
})


def _safe_detail(code: str, detail: str) -> str:
    compact = " ".join(str(detail).split())
    if code == "INVALID_SEMANTIC_INPUT":
        first = str(detail).splitlines()[0].strip()
        if first.startswith(("字段 ", "参数")):
            return first[:300]
        return ""
    if code != "DERIVED_SEMANTIC_ACTION":
        return ""
    compact = re.sub(r"由引擎[^；。]*[；。]?", "", compact)
    compact = compact.replace("引擎", "")
    return compact[:300]


def render_public_bgact_error(
    internal_code: str,
    internal_detail: str = "",
) -> PublicBgActError:
    code = str(internal_code or "BG_ACT_FAILED")
    if code == "UNKNOWN_ROUTE_ID":
        return PublicBgActError(
            code=code,
            message="这个编号不属于当前可提交路线，尚未执行任何行动。",
            next_action="采用当前局面已核验的路线时，填写 Check 返回的数字编号；局面已改变或路线已修改时，重新 Check 后再选择，不要猜编号。",
        )
    if code in {"DIRECT_CHAIN_REQUIRES_CHECK", "CHECK_REQUIRED"}:
        return PublicBgActError(
            code="CHECK_REQUIRED",
            message="当前提交缺少有效的已核验路线，尚未执行任何行动。",
            next_action=(
                "用 operation=check 核验当前意图；根据返回结果重新分析，符合意图和预期效果后提交对应数字编号。"
                "若都不满意，调整关键选择并 Check 其他路线。不要复用上一局面的编号。"
            ),
        )
    if code == "INVALID_SEMANTIC_OPERATION":
        return PublicBgActError(
            code="INVALID_ARGUMENT",
            message="参数不符合当前行动格式。",
            next_action=(
                "按当前 Tool schema 修正 operation 及其对应字段后再调用；"
                "Check 使用待校验 chains，Commit 使用本轮数字编号或一条已经决定执行的"
                "完整 chains。只修正包装，不改变原动作与顺序。"
            ),
        )
    if code == "INVALID_SEMANTIC_INPUT":
        return PublicBgActError(
            code="INVALID_ARGUMENT",
            message="参数不符合当前行动字段约束。",
            detail=_safe_detail(code, internal_detail),
            next_action=(
                "按上方字段约束和当前 Tool schema 修正该字段；保留其他仍合法的"
                "动作与顺序，但不要原样重发。"
            ),
        )
    if code == "DERIVED_SEMANTIC_ACTION":
        return PublicBgActError(
            code="INVALID_ARGUMENT",
            message="参数包含不应由模型提交的自动推导动作。",
            detail=_safe_detail(code, internal_detail),
            next_action=(
                "省略该派生动作，只保留模型拥有的语义选择；仍不确定时重新 Check，"
                "已经决定并写出完整路线时也可直接 Commit。"
            ),
        )
    if code in _ILLEGAL_ROUTE_CODES:
        return PublicBgActError(
            code="ILLEGAL_ROUTE",
            message="当前路线没有形成可提交的合法候选。",
            next_action=(
                "根据最新校验结果修改首个不符合之处并重新形成完整路线；"
                "需要核验时 Check，已经决定并写出完整路线时也可直接 Commit。"
            ),
        )
    if code in _SEMANTIC_RECHECK_CODES:
        return PublicBgActError(
            code="CHECK_REQUIRED",
            message="该显示编号不是当前可提交短标签；参考路线尚未形成提交绑定。",
            next_action=(
                "复制想采用参考路线的完整 semanticChain，使用 operation=check "
                "重新校验；不要改成直接 chains commit。"
            ),
        )
    if code in _STALE_FRAME_CODES:
        return PublicBgActError(
            code="STALE_FRAME",
            message="当前候选不属于最新决策状态。",
            next_action=(
                "以最新 DecisionFrame 重新判断并形成路线；仍需校验时 Check，"
                "已经决定并写出完整路线时也可直接 Commit。"
            ),
        )
    if code == "ACTION_ALREADY_COMMITTED":
        return PublicBgActError(
            code="ALREADY_COMMITTED",
            message="当前决策已经提交。",
            next_action="不要再次提交，等待下一 DecisionFrame。",
        )
    if code == "DUPLICATE_CHECK":
        return PublicBgActError(
            code="DUPLICATE_CHECK",
            message="当前 DecisionFrame 已有完全相同的 Check 结果。",
            next_action=(
                "使用上一条 Tool result 继续比较；若路线不符合意图，修改动作或参数后"
                "提交不同的完整 Check，不要原样重发。"
            ),
        )
    if code in _UNCERTAIN_CODES:
        return PublicBgActError(
            code="COMMIT_UNCERTAIN_STOP",
            message="提交结果尚不能安全确认。",
            next_action="不要再次提交；等待宿主恢复或确认当前状态。",
        )
    if code in _CHECK_UNAVAILABLE_CODES:
        return PublicBgActError(
            code="CHECK_UNAVAILABLE",
            message="当前校验服务不可用。",
            next_action="稍后重试当前 Check；不要改写或提交未经校验的路线。",
        )
    return PublicBgActError(
        code="BG_ACT_FAILED",
        message="当前行动请求未完成。",
        next_action="根据最新 DecisionFrame 与 Tool result 重新判断。",
    )


__all__ = ["PublicBgActError", "render_public_bgact_error"]
