"""死循环检测器 — 3 层 + 4 级渐进干预。

层 1: 指纹哈希 + 滑动窗口 (30 次)
  同一工具+相同参数 ≥10→hint, ≥15→warning, ≥20→critical

层 2: 乒乓振荡
  A↔B 交替 ≥4→hint, ≥5→warning, ≥6→critical

层 3: 同结果停滞
  连续同结果 ≥3→hint, ≥4→warning, ≥5→critical

干预:
  HINT:    注入轻提示提醒模型换方法
  WARNING: 注入强提示 + block 被检测工具（换工具自动恢复）
  CRITICAL: 终止运行
  读工具(Read/Grep/Glob) 永远不被 block，避免误杀正常检查
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class LoopSeverity(str, Enum):
    NONE = "none"
    HINT = "hint"        # 轻提示：注入提醒，模型继续
    WARNING = "warning"  # 强提示 + 禁止当前工具 1 轮
    CRITICAL = "critical" # 彻底终止


@dataclass
class LoopAlert:
    severity: LoopSeverity
    detector: str
    message: str
    tool_name: str = ""
    count: int = 0


# ── 配置：3 级渐进阈值 ──
FINGERPRINT_WINDOW = 30
FINGERPRINT_HINT = 10      # 同调用出现 10 次 → 轻提示
FINGERPRINT_WARNING = 15   # 同调用出现 15 次 → 强提示 + block
FINGERPRINT_CRITICAL = 20  # 同调用出现 20 次 → 终止

PINGPONG_HINT = 4          # 交替 4 对 → 轻提示
PINGPONG_WARNING = 5       # 交替 5 对 → 强提示 + block
PINGPONG_CRITICAL = 6      # 交替 6 对 → 终止
PINGPONG_UNIQUE = 2        # 只有 2 个不同工具才构成乒乓

STALENESS_HINT = 3         # 同结果连续 3 次 → 轻提示
STALENESS_WARNING = 4      # 同结果连续 4 次 → 强提示 + block
STALENESS_CRITICAL = 5     # 同结果连续 5 次 → 终止

# 读工具：永不 block（正常检查不应误杀），但检测仍运行
READ_ONLY_TOOLS = {"Read", "Grep", "Glob"}
# layer 3 目标：所有工具都检测同结果停滞
ALL_STALE_TOOLS = {"Read", "Grep", "Glob", "Bash"}


class LoopDetector:
    ""

    def __init__(self):
        self._fingerprints: list[str] = []     # 滑动窗口
        self._tool_names: list[str] = []       # 工具调用顺序(乒乓检测)
        self._readonly_results: dict[str, str] = {}  # 只读工具的最近一次结果
        self._readonly_streak = 0              # 连续只读无进展次数
        self._blocked_tools: set[str] = set()  # 被 warning 级禁用的工具
        self._last_tool: str = ""              # 上一轮被检测的工具（用于 auto-recover）

    def check(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        result: str,
    ) -> LoopAlert:
        """检查一次工具调用是否触发循环检测。

        每次工具执行后调用。返回最严重的 alert。
        优先级: critical > warning > hint > none
        """
        severity_order = {
            LoopSeverity.CRITICAL: 3,
            LoopSeverity.WARNING: 2,
            LoopSeverity.HINT: 1,
            LoopSeverity.NONE: 0,
        }
        most_severe = LoopAlert(severity=LoopSeverity.NONE, detector="none", message="")

        for alert in [
            self._check_fingerprint(tool_name, tool_input),
            self._check_pingpong(tool_name),
            self._check_same_result_staleness(tool_name, result),
        ]:
            if severity_order[alert.severity] >= severity_order[LoopSeverity.CRITICAL]:
                return alert  # critical: 立即返回
            if severity_order[alert.severity] > severity_order[most_severe.severity]:
                most_severe = alert

        return most_severe

    def block_tool(self, tool_name: str) -> None:
        """禁用一个工具（warning 级干预）。读工具永不 block。"""
        if tool_name not in READ_ONLY_TOOLS:
            self._blocked_tools.add(tool_name)

    def auto_recover_block(self, current_tools: list[str]) -> None:
        """如果模型换了工具（不再使用被禁工具），自动恢复。
        只检查这一轮的工具列表中有没有已 block 的 — 如果有新工具出现，
        删除被替换掉的 block。
        """
        if not self._blocked_tools:
            return
        current_set = set(current_tools)
        # 如果当前调用的工具中没有任何被禁工具 → 全部恢复
        if not (current_set & self._blocked_tools):
            self._blocked_tools.clear()

    def is_blocked(self, tool_name: str) -> bool:
        """检查某个工具是否被禁止。"""
        return tool_name in self._blocked_tools

    # ── 层 1: fingerprint ──

    def _check_fingerprint(
        self, tool_name: str, tool_input: dict,
    ) -> LoopAlert:
        fp = self._make_fingerprint(tool_name, tool_input)
        self._fingerprints.append(fp)

        if len(self._fingerprints) > FINGERPRINT_WINDOW:
            self._fingerprints = self._fingerprints[-FINGERPRINT_WINDOW:]

        count = self._fingerprints.count(fp)

        if count >= FINGERPRINT_CRITICAL:
            return LoopAlert(
                severity=LoopSeverity.CRITICAL,
                detector="fingerprint",
                message=(
                    f"Tool '{tool_name}' called {count} times with same args "
                    f"in last {FINGERPRINT_WINDOW} calls. "
                    f"Likely an infinite loop — stopping."
                ),
                tool_name=tool_name,
                count=count,
            )
        if count >= FINGERPRINT_WARNING:
            return LoopAlert(
                severity=LoopSeverity.WARNING,
                detector="fingerprint",
                message=f"Tool '{tool_name}' called {count} times with same args.",
                tool_name=tool_name,
                count=count,
            )
        if count >= FINGERPRINT_HINT:
            return LoopAlert(
                severity=LoopSeverity.HINT,
                detector="fingerprint",
                message=f"Tool '{tool_name}' called {count} times with same args.",
                tool_name=tool_name,
                count=count,
            )
        return LoopAlert(severity=LoopSeverity.NONE, detector="fingerprint", message="")

    # ── 层 2: ping-pong ──

    def _check_pingpong(self, tool_name: str) -> LoopAlert:
        self._tool_names.append(tool_name)
        if len(self._tool_names) > FINGERPRINT_WINDOW:
            self._tool_names = self._tool_names[-FINGERPRINT_WINDOW:]
        self._last_tool = tool_name

        if len(set(self._tool_names)) != PINGPONG_UNIQUE:
            return LoopAlert(severity=LoopSeverity.NONE, detector="pingpong", message="")

        pairs = 0
        for i in range(1, len(self._tool_names)):
            if self._tool_names[i] != self._tool_names[i - 1]:
                pairs += 1

        names = list(set(self._tool_names))
        if pairs >= PINGPONG_CRITICAL:
            return LoopAlert(
                severity=LoopSeverity.CRITICAL,
                detector="pingpong",
                message=f"Ping-pong oscillation: {' ↔ '.join(names)} alternating {pairs} times. Stopping.",
                tool_name=tool_name,
                count=pairs,
            )
        if pairs >= PINGPONG_WARNING:
            return LoopAlert(
                severity=LoopSeverity.WARNING,
                detector="pingpong",
                message=f"Ping-pong: {' ↔ '.join(names)} alternating {pairs} times.",
                tool_name=tool_name,
                count=pairs,
            )
        if pairs >= PINGPONG_HINT:
            return LoopAlert(
                severity=LoopSeverity.HINT,
                detector="pingpong",
                message=f"Ping-pong pattern forming: {' ↔ '.join(names)} alternating {pairs} times.",
                tool_name=tool_name,
                count=pairs,
            )
        return LoopAlert(severity=LoopSeverity.NONE, detector="pingpong", message="")

    # ── 层 3: readonly staleness ──

    def _check_same_result_staleness(
        self, tool_name: str, result: str,
    ) -> LoopAlert:
        key = f"{tool_name}.{_shard(result)}"
        if key in self._readonly_results:
            self._readonly_streak += 1
        else:
            self._readonly_results = {key: result[:200]}
            self._readonly_streak = 0

        if self._readonly_streak >= STALENESS_CRITICAL:
            return LoopAlert(
                severity=LoopSeverity.CRITICAL,
                detector="staleness",
                message=f"'{tool_name}' returned same result {self._readonly_streak} times in a row. Agent is stuck — stopping.",
                tool_name=tool_name,
                count=self._readonly_streak,
            )
        if self._readonly_streak >= STALENESS_WARNING:
            return LoopAlert(
                severity=LoopSeverity.WARNING,
                detector="staleness",
                message=f"'{tool_name}' returned same result {self._readonly_streak} times in a row.",
                tool_name=tool_name,
                count=self._readonly_streak,
            )
        if self._readonly_streak >= STALENESS_HINT:
            return LoopAlert(
                severity=LoopSeverity.HINT,
                detector="staleness",
                message=f"'{tool_name}' returned same result {self._readonly_streak} times in a row.",
                tool_name=tool_name,
                count=self._readonly_streak,
            )
        return LoopAlert(severity=LoopSeverity.NONE, detector="staleness", message="")

    # ── helpers ──

    def _make_fingerprint(self, tool_name: str, tool_input: dict) -> str:
        """生成工具调用的哈希指纹。"""
        data = json.dumps({"name": tool_name, "input": tool_input}, sort_keys=True)
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    def reset(self) -> None:
        self._fingerprints.clear()
        self._tool_names.clear()
        self._readonly_results.clear()
        self._readonly_streak = 0
        self._blocked_tools.clear()
        self._last_tool = ""


def _shard(result: str) -> str:
    """截取结果的代表性部分作为分片键。"""
    return hashlib.sha256(result[:500].encode()).hexdigest()[:12]
