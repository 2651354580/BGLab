"""Safe, provider-neutral presentation for exhausted API failures."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping

from bglab.llm.types import ProviderFailureInfo


@dataclass(frozen=True)
class ProviderIssuePresentation:
    category: str
    title: str
    recoverable: bool
    status_code: int | None = None
    attempts: int = 0
    retries: int = 0
    timeouts: int = 0

    @property
    def detail(self) -> str:
        if self.attempts > 0:
            prefix = f"自动请求 {self.attempts} 次后仍未成功"
        else:
            prefix = "自动重试后仍未成功"
        return f"{prefix}；输入任意内容继续。"

    def with_usage(
        self,
        usage: Mapping[str, int | float | str] | None,
    ) -> ProviderIssuePresentation:
        values = usage or {}
        return replace(
            self,
            attempts=_safe_count(values.get("provider_attempts")),
            retries=_safe_count(values.get("provider_retries")),
            timeouts=_safe_count(values.get("provider_timeouts")),
        )


def project_provider_failure(
    failure: ProviderFailureInfo,
) -> ProviderIssuePresentation:
    """Map structured metadata to a stable label without raw provider text."""

    reason = str(failure.reason or "unknown").strip().casefold()
    status = failure.status_code
    if "timeout" in reason:
        category, title = "timeout", "API 请求超时"
    elif reason == "repeated_output":
        category, title = "repeated_output", "模型输出未收敛"
    elif reason == "model_nonconvergence":
        category, title = "model_nonconvergence", "模型多次尝试仍未完成行动"
    elif reason == "chat_reply_unavailable":
        category, title = "chat_reply_unavailable", "模型暂未完成聊天回复"
    elif reason == "output_limit":
        category, title = "output_limit", "模型输出达到上限"
    elif reason == "invalid_tool_arguments":
        category, title = "invalid_tool_arguments", "模型未生成完整工具参数"
    elif "connection" in reason or "network" in reason:
        category, title = "connection", "无法连接 API 服务"
    elif status in {401, 403} or reason in {"http_401", "http_403"}:
        category, title = "authentication", "API 认证失败"
    elif status == 429 or reason == "http_429":
        category, title = "rate_limit", "API 请求受限"
    elif reason == "insufficient_system_resource" or (status is not None and status >= 500) or reason.startswith("http_5"):
        category, title = "service", "API 服务暂时不可用"
    else:
        category, title = "provider", "API 请求失败"
    return ProviderIssuePresentation(
        category=category,
        title=title,
        recoverable=True,
        status_code=status,
    )


def _safe_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return 0
