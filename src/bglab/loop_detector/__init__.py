"""工具死循环检测 — 3 层 + 渐进干预。

对齐业界最佳实践（loop-breaker / DeerFlow / guardrail-rs）:
  1. Fingerprint detector: 滑动窗口 + 哈希指纹, 同一工具+相同参数重复检测
  2. Ping-pong detector: A→B→A→B 振荡模式
  3. Same-result staleness detector: 连续同结果无进展

4 级渐进升级: HINT(轻提示) → WARNING(强提示+block) → CRITICAL(终止) → BLOCKED_TOOL(拒绝)

源码无专门 detector，这 3 层各自对齐分散在权限/压缩/恢复中的模式。
"""

from bglab.loop_detector.detector import LoopDetector, LoopSeverity, LoopAlert

__all__ = ["LoopDetector", "LoopSeverity", "LoopAlert"]
