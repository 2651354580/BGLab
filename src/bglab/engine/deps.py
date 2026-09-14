"""QueryDeps — dependency injection container.

Aligns with query/deps.ts: injects I/O + stateful resources so query_loop
doesn't hardcode imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class QueryDeps:
    ""

    # Default None = caller should pass call_model explicitly.
    # _default_deps() resolves to the production implementation.
    call_model: Any = None

    # Non-streaming auxiliary I/O; compaction must be injectable too.
    complete_text: Any = None

    # Stateful per-session resources — created once, reused across turns
    compact_tracker: Any = None      # CompactTracker
    loop_detector: Any = None        # LoopDetector
    budget_tracker: Any = None       # BudgetTracker (engine.stubs)

    # Stop hooks per-session state — cursor + counters. Hook policy belongs to
    # QueryLoopProfile so Code and Game cannot grow separate loop branches.
    stop_hooks_state: Any = None     # StopHooksState
    query_profile: Any = None         # defaults to CODE_QUERY_PROFILE

    # Post-compact relink state — restored after compaction boundary
    compact_relink: Any = None       # dict: claude_md, skill_names, session_meta, permission_mode, agent_names

    # Deferred tool registry — schema filtering for large/infrequent tools
    deferred_registry: Any = None      # DeferredRegistry from tools.deferred

    # System-attachment tracking
    _prev_permission_mode: Any = None
    _last_date: str = ""

    # Deferred tool list attachment tracking
    _deferred_list_injected: bool = False
    _deferred_list_last_turn: int = 0
    _last_assembly: Any = None       # AssemblyResult from last classify()
    _last_tool_surface_names: list[str] | None = None

    # UI progress callbacks (set by CLI, called by engine)
    compaction_tick: Any = None      # Callable[[str, str], None] — label, tokens_str
    llm_spinner: Any = None          # Callable[[str], None] — start spinner with message
    invalidate_prompt_context: Any = None  # Callable[[str], None]
    request_prompt_resolver: Any = None  # Callable[..., PromptBundle]
    current_cwd: str | None = None

    # Feature flags — FeatureFlags instance (persisted + env-overridden)
    feature_flags: Any = None         # FeatureFlags from session.feature_flags

    def __post_init__(self) -> None:
        if self.deferred_registry is None:
            from bglab.tools.deferred import DeferredRegistry

            self.deferred_registry = DeferredRegistry()


def get_call_model():
    """Lazy resolver — allows tests to mock bglab.engine.deps.get_call_model."""
    from bglab.llm.client import call_model as _call_model
    return _call_model
