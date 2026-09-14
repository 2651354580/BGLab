"""LLM 层的类型定义 — 和 Anthropic/OpenAI 都对齐。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

from bglab.permissions.types import ToolPermissionSpec


class StopReason(str, Enum):
    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"


class LoopEventType(str, Enum):
    TEXT = "text"                      # 流式文本片段
    TOOL_USE_START = "tool_use_start"  # 工具调用开始
    TOOL_USE_END = "tool_use_end"      # 工具调用结束
    TOOL_RESULT = "tool_result"        # 工具执行结果
    PERMISSION_ASK = "permission_ask"  # 需要用户确认（CLI 弹 input()）
    ERROR = "error"                    # 出错了
    DONE = "done"                      # 本轮完成


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolDefinition:
    """工具定义 — 能直接转成 OpenAI/Anthropic tool schema。"""
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema 格式的 inputs
    search_hint: str = ""       # 对齐 Tool.searchHint — 用于延期工具搜索
    always_load: bool = False   # 永不被 defer
    should_defer: bool = False  # 总是被 defer（即使低于 token 阈值）
    permission_spec: ToolPermissionSpec = field(
        default_factory=ToolPermissionSpec,
    )

    def to_openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_anthropic_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def to_responses_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass
class TerminalInfo:
    """终端信息 — 挂在最后的 DONE 事件上。"""
    reason: str                      # "completed" | "max_turns" | "model_error" | ...
    turn_count: int = 0
    total_usage: dict[str, int] | None = None
    elapsed_ms: float = 0.0


@dataclass(frozen=True)
class ProviderFailureInfo:
    """Redacted, structured metadata for a Provider/API failure."""

    reason: str
    retryable_same_slot: bool
    switch_slot: bool
    status_code: int | None = None


@dataclass(frozen=True)
class CompletionObservation:
    """Body-free outcome of one non-streaming completion, including no usage."""

    model: str
    usage: dict[str, int | bool] | None
    provider_failure: ProviderFailureInfo | None
    provider_attempts: int
    elapsed_ms: float
    status: Literal["completed", "failed", "cancelled", "incomplete"]


@dataclass
class LoopEvent:
    """Agent Loop 产出的每一帧事件。"""
    type: LoopEventType
    text: str | None = None
    tool_use: ToolUseBlock | None = None
    tool_result: ToolResultBlock | None = None
    error: str | None = None
    stop_reason: StopReason | None = None
    usage: dict[str, Any] | None = None
    reasoning_content: str | None = None
    terminal: TerminalInfo | None = None  # 仅最后一个 DONE 事件带
    provider_failure: ProviderFailureInfo | None = None
    request_purpose: str = "action"  # Auxiliary DONE events only contribute usage.
