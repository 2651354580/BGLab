"""One per-query deferred Tool registry and its three model-facing bridges."""

from __future__ import annotations

import asyncio
import copy
import concurrent.futures
import inspect
import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from bglab.llm.types import ToolDefinition
from bglab.engine.error_policy import DeferredTargetHandlerFailure
from bglab.permissions.types import ToolPermissionSpec
from bglab.tools.base import tool_error


_BRIDGE_NAMES = frozenset({"ToolSearch", "ToolDescribe", "ToolCall"})


def _bridge_definition(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    *,
    read_only: bool,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        permission_spec=ToolPermissionSpec(
            read_only=read_only,
            auto_allow=True,
            plan_allowed=True,
        ),
    )


_BRIDGE_DEFINITIONS = (
    _bridge_definition(
        "ToolSearch",
        "Search the currently deferred Tool catalog by name or capability.",
        {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        ["query"],
        read_only=True,
    ),
    _bridge_definition(
        "ToolDescribe",
        "Return the exact parameter schema for one deferred Tool.",
        {"name": {"type": "string"}},
        ["name"],
        read_only=True,
    ),
    _bridge_definition(
        "ToolCall",
        "Invoke one deferred Tool after inspecting its schema.",
        {
            "name": {"type": "string"},
            "arguments": {"type": "object"},
        },
        ["name", "arguments"],
        read_only=False,
    ),
)


@dataclass(frozen=True)
class AssemblyResult:
    activated: bool
    tool_defs: tuple[ToolDefinition, ...]
    deferred_count: int = 0


class DeferredRegistry:
    """Own classification, discovery, description, and dispatch for one query.

    Only explicitly ``should_defer`` tools enter the catalog.  This avoids a
    second global registry and prevents bridge tools from being advertised
    when no deferred capability exists.
    """

    def __init__(self) -> None:
        self._catalog: list[dict[str, Any]] = []
        self._by_name: dict[str, dict[str, Any]] = {}

    def classify(
        self,
        tool_defs: list[ToolDefinition],
        *,
        should_defer_names: set[str] | frozenset[str] | None = None,
    ) -> AssemblyResult:
        deferred_names = set(should_defer_names or ())
        visible: list[ToolDefinition] = []
        catalog: list[dict[str, Any]] = []
        seen: set[str] = set()

        for definition in tool_defs:
            if not isinstance(definition, ToolDefinition):
                raise TypeError("deferred registry requires ToolDefinition values")
            name = str(definition.name).strip()
            if not name:
                raise ValueError("deferred Tool schema name must be non-empty")
            if name in seen:
                raise ValueError(f"duplicate Tool schema: {name}")
            seen.add(name)
            if name in _BRIDGE_NAMES:
                # Bridge schemas are registry-owned and cannot be supplied by
                # another Tool registry with different behavior.
                continue
            copied = copy.deepcopy(definition)
            if name not in deferred_names:
                visible.append(copied)
                continue
            catalog.append({
                "name": name,
                "description": str(definition.description),
                "parameters": copy.deepcopy(definition.parameters),
                "searchHint": str(definition.search_hint),
                "permissionSpec": definition.permission_spec,
            })

        catalog.sort(key=lambda entry: entry["name"].casefold())
        self._catalog = catalog
        self._by_name = {entry["name"]: entry for entry in catalog}
        if not catalog:
            return AssemblyResult(False, tuple(visible), 0)

        return AssemblyResult(
            True,
            tuple([*visible, *(copy.deepcopy(_BRIDGE_DEFINITIONS))]),
            len(catalog),
        )

    def bridge_handlers(
        self,
        handlers: Mapping[str, Callable],
        *,
        authorize: Callable[[str, dict[str, Any], ToolPermissionSpec], Any],
    ) -> dict[str, Callable]:
        """Bind all three bridges to this registry and one handler surface."""

        source_handlers = dict(handlers)

        async def call(arguments: Mapping[str, Any]):
            return await self.dispatch_call(
                arguments,
                source_handlers,
                authorize=authorize,
            )

        return {
            "ToolSearch": self.dispatch_search,
            "ToolDescribe": self.dispatch_describe,
            "ToolCall": call,
        }

    def dispatch_search(self, arguments: Mapping[str, Any]) -> str:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return tool_error("Error: query is required")
        try:
            limit = int(arguments.get("max_results", 5))
        except (TypeError, ValueError):
            return tool_error("Error: max_results must be an integer")
        limit = max(1, min(limit, 20))

        selected: list[dict[str, Any]]
        if query.casefold().startswith("select:"):
            names = [part.strip() for part in query[7:].split(",") if part.strip()]
            selected = [self._by_name[name] for name in names if name in self._by_name]
        else:
            terms = [part.casefold() for part in query.split() if part.strip()]
            scored: list[tuple[int, str, dict[str, Any]]] = []
            for entry in self._catalog:
                haystack = " ".join((
                    entry["name"],
                    entry["description"],
                    entry["searchHint"],
                )).casefold()
                score = sum(term in haystack for term in terms)
                if score:
                    scored.append((-score, entry["name"].casefold(), entry))
            scored.sort(key=lambda item: (item[0], item[1]))
            selected = [item[2] for item in scored]

        public = [
            {"name": entry["name"], "description": entry["description"]}
            for entry in selected[:limit]
        ]
        return json.dumps({"tools": public}, ensure_ascii=False, sort_keys=True)

    def dispatch_describe(self, arguments: Mapping[str, Any]) -> str:
        name = str(arguments.get("name", "")).strip()
        entry = self._by_name.get(name)
        if entry is None:
            return tool_error(f"Error: deferred Tool not found: {name or '<empty>'}")
        return json.dumps(
            {
                "name": entry["name"],
                "description": entry["description"],
                "parameters": entry["parameters"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    async def dispatch_call(
        self,
        arguments: Mapping[str, Any],
        handlers: Mapping[str, Callable],
        *,
        authorize: Callable[[str, dict[str, Any], ToolPermissionSpec], Any],
    ) -> Any:
        name = str(arguments.get("name", "")).strip()
        if name not in self._by_name:
            return tool_error(f"Error: deferred Tool not found: {name or '<empty>'}")
        tool_arguments = arguments.get("arguments", {})
        if not isinstance(tool_arguments, dict):
            return tool_error("Error: arguments must be an object")
        handler = handlers.get(name)
        if handler is None:
            return tool_error(f"Error: deferred Tool handler unavailable: {name}")
        permission_spec = self._by_name[name]["permissionSpec"]
        try:
            decision = authorize(name, dict(tool_arguments), permission_spec)
            if inspect.isawaitable(decision):
                decision = await decision
            allowed, reason = decision
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            raise
        except Exception:
            return tool_error(
                f"Permission denied: deferred Tool authorization failed for {name}",
            )
        if not allowed:
            return tool_error(f"Permission denied: {reason}")
        try:
            result = handler(dict(tool_arguments))
            if inspect.isawaitable(result):
                result = await result
            return result
        except (asyncio.CancelledError, concurrent.futures.CancelledError):
            raise
        except Exception as error:
            raise DeferredTargetHandlerFailure(permission_spec) from error


__all__ = ["AssemblyResult", "DeferredRegistry"]
