"""权限类型 — 对齐 types/permissions.ts。

3 层权限架构:
  1. Tool-level: 每个 Tool 自己的 check_permission()
  2. Settings-level: alwaysAllow / alwaysDeny / alwaysAsk rules
  3. Mode-level: default / plan / accept_edits / bypass
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class PermissionMode(str, Enum):
    """对齐 types/permissions.ts EXTERNAL_PERMISSION_MODES"""
    DEFAULT = "default"           # 写/命令 → 询问用户
    PLAN = "plan"                 # 只读
    ACCEPT_EDITS = "accept_edits" # 自动接受编辑
    BYPASS = "bypass"             # 全放行
    DONT_ASK = "dont_ask"        


class PermissionBehavior(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass
class PermissionDecision:
    """权限检查的最终结果。"""
    behavior: PermissionBehavior
    reason: str = ""
    updated_input: dict[str, Any] | None = None


@dataclass(frozen=True)
class ToolPermissionSpec:
    """Internal permission Authority carried with one registered Tool.

    ``read_only`` classifies side effects only.  Permission is granted only by
    the ordered policy through ``auto_allow``, mode/rules, or confirmation.
    """

    read_only: bool = False
    auto_allow: bool = False
    plan_allowed: bool = False
    checker: Callable[[dict[str, Any]], PermissionDecision] | None = None


# ── Settings rules ──

class RuleSource(str, Enum):
    """规则来源 — 对齐 types/permissions.ts PermissionRuleSource"""
    USER_SETTINGS = "userSettings"
    PROJECT_SETTINGS = "projectSettings"
    CLI_ARG = "cliArg"
    SESSION = "session"          # 用户在 session 中点了"始终允许"


@dataclass
class PermissionRule:
    """一条权限规则 — 对齐 types/permissions.ts PermissionRuleValue"""
    tool_name: str
    behavior: PermissionBehavior
    source: RuleSource
    rule_content: str | None = None  # 内容匹配, e.g. "prefix:* /safe/" for Bash


# ── PermissionContext ──

@dataclass
class PermissionContext:
    """对齐 Tool.ts ToolPermissionContext

    跨 session 不恢复权限（对齐源码）."""
    mode: PermissionMode = PermissionMode.DEFAULT
    always_allow: dict[str, list[PermissionRule]] = field(default_factory=dict)  # tool_name → rules
    always_deny: dict[str, list[PermissionRule]] = field(default_factory=dict)
    always_ask: dict[str, list[PermissionRule]] = field(default_factory=dict)
    is_bypass_available: bool = True

    def add_allow_rule(self, tool_name: str, source: RuleSource,
                       content: str | None = None) -> None:
        self.always_allow.setdefault(tool_name, []).append(
            PermissionRule(tool_name, PermissionBehavior.ALLOW, source, content))

    def add_deny_rule(self, tool_name: str, source: RuleSource,
                       content: str | None = None) -> None:
        self.always_deny.setdefault(tool_name, []).append(
            PermissionRule(tool_name, PermissionBehavior.DENY, source, content))


# ── Skill frontmatter integration ──

def merge_skill_allowed_tools(
    context: PermissionContext,
    skills: list[dict] | None,
) -> None:
    ""
    if not skills:
        return
    for skill in skills:
        raw = skill.get("allowed_tools", "")
        if not raw:
            continue
        if isinstance(raw, str):
            tool_names = [t.strip() for t in raw.split(",")]
        elif isinstance(raw, list):
            tool_names = [str(t).strip() for t in raw]
        else:
            continue
        for tool_name in tool_names:
            if tool_name:
                context.add_allow_rule(tool_name, RuleSource.SESSION)


# ── Settings-based rule population ──

def populate_permission_context_from_settings(
    context: PermissionContext,
    settings: dict | None,
) -> None:
    ""
    if not settings:
        return
    rules = settings.get("permission_rules")
    if not rules or not isinstance(rules, dict):
        return

    for behavior_key, target_dict in (
        ("deny", context.always_deny),
        ("allow", context.always_allow),
        ("ask", context.always_ask),
    ):
        entries = rules.get(behavior_key)
        if not entries or not isinstance(entries, dict):
            continue
        for tool_name, val in entries.items():
            if not val:  # falsy → skip
                continue

            # Normalize to list of content matchers
            if isinstance(val, list):
                contents: list = val
            elif val is True:
                contents = [None]  # no content matching
            else:
                contents = [str(val)]  # truthy non-bool → content matcher

            for content in contents:
                if behavior_key == "deny":
                    context.add_deny_rule(tool_name, RuleSource.USER_SETTINGS, content if content else None)
                elif behavior_key == "allow":
                    context.add_allow_rule(tool_name, RuleSource.USER_SETTINGS, content if content else None)
                else:  # ask
                    context.always_ask.setdefault(tool_name, []).append(
                        PermissionRule(
                            tool_name, PermissionBehavior.ASK, RuleSource.USER_SETTINGS,
                            content if content else None,
                        ),
                    )
