""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class StopHooksState:
    """All mutable state for stop hooks: extraction cursor, auto-dream counters, snapshot."""

    # ── Extraction state (was extractMemories.ts closure-scoped) ──
    last_memory_message_uuid: str | None = None  # cursor: last processed message UUID
    memory_extract_count: int = 0
    turns_since_last_extraction: int = 0

    # ── Game memory extraction state (separate cursor for ~/.bglab/memory/games/) ──
    last_game_memory_message_uuid: str | None = None
    last_game_memory_message_count: int = 0
    game_memory_extract_count: int = 0
    game_turns_since_last_extraction: int = 0

    # ── Auto-dream state ──
    last_auto_dream_time: float = 0.0
    auto_dream_session_count: int = 0

    # ── Snapshot slot (was module-level _last_snapshot) ──
    last_snapshot: object | None = None  # CacheSafeSnapshot
