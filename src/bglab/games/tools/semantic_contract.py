"""One immutable model-facing contract for the existing BgAct seam.

The contract is derived from a package ``SemanticDescriptor``.  It centralizes
copy and public field expectations already consumed by BgAct; it does not add
a Prompt, Tool, query stage, or lifecycle.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from bglab.games.semantic_validation.checked_route_fact import CheckedRouteFact
from bglab.games.semantic_validation.descriptor import SemanticDescriptor


_PUBLIC_FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_CHECKED_ROUTE_REFERENCES = (
    "C1", "C2", "C3", "C4", "C5",
)

_GENERIC_CONVERSION_GUIDANCE = (
    "只使用 Tool schema 中的 semantic action 名；不要复制 Frame 的引擎 camelCase 名。"
    "从整条冻结回答的可执行候选与比较选择共同还原最终选中路线；"
    "最终段落即使简写，也不能漏掉前文已明确绑定到该路线且未被撤销的选择。"
    "若各节冲突，较后的比较选择与后续边界优先。"
    "语义 chains 只写玩家选择；自动资源、固定单一后续和机械刷新不写。"
    "同一组选中动作按 Frame 的权威因果顺序排列，不按自然语言句子出现顺序排列。"
    "调用前逐个 action 对照当前 Tool schema 的 properties 做最终检查；"
    "未声明字段必须删除，不能把自然语言里的说明性名词自动变成参数。"
    "若某动作的目的地是规则固定的，而该 action schema 没有 target/location 字段，"
    "自然语言提到这个固定目的地也不得补写该字段。"
    "冻结回答已经点名的资源、颜色、目标、分支或顺序必须转换成对应 action 字段。"
    "若它在Frame明确列出的选择间仍未确定最优，应形成多条完整临时路线供同一次Check，"
    "而不是在choice前截断；Frame与Schema未提供的目标仍不得编造。"
    "冻结回答明确的采用、跳过或不支付也是玩家选择，必须写入 Tool schema 对应的分支 action。"
    "不得补造Frame与Schema未提供的ID。"
)

_BRANCH_CHAIN_GUIDANCE = (
    "若路线明确先选择一个分支，再执行该分支开启的下游动作，这代表两个按因果顺序"
    "发生的玩家选择；Tool schema 提供分支选择 action 时，必须先写该 choice action，"
    "再写下游 action，不能用下游 action 代替或省略上游分支。"
    "若冻结回答已经明确了下游目标（例如某个成员目标、卡牌、位置或后续选择），"
    "必须继续查 Tool schema 写出该目标对应的 semantic action；不能只写到开启它的"
    "上游动作，等待 Check 猜测或自动补全玩家意图。"
)

_SINGLE_CONVERSION_HEAD = (
    "把上一条已冻结自然语言中的最终选择原样转换为一次 BgAct 只读校验。"
    "本回复不要输出自然语言，只调用一次 BgAct；operation 必须是 check，chains 必须且只能包含一条路线；"
    "保留行动顺序和战略选择，不调用 commit。"
)

_BATCH_CONVERSION_HEAD = (
    "把上一条已冻结自然语言明确标出的两至三条‘可执行候选’分别转换为同一次 BgAct 批量只读校验。"
    "本回复不要输出自然语言，只调用一次 BgAct；operation 必须是 check，chains 按待校验集合顺序完整保留；"
    "正文第1/2/3条候选必须原样对应 R1/R2/R3，不能把最终暂选路线移动到R1，"
    "‘暂选路线补充’只能给对应候选补全已明确参数，不能改变任何R序号或删除其它候选；"
    "集合内每条都是上游明确列出的可执行候选，即使排序较低也必须保留，"
    "不能由转换步骤预先删除或重排。如果正文给了四条以上具体路线却没有明确一至三条"
    "待校验集合，不要擅自替模型排序或调用 commit；该输入应留给上游重新明确集合。"
    "每条保留行动顺序和战略选择。"
)

_GENERIC_SUBMISSION_BOUNDARY = (
    "采用 Check 返回路线时，优先使用本轮短 ID；修改动作或参数后该 ID 不再代表新路线。"
    "新路线需要核验时重新 Check；模型已经决定并写出完整路线时也可直接 Commit。"
    "“结果：”只描述权威效果，不能把结果字段、资源支付、自动奖励或机械步骤反向改写成 actions。"
    "标为“权威自动补全”的派生步骤必须省略；不在当前 Tool schema 的 action 不得提交。"
)
_FINISH_SUBMISSION_BOUNDARY = (
    "finish_action 会立即结束当前可选动作链，不会把已解锁行动留到下一 Frame；"
    "若先支付解锁行动再 finish_action，该行动会被当场放弃。"
    "它只用于比较当前可执行目标后明确决定放弃该行动；不能因为后缀尚未写出、"
    "参数不确定或想让路线尽快完整就使用 finish_action。"
)

_CHECK_RESULT_GUIDANCE = (
    "\n\n下一步：先确认候选保留了你的选择，或其修正、补出的分支是你愿意接受的。"
    "决定执行其中一条时，直接 Commit 本轮路线 id；它的费用、收益和剩余资源已由引擎核验。"
    "若都不合适，针对不接受的选择形成新意图，再 Check；也可以直接 Commit 已完整确定的新行动链。\n"
    "Check 核验规则和结果，不替你决定战略；未来价值仍按条件判断。"
    "相同事实不必重新计算，没有新条件不要重发相同 Check。"
    "公开说明只写最终选择和真正影响决定的依据，区分实际结果与未来条件。\n"
)

_RESULT_GUIDANCE_HEAD = _CHECK_RESULT_GUIDANCE


@dataclass(frozen=True, slots=True)
class PublicFieldRequirement:
    """One field that model guidance requires from a public checked-route fact."""

    field: str
    source: Literal["fact", "flattened-outcome"]
    when_action: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.field, str) or _PUBLIC_FIELD_RE.fullmatch(self.field) is None:
            raise ValueError("public field requirement must use one public field name")
        if self.source not in {"fact", "flattened-outcome"}:
            raise ValueError("public field requirement source is invalid")
        if self.when_action is not None and (
            not isinstance(self.when_action, str) or not self.when_action
        ):
            raise ValueError("public field requirement action must be non-empty text")


def _required_public_fields(
    action_names: tuple[str, ...],
) -> tuple[PublicFieldRequirement, ...]:
    requirements = [
        PublicFieldRequirement("label", "fact"),
        PublicFieldRequirement("semanticChain", "fact"),
        PublicFieldRequirement("matchedPrefix", "fact"),
        PublicFieldRequirement("firstDivergence", "fact"),
    ]
    if "finish_action" in action_names:
        requirements.append(PublicFieldRequirement(
            "unexecutedActionAbandoned",
            "flattened-outcome",
            when_action="finish_action",
        ))
    return tuple(requirements)


@dataclass(frozen=True, slots=True)
class SemanticInteractionContract:
    """Immutable copy/schema contract derived from one package descriptor."""

    engine: str
    action_names: tuple[str, ...]
    checked_route_references: tuple[str, ...]
    required_public_fields: tuple[PublicFieldRequirement, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.engine, str) or not self.engine.strip():
            raise ValueError("interaction contract engine must be non-empty text")
        action_names = tuple(self.action_names)
        if (
            not action_names
            or any(not isinstance(name, str) or not name for name in action_names)
            or len(action_names) != len(set(action_names))
        ):
            raise ValueError(
                "interaction contract action_names must be unique non-empty text",
            )
        references = tuple(self.checked_route_references)
        if references != _CHECKED_ROUTE_REFERENCES:
            raise ValueError(
                "interaction contract checked_route_references must equal the "
                "current BgAct reference set",
            )
        requirements = tuple(self.required_public_fields)
        if (
            not requirements
            or any(
                not isinstance(item, PublicFieldRequirement)
                for item in requirements
            )
            or len(requirements) != len(set(requirements))
        ):
            raise ValueError(
                "interaction contract required_public_fields must be unique "
                "PublicFieldRequirement values",
            )
        unknown_actions = sorted({
            item.when_action
            for item in requirements
            if item.when_action is not None
            and item.when_action not in action_names
        })
        if unknown_actions:
            raise ValueError(
                "interaction contract public-field requirement references "
                "unknown action: " + unknown_actions[0],
            )
        if requirements != _required_public_fields(action_names):
            raise ValueError(
                "interaction contract required_public_fields must equal the "
                "guidance inventory for action_names",
            )
        object.__setattr__(self, "engine", self.engine.strip())
        object.__setattr__(self, "action_names", action_names)
        object.__setattr__(self, "checked_route_references", references)
        object.__setattr__(self, "required_public_fields", requirements)

    @classmethod
    def for_descriptor(
        cls,
        descriptor: SemanticDescriptor,
    ) -> "SemanticInteractionContract":
        if not isinstance(descriptor, SemanticDescriptor):
            raise TypeError("interaction contract requires SemanticDescriptor")
        action_names = tuple(descriptor.model_action_names)
        return cls(
            engine=descriptor.engine,
            action_names=action_names,
            checked_route_references=_CHECKED_ROUTE_REFERENCES,
            required_public_fields=_required_public_fields(action_names),
        )

    @property
    def supports_finish_action(self) -> bool:
        return "finish_action" in self.action_names

    def checked_route_reference_schema(self) -> dict[str, object]:
        return {
            "type": "string",
            "enum": list(self.checked_route_references),
        }

    def submission_boundary_guidance(self) -> str:
        return _GENERIC_SUBMISSION_BOUNDARY + (
            _FINISH_SUBMISSION_BOUNDARY if self.supports_finish_action else ""
        )

    def conversion_guidance(self) -> str:
        return _GENERIC_CONVERSION_GUIDANCE + _BRANCH_CHAIN_GUIDANCE + (
            _FINISH_SUBMISSION_BOUNDARY if self.supports_finish_action else ""
        )

    def single_conversion_instruction(self) -> str:
        return _SINGLE_CONVERSION_HEAD + self.conversion_guidance()

    def batch_conversion_instruction(self) -> str:
        return _BATCH_CONVERSION_HEAD + self.conversion_guidance()

    def check_operation_summary(self) -> str:
        return (
            "只读校验一至三条拟议的当前行动路线，不改变游戏状态；返回语义最接近的"
            "完整合法路线及其效果。写入自己已经作出的玩家选择；Check 可以修复小遗漏，"
            "但不替你选择策略。不要故意停在当前 Frame 已经能够作出的玩家选择之前。"
            "需要比较的路线放进同一次 Check，不要逐条串行校验。确定性的合法性、费用、"
            "自动奖励、强制补全和路线效果由 Check 权威计算，不要在可见正文中反复推导。"
            "返回路线不是策略推荐，没有最低收益门槛，也不要求接受。"
        )

    def commit_operation_summary(self) -> str:
        return (
            "提交一条行动：可以使用当前 DecisionFrame 中 Check 返回的路线 ID，"
            "也可以在决定执行后直接提交一条完整行动链。Check 之后优先用当前短 ID，避免"
            "重写错误；它只是安全简写，不是唯一 Commit 方式。直接 Commit 是模型的自主"
            "选择，不是只给简单路线的例外。行动链会被权威校验，只有无需纠错或新增玩家"
            "选择时才成功。DecisionFrame 改变后路线 ID 立即失效。Commit 会改变权威状态。"
        )

    def tool_description(self) -> str:
        return (
            "比较路线或不确定某个玩家选择时，用 operation=check 一次校验一至三条有实质"
            "差异的当前行动路线。Check 只读，返回完整合法的纠正路线和事实效果；它不替"
            "模型排序策略，也不要求接受任何路线。决定执行后，用 operation=commit 提交"
            "当前路线 ID，或直接提交一条完整行动链。Check 之后优先用当前 ID；它只是"
            "简写，不是唯一 Commit 方式。无论路线长短，直接 Commit 都由模型自主决定，"
            "但只有无需纠错或新增玩家选择的完整链才会成功。若 Check 结果都不符合原意，"
            "形成实质不同的新路线；需要核验时再 Check，已经决定时可直接 Commit 完整新链。"
            "旧 Frame 的 ID 无效。Commit 会改变权威游戏状态。"
        )

    def tool_guidance(self) -> str:
        return self.tool_description().replace(
            "Choose check",
            "Choose operation=check",
        ).replace(
            "commit only",
            "operation=commit only",
        )

    def check_result_guidance(self) -> str:
        return _CHECK_RESULT_GUIDANCE

    def result_guidance(self) -> str:
        return _RESULT_GUIDANCE_HEAD + self.submission_boundary_guidance()

    def audit_visible_requirements(
        self,
        checked_routes: Sequence[CheckedRouteFact],
    ) -> tuple[str, ...]:
        """Return missing public fields; invalid input raises instead of guessing."""

        if not isinstance(checked_routes, (list, tuple)):
            raise TypeError("checked_routes must be a sequence of CheckedRouteFact")
        issues: list[str] = []
        for fact in checked_routes:
            if not isinstance(fact, CheckedRouteFact):
                raise TypeError("checked_routes must contain CheckedRouteFact values")
            public_fact = fact.to_dict()
            flattened = {
                key: value
                for key, value in public_fact.items()
                if key != "outcome"
            }
            flattened.update(public_fact["outcome"])
            actions = {
                item.get("action")
                for item in public_fact["semanticChain"]["actions"]
                if isinstance(item, dict)
            }
            for requirement in self.required_public_fields:
                if (
                    requirement.when_action is not None
                    and requirement.when_action not in actions
                ):
                    continue
                source = public_fact if requirement.source == "fact" else flattened
                if requirement.field not in source:
                    source_label = requirement.source.replace("-", " ")
                    issues.append(
                        f"{fact.label}: missing {source_label} field "
                        f"{requirement.field}",
                    )
        return tuple(issues)

    @classmethod
    def compatibility_conversion_guidance(cls) -> str:
        """Generate the game-neutral compatibility conversion copy."""

        return _GENERIC_CONVERSION_GUIDANCE + _BRANCH_CHAIN_GUIDANCE

    @classmethod
    def compatibility_single_conversion_instruction(cls) -> str:
        return _SINGLE_CONVERSION_HEAD + cls.compatibility_conversion_guidance()

    @classmethod
    def compatibility_batch_conversion_instruction(cls) -> str:
        return _BATCH_CONVERSION_HEAD + cls.compatibility_conversion_guidance()

    @classmethod
    def compatibility_submission_boundary(cls) -> str:
        """Generate generic legacy copy without leaking a game capability."""

        return _GENERIC_SUBMISSION_BOUNDARY

    @classmethod
    def compatibility_check_result_guidance(cls) -> str:
        return _CHECK_RESULT_GUIDANCE

    @classmethod
    def compatibility_result_guidance(cls) -> str:
        return _RESULT_GUIDANCE_HEAD + _GENERIC_SUBMISSION_BOUNDARY


__all__ = [
    "PublicFieldRequirement",
    "SemanticInteractionContract",
]
