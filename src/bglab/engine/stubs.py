"""所有预留接口的桩函数 — 每个 Step 实现时替换为真实逻辑。

按模块组织：Skills / Hooks / Memory / Permissions / Budget / Compaction / SubAgent

返回值设计原则：
  - 桩返回空/默认值，不阻塞主流程
  - print 出自己在做什么（方便调试）
  - 签名和真实接口一致
"""

from __future__ import annotations

from typing import Any
from dataclasses import dataclass, field

from bglab.llm.types import ToolDefinition


# ═══════════════════════════════════════════════════════════
# Skills 桩（→ Step 4-10）
# ═══════════════════════════════════════════════════════════

async def load_skills(cwd: str) -> list[dict[str, str]]:
    """加载所有可用 Skills — 名字+描述。委托给 skills.loader 实现。"""
    from bglab.skills.loader import load_skills as _real_load
    return await _real_load(cwd)


async def get_skill_attachment(skills: list[dict]) -> str:
    """生成 skills 的 attachment 文本。委托给 skills.loader 实现。"""
    from bglab.skills.loader import get_skill_attachment as _real_get
    return await _real_get(skills)


# ═══════════════════════════════════════════════════════════
# Hooks 桩（→ Step 5-7）
# ═══════════════════════════════════════════════════════════

async def run_pre_tool_hooks(tool_name: str, tool_args: dict) -> bool:
    """PreToolUse hook — 返回 True = 拦截。Step 5 实现。"""
    return False


async def run_stop_hooks(
    messages: list[dict],
    state: Any,
) -> tuple[list[dict], bool]:
    """Stop hooks — 返回 (blocking_errors, prevent_continuation)。Step 7 实现。"""
    return [], False


async def run_post_turn_hooks(state: Any) -> None:
    """Post-turn hooks — 提取记忆、生成建议、整合记忆。Step 7-10 实现。"""
    pass


# ═══════════════════════════════════════════════════════════
# Memory 桩（→ Step 10）
# ═══════════════════════════════════════════════════════════

async def load_memory_prompt() -> str | None:
    """加载 memory 索引供 system prompt 注入。Step 10 实现。"""
    return None


async def select_relevant_memories(messages: list[dict], n: int = 5) -> list[dict]:
    """Flash 模型挑选 N 个相关记忆。Step 10 实现。"""
    return []


# ═══════════════════════════════════════════════════════════
# Permissions 桩（→ Step 5）
# ═══════════════════════════════════════════════════════════

def check_permission(
    tool_name: str,
    tool_args: dict[str, Any],
    mode: str = "default",
    allow_rules: list[str] | None = None,
    deny_rules: list[str] | None = None,
) -> tuple[bool, str]:
    """权限检查 — 对齐 canUseTool()。Step 5 实现。

    Returns:
        (allowed, reason)
    """
    # 临时：plan 模式下禁止写操作
    if mode == "plan" and tool_name in ("Write", "Edit", "Bash"):
        return False, f"plan mode: {tool_name} denied"
    return True, "ok"


# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════

import re as _re
import time as _time

_COMPLETION_THRESHOLD = 0.9  
_DIMINISHING_THRESHOLD = 500  

_SHORTHAND_START_RE = _re.compile(r'^\s*\+(\d+(?:\.\d+)?)\s*(k|m|b)\b', _re.IGNORECASE)
_SHORTHAND_END_RE = _re.compile(r'\s\+(\d+(?:\.\d+)?)\s*(k|m|b)\s*[.!?]?\s*$', _re.IGNORECASE)
_VERBOSE_RE = _re.compile(r'\b(?:use|spend)\s+(\d+(?:\.\d+)?)\s*(k|m|b)\s*tokens?\b', _re.IGNORECASE)
_MULTIPLIERS = {'k': 1000, 'm': 1_000_000, 'b': 1_000_000_000}


def parse_token_budget(text: str) -> int | None:
    """对齐 utils/tokenBudget.ts:21 parseTokenBudget。

    支持: `+500k`(开头/结尾) / `use 2M tokens` / `spend 3m tokens`。
    """
    if not text:
        return None
    m = _SHORTHAND_START_RE.match(text)
    if m:
        return _parse_match(m)
    m = _SHORTHAND_END_RE.search(text)
    if m:
        return _parse_match(m)
    m = _VERBOSE_RE.search(text)
    if m:
        return _parse_match(m)
    return None


