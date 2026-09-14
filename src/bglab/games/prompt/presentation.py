"""Player communication content Module."""

from __future__ import annotations

from bglab.games.features import GameFeatureProfile


GAME_OUTPUT_SECTION = """# Communication

可见正文面向玩家，使用中文，说明选择及影响它的事实即可。可以直接调用工具，也可以在需要说明时输出正文；没有字数或句数要求。内部自问、未采用草稿以及工具已经展示的参数和计算无需写入正文。"""


READ_ONLY_OUTPUT_SECTION = """# Communication

Use the language of the latest user-facing instruction. Do not switch because
rules, DecisionFrame field names, Tool schema, or Tool results use another
language. Give compact, checkable conclusions and the material arithmetic or
boundary that supports them. Separate confirmed facts from strategic inference.
Do not reveal private token-by-token chain of thought."""


def presentation_sections(profile: GameFeatureProfile) -> tuple[str, ...]:
    if not profile.basic_tools:
        return (READ_ONLY_OUTPUT_SECTION,)
    return (GAME_OUTPUT_SECTION,)
PRESENTATION_SECTIONS = (GAME_OUTPUT_SECTION,)
TONE_SECTION = GAME_OUTPUT_SECTION
LANGUAGE_SECTION = GAME_OUTPUT_SECTION
