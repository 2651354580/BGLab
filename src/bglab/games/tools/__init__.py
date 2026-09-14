"""Game tools package with lazy Player-tool assembly exports.

Semantic Core submodules must remain importable without importing the
Game-owned ``tool_factory`` or ``AuthorityWorker`` dependency graph.
"""

from __future__ import annotations

from typing import Any


__all__ = ["create_all_tools", "create_tools"]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(name)
    from bglab.games.tools.tool_factory import create_all_tools, create_tools

    return {
        "create_all_tools": create_all_tools,
        "create_tools": create_tools,
    }[name]
