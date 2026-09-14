"""Hooks 系统 — Stop hooks + Post-turn 后台任务。

对齐 query/stopHooks.ts handleStopHooks():
  ① 保存 cache-safe snapshot → side_question / post-turn forks 复用 prompt cache
  ② Prompt suggestion → 后台 LLM 生成下轮提示建议
  ③ Memory extraction → 后台 LLM 提取记忆写入 memory/
  ④ Auto-dream consolidation → 跨 session 记忆整合 (24h+5sessions 门控)
  ⑤ 用户自定义 Stop hooks → command 类型, exit码+JSON输出
"""

from bglab.hooks.stop_hooks import (
    handle_stop_hooks,
    StopHookConfig,
    StopHookResult,
    CacheSafeSnapshot,
)
from bglab.hooks.state import StopHooksState

__all__ = [
    "handle_stop_hooks",
    "StopHookConfig",
    "StopHookResult",
    "CacheSafeSnapshot",
    "StopHooksState",
]
