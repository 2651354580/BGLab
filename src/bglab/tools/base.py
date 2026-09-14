""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from bglab.permissions.types import (
    PermissionDecision,
    ToolPermissionSpec,
)


def _always_true() -> bool:
    return True


class ToolCallResult(str):
    """String-compatible tool output carrying an explicit semantic status."""

    is_error: bool
    metadata: dict[str, Any]

    def __new__(
        cls,
        content: str,
        *,
        is_error: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ):
        value = super().__new__(cls, content)
        value.is_error = is_error
        value.metadata = dict(metadata or {})
        return value


def tool_error(
    content: str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> ToolCallResult:
    """Return a model-visible tool failure without raising away its details."""
    return ToolCallResult(content, is_error=True, metadata=metadata)


@dataclass
class Tool:
    ""
    name: str
    description: str
    prompt: str
    parameters: dict[str, Any]
    call: Callable[[dict[str, Any]], str]

    
    aliases: list[str] = field(default_factory=list)
    searchHint: str = ""

    
    is_read_only: bool = False
    auto_allow: bool = False
    plan_allowed: bool = False
    is_destructive: bool = False
    is_mcp: bool = False
    mcp_info: dict[str, str] | None = None
    always_load: bool = False
    should_defer: bool = False

    
    check_permission: Callable[[dict[str, Any]], PermissionDecision] | None = None
    validate_input: Callable[[dict[str, Any]], str | None] | None = None
    is_enabled: Callable[[], bool] = field(default_factory=lambda: _always_true)
    is_concurrency_safe: bool = False
    max_result_size_chars: int = 50_000

    @property
    def permission_spec(self) -> ToolPermissionSpec:
        return ToolPermissionSpec(
            read_only=self.is_read_only,
            auto_allow=self.auto_allow,
            plan_allowed=self.plan_allowed,
            checker=self.check_permission,
        )

    def to_tool_definition(self):
        from bglab.llm.types import ToolDefinition
        return ToolDefinition(
            name=self.name,
            description=self.prompt,
            parameters=self.parameters,
            search_hint=self.searchHint,
            always_load=self.always_load,
            should_defer=self.should_defer,
            permission_spec=self.permission_spec,
        )




def build_tool(
    name: str,
    description: str,
    prompt: str,
    parameters: dict[str, Any],
    call: Callable[[dict[str, Any]], str],
    **overrides,
) -> Tool:
    ""
    return Tool(
        name=name,
        description=description,
        prompt=prompt,
        parameters=parameters,
        call=call,
        **overrides,
    )




def tool_matches_name(tool: Tool, name: str) -> bool:
    ""
    if tool.name == name:
        return True
    return name in tool.aliases


# ── ToolRegistry ──

class ToolRegistry:
    ""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        ""
        return find_tool_by_name(self._tools, name)

    def get_handler(self, name: str) -> Callable | None:
        tool = self.get(name)
        return tool.call if tool else None

    def get_permission_checker(self, name: str) -> Callable | None:
        tool = self.get(name)
        return tool.permission_spec.checker if tool else None

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    def list_enabled(self) -> list[Tool]:
        """返回所有 is_enabled()=True 的工具。"""
        return [t for t in self._tools.values() if t.is_enabled()]

    def to_definitions(self) -> list:
        return [tool.to_tool_definition() for tool in self.list_enabled()]

    def to_handler_dict(self) -> dict[str, Callable]:
        return {tool.name: tool.call for tool in self.list_enabled()}

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def __iter__(self):
        return iter(self._tools.values())

    def __len__(self) -> int:
        return len(self._tools)


def find_tool_by_name(tools: dict[str, Tool], name: str) -> Tool | None:
    ""
    if name in tools:
        return tools[name]
    for tool in tools.values():
        if name in tool.aliases:
            return tool
    return None




def assemble_tool_pool(
    builtin_tools: list[Tool],
    mcp_tools: list[Tool] | None = None,
) -> list[Tool]:
    ""
    mcp = mcp_tools or []

    # 去重: built-in 名字优先
    builtin_names = {t.name for t in builtin_tools} | {
        alias for t in builtin_tools for alias in t.aliases
    }
    deduped_mcp = [t for t in mcp if t.name not in builtin_names]

    # 各自字母排序（builtInTools 在前，MCP 在后）
    by_name = lambda t: t.name
    return sorted(builtin_tools, key=by_name) + sorted(deduped_mcp, key=by_name)




def filter_tools_by_mode(
    tools: list[Tool],
    permission_mode: str,
) -> list[Tool]:
    ""
    if permission_mode == "plan":
        return [tool for tool in tools if tool.permission_spec.plan_allowed]
    return list(tools)


def filter_tools_by_deny_rules(
    tools: list[Tool],
    permission_context,
) -> list[Tool]:
    ""
    if permission_context is None:
        return list(tools)
    denied = set()
    for tool_name, rules in getattr(permission_context, 'always_deny', {}).items():
        if rules:
            denied.add(tool_name)
    if not denied:
        return list(tools)
    return [t for t in tools if t.name not in denied]
