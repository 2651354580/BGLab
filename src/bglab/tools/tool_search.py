"""Compatibility export for the registry-owned deferred Tool bridge.

The live ToolSearch/ToolDescribe/ToolCall schemas and handlers are created by
``DeferredRegistry`` through ``QueryDeps``.  This disabled Tool object preserves
the old import name without maintaining a second global catalog.
"""

from __future__ import annotations

from bglab.tools.base import Tool, tool_error


def tool_search(_arguments: dict):
    return tool_error(
        "Error: ToolSearch is available only when this query has deferred tools.",
    )


ToolSearchTool = Tool(
    name="ToolSearch",
    searchHint="find deferred tools by capability",
    description="Search the current query's deferred Tool catalog.",
    prompt="Search the current query's deferred Tool catalog.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    },
    call=tool_search,
    is_read_only=True,
    auto_allow=True,
    plan_allowed=True,
    is_enabled=lambda: False,
)


__all__ = ["ToolSearchTool", "tool_search"]
