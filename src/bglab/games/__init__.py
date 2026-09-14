"""bglab games — persistent browser-driven board-game sessions.

当前支持:
  - Splendor（璀璨宝石）

架构:
  浏览器权威状态 → WebSocket 按 pid 直达持久 AI → BgAct 立即提交到浏览器
  → AI 独立完成 BgPlan/战报。Leader 只负责启动、关闭、恢复和 TUI 状态。
"""