def _parse_match(m: _re.Match) -> int:
    return int(float(m.group(1)) * _MULTIPLIERS[m.group(2).lower()])


@dataclass
class BudgetTracker:
    ""
    budget_total: int | None = None
    continuation_count: int = 0
    last_delta_tokens: int = 0
    last_global_turn_tokens: int = 0
    started_at: float = field(default_factory=lambda: _time.time())

    def check(self, global_turn_tokens: int, agent_id: str | None = None) -> dict:
        ""
        if agent_id or self.budget_total is None or self.budget_total <= 0:
            return {'action': 'stop', 'completion_event': None}

        turn_tokens = global_turn_tokens
        pct = round((turn_tokens / self.budget_total) * 100)
        delta_since_last = global_turn_tokens - self.last_global_turn_tokens

        is_diminishing = (
            self.continuation_count >= 3
            and delta_since_last < _DIMINISHING_THRESHOLD
            and self.last_delta_tokens < _DIMINISHING_THRESHOLD
        )

        if not is_diminishing and turn_tokens < self.budget_total * _COMPLETION_THRESHOLD:
            self.continuation_count += 1
            self.last_delta_tokens = delta_since_last
            self.last_global_turn_tokens = global_turn_tokens
            return {
                'action': 'continue',
                'nudge_message': self._nudge(pct, turn_tokens, self.budget_total),
                'continuation_count': self.continuation_count,
                'pct': pct,
                'turn_tokens': turn_tokens,
                'budget': self.budget_total,
            }

        if is_diminishing or self.continuation_count > 0:
            return {
                'action': 'stop',
                'completion_event': {
                    'continuation_count': self.continuation_count,
                    'pct': pct,
                    'turn_tokens': turn_tokens,
                    'budget': self.budget_total,
                    'diminishing_returns': is_diminishing,
                    'duration_ms': int((_time.time() - self.started_at) * 1000),
                },
            }

        return {'action': 'stop', 'completion_event': None}

    @staticmethod
    def _nudge(pct: int, turn_tokens: int, budget: int) -> str:
        return (
            f"Stopped at {pct}% of token target "
            f"({turn_tokens:,} / {budget:,}). Keep working \u2014 do not summarize."
        )

    def add(self, usage: dict[str, int] | None) -> None:
        """兼容旧接口 — 不做累计（check 用 global_turn_tokens 即时参数）。"""
        pass


# ═══════════════════════════════════════════════════════════
# Compaction 桩（→ Step 7）
# ═══════════════════════════════════════════════════════════

def autocompact(
    messages: list[dict],
    tracking: dict | None = None,
) -> tuple[list[dict] | None, dict | None]:
    """AutoCompact — 决策器。Step 7 实现。
    Returns: (compacted_messages_or_None, new_tracking)
    """
    return None, tracking


def context_collapse(messages: list[dict]) -> list[dict]:
    """Context collapse — 读时投影。Step 7 实现。"""
    return messages


def snip_messages(messages: list[dict]) -> tuple[list[dict], int]:
    """Snip — 删除空结果/被拒绝的 turn。Step 7 实现。"""
    return messages, 0


# ═══════════════════════════════════════════════════════════
# SubAgent 桩（→ Step 9）
# ═══════════════════════════════════════════════════════════

async def run_sub_agent(
    agent_type: str,
    prompt: str,
    messages: list[dict],
    tools: list[ToolDefinition],
    system_prompt: str,
) -> str:
    """Fork/SubAgent 执行。Step 9 实现。"""
    return f"[sub-agent] 桩: {agent_type} not implemented"


# ═══════════════════════════════════════════════════════════
# Persistence 桩（→ Step 8）
# ═══════════════════════════════════════════════════════════

def save_transcript(messages: list[dict], path: str | None = None) -> None:
    """JSONL 写入 transcript。Step 8 实现。"""
    pass


def load_transcript(path: str) -> list[dict]:
    """JSONL 读取 transcript。Step 8 实现。"""
    return []
