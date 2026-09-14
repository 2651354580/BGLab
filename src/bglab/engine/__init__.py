""

from __future__ import annotations

from importlib import import_module


_EXPORT_MODULES = {
    "submit_message": "bglab.engine.submit",
    "QueryEngine": "bglab.engine.submit",
    "process_user_input": "bglab.engine.submit",
    "fetch_system_prompt_parts": "bglab.engine.submit",
    "QueryDeps": "bglab.engine.deps",
    "query_loop": "bglab.engine.query",
    "QueryState": "bglab.engine.query",
    "QueryTracking": "bglab.engine.query",
    "ToolUseContext": "bglab.engine.query",
    "Transition": "bglab.engine.query",
    "TransitionReason": "bglab.engine.query",
    "TerminalReason": "bglab.engine.query",
}

__all__ = [
    "submit_message",
    "QueryEngine",
    "process_user_input",
    "fetch_system_prompt_parts",
    "query_loop",
    "QueryDeps",
    "QueryState",
    "QueryTracking",
    "ToolUseContext",
    "Transition",
    "TransitionReason",
    "TerminalReason",
]


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
