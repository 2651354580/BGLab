""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SessionState:
    ""

    permission_mode: str = "default"
    pre_plan_mode: str = "default"
    model: str = "deepseek-chat"
    extra_dirs: list = field(default_factory=list)
    vim_mode: bool = False
    session_tag: str = ""
    session_name: str = ""
    total_turns: int = 0
    cwd: str = ""
    context_tokens_used: int = 0
    context_tokens_total: int = 128000


# Module-level singleton — the single source of truth for the whole process.
session_state = SessionState()
