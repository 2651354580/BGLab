"""Conditional Game Memory semantics selected by the Player Profile."""

from __future__ import annotations

from bglab.games.features import GameFeatureProfile


MEMORY_SECTION = """## Game memory

<game-memory> 是有条件的历史经验，不是当前状态或正式规则。只有条件与当前局面匹配时才使用；
最新规则、DecisionFrame 和 Tool Result 始终优先。只需在本局内保留的信息应写入持久 Plan，
而不是长期 Memory。"""


def persistence_sections(profile: GameFeatureProfile) -> tuple[str, ...]:
    if not (profile.memory_selection or profile.memory_extraction):
        return ()
    return (MEMORY_SECTION,)
PERSISTENCE_SECTIONS = (MEMORY_SECTION,)
