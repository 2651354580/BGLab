""

from __future__ import annotations

import os
import sys
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

# ── 开关 ──
TRACE = os.environ.get("BGLAB_TRACE", "0") == "1"

# ANSI
DIM = "\033[2m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
BLUE = "\033[34m"
RESET = "\033[0m"
BOLD = "\033[1m"

_STORE: dict[str, list["Span"]] = {}
_LOCK = threading.Lock()


@dataclass
class Span:
    checkpoint: str
    start_ns: int
    end_ns: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    caller: str = ""

    @property
    def elapsed_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1_000_000


def _caller_name() -> str:
    try:
        f = sys._getframe(3)
        return f"{f.f_code.co_name}:{f.f_lineno}"
    except (ValueError, AttributeError):
        return "<root>"


def checkpoint(name: str, **data: Any) -> None:
    """Record a named checkpoint with data. Async-safe."""
    if not TRACE:
        return
    ns = time.monotonic_ns()
    with _LOCK:
        _STORE.setdefault(name, []).append(
            Span(checkpoint=name, start_ns=ns, end_ns=ns, data=data, caller=_caller_name())
        )


class Tracker:
    """Per-phase timer + metrics. Use as context manager or explicit start/stop."""

    def __init__(self, phase: str, **meta: Any):
        self.phase = phase
        self.meta = meta
        self._start: int = 0
        self._end: int = 0

    def start(self) -> "Tracker":
        self._start = time.monotonic_ns()
        return self

    def stop(self, **extra: Any) -> float:
        self._end = time.monotonic_ns()
        elapsed_ms = (self._end - self._start) / 1_000_000
        if TRACE:
            with _LOCK:
                _STORE.setdefault(self.phase, []).append(
                    Span(
                        checkpoint=self.phase,
                        start_ns=self._start,
                        end_ns=self._end,
                        data={**self.meta, **extra, "elapsed_ms": elapsed_ms},
                        caller=_caller_name(),
                    )
                )
        return elapsed_ms

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()


# ── Per-phase quick-call shortcuts ──

def compaction_pre(messages: int, tokens: int) -> Tracker:
    t = Tracker("compaction", phase="pre", msg_count=messages, est_tokens=tokens)
    t.start()
    return t


def compaction_post(pre_tokens: int, post_tokens: int, saved: int) -> None:
    checkpoint("compaction", phase="post", pre_tokens=pre_tokens,
               post_tokens=post_tokens, saved=saved)


def llm_call_start(model: str, max_tokens: int, msg_count: int) -> Tracker:
    t = Tracker("llm_call", model=model, max_tokens=max_tokens, msg_count=msg_count)
    t.start()
    return t


def llm_call_end(usage: dict | None, stop_reason: str = "") -> None:
    if usage:
        checkpoint("llm_call", phase="end", input=usage.get("input_tokens", 0),
                   output=usage.get("output_tokens", 0), stop=stop_reason)


def tool_exec_start(tool: str, args_summary: str, turn: int) -> Tracker:
    t = Tracker("tool_exec", tool=tool, args=args_summary, turn=turn)
    t.start()
    return t


def tool_exec_end(tool: str, result_len: int, is_error: bool) -> None:
    checkpoint("tool_exec", phase="end", tool=tool, result_len=result_len, is_error=is_error)


def permission_check(tool: str, decision: str, reason: str) -> None:
    checkpoint("permission", tool=tool, decision=decision, reason=reason)


def stop_hooks_start(turn: int, msg_count: int) -> Tracker:
    t = Tracker("stop_hooks", turn=turn, msg_count=msg_count)
    t.start()
    return t


def loop_detector_result(detector: str, severity: str, tool: str) -> None:
    checkpoint("loop_detect", detector=detector, severity=severity, tool=tool)


def memory_extraction(written: int, new_msgs: int, existing: int) -> None:
    checkpoint("memory_extraction", written=written, new_msgs=new_msgs, existing=existing)


def compact_boundary(action: str, pre_tokens: int = 0, messages_compacted: int = 0,
                     messages_kept: int = 0, has_relink: bool = False) -> None:
    checkpoint("compact_boundary", action=action, pre_tokens=pre_tokens,
               messages_compacted=messages_compacted, messages_kept=messages_kept,
               has_relink=has_relink)


def transcript_save(session_id: str, msg_count: int, has_boundary: bool = False) -> None:
    checkpoint("transcript_save", session_id=session_id, msg_count=msg_count,
               has_boundary=has_boundary)


def transcript_load(path: str, msg_count: int, has_boundary: bool = False,
                    has_relink: bool = False) -> None:
    checkpoint("transcript_load", path=path, msg_count=msg_count,
               has_boundary=has_boundary, has_relink=has_relink)


# ── Bulk readout ──

def flush_traces() -> str:
    """Return all trace data as formatted text. Clears store."""
    with _LOCK:
        if not _STORE:
            return ""
        sep = "=" * 60
        lines = [f"\n{CYAN}{BOLD}{sep}{RESET}",
                 f"{CYAN}[TRACE] {len(_STORE)} checkpoints{RESET}",
                 f"{DIM}{'-' * 60}{RESET}"]
        for name, spans in _STORE.items():
            count = len(spans)
            total_ms = sum(s.elapsed_ms for s in spans)
            max_elapsed = max(s.elapsed_ms for s in spans)
            lines.append(f"  {BOLD}{name}{RESET}: {count} calls "
                        f"| total {total_ms:.0f}ms | max {max_elapsed:.0f}ms")
            if spans and spans[0].data:
                keys = ", ".join(f"{k}={v}" for k, v in spans[0].data.items())
                lines.append(f"    {DIM}{keys}{RESET}")
        lines.append(f"{CYAN}{sep}{RESET}")
        _STORE.clear()
        return "\n".join(lines)


def traces_summary() -> dict[str, dict[str, int | float]]:
    """Return structured trace summary."""
    with _LOCK:
        result: dict[str, dict[str, int | float]] = {}
        for name, spans in _STORE.items():
            result[name] = {"count": len(spans), "total_ms": round(sum(s.elapsed_ms for s in spans), 1)}
        return result
