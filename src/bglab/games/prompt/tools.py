"""General Tool policy for Player Agents.

Tool-specific lifecycles belong to each Tool description and JSON Schema.
"""

from __future__ import annotations

from bglab.games.features import GameFeatureProfile


GENERAL_TOOL_POLICY_SECTION = """# Using tools

Tool Schema 是 Tool 名称、字段、类型、必填项、枚举和 JSON 结构的唯一权威；当前 Schema
没有的字段不要发送。

按当前实质问题选择 Tool，不要把所有可用 Tool 都调用一遍。相互独立的只读工作可以一起做，
存在依赖的工作按权威顺序执行。读取并应用最新 Tool Result。调用失败、已经提交或返回相同答案后，
不要原样重复调用。格式错误要修正参数；游戏方案不合法则要改变方案本身。

Tool 提供事实、计算、合法性或建议，但策略责任仍由你承担。只有成功改变状态的 Tool Result
才能完成当前 DecisionFrame。核心 Tool 的详细生命周期由该 Tool 的描述定义；每个游戏的行动
JSON 由其 Parameters 定义。"""


def tool_sections(
    profile: GameFeatureProfile,
    action_protocol: str = "semantic-v2",
    enabled_tool_names: frozenset[str] | None = None,
) -> tuple[str, ...]:
    del enabled_tool_names
    if not profile.basic_tools:
        return ()
    if action_protocol != "semantic-v2":
        raise ValueError(
            f"unsupported historical action protocol: {action_protocol}; "
            "current Prompt requires semantic-v2",
        )
    return (GENERAL_TOOL_POLICY_SECTION,)
