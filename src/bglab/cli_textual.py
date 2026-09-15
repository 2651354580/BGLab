"""Textual-based REPL — fullscreen TUI for bglab.

Ink features ported (from ink-ui/):
  components/theme.ts          → Theme constants
  components/TaskBoard.tsx     → TaskStore + TaskBoard widget
  components/PlanModeBanner.tsx → PlanModeBanner widget
  components/StatusLine.tsx    → Enhanced StatusLine widget
  main.tsx                     → Divider, Pane, Byline, StatusIcon, ToolPill,
                                 ToolCard, Spinner (stall detection),
                                 SessionPicker, SlashPalette,
                                 Permission dialog (3-option), Toast,
                                 Welcome screen, HelpPane, CommandCard

Usage:
  python -m bglab.cli_textual
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import random
import sys
import time
import uuid
from dataclasses import dataclass, replace
from hashlib import sha1
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from rich.markup import escape, escape as rich_escape
from rich.markdown import Markdown
from rich.text import Text
from rich.theme import Theme as RichTheme
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.message import Message
from textual.theme import Theme as AppTheme
from textual.widgets import Input, Static


from bglab.engine import QueryEngine
from bglab.engine.error_policy import RUNTIME_FAILURE
from bglab.engine.deps import QueryDeps


logger = logging.getLogger("bglab.tui")
from bglab.game_tui_projection import (
    GamePresentationMetadata,
    GameReportProjection,
    project_game_report_records,
    project_game_reports,
)
from bglab.llm.client import load_env
from bglab.llm.provider_slots import (
    ProviderSlot,
    ProviderSlotPolicy,
    load_provider_slot_policy,
)
from bglab.llm.providers import PROVIDERS
from bglab.llm.types import (
    LoopEvent,
    LoopEventType,
    TerminalInfo,
    ToolResultBlock,
    ToolUseBlock,
)
from bglab.tools import build_registry
from bglab.tui.activity import ActivityLine, motion_enabled, shimmer_text
from bglab.tui_assets import BGLAB_WORDMARK_ANSI, GAME_ARTWORK
from bglab.tui_presentation import (
    PresentationReducer,
    ToolKey,
    ToolPresentationState,
    TurnKey,
)


async def submit_message(*, query_engine: QueryEngine, user_message: str, **_kwargs):
    """Injectable TUI adapter; production always uses the persistent engine."""

    async for event in query_engine.submit_message(user_message):
        yield event


_DEFAULT_TUI_SUBMIT_MESSAGE = submit_message

# ══════════════════════════════════════════════════════════════════════
# Theme — slate surfaces, blue focus and restrained warm status accents
# ══════════════════════════════════════════════════════════════════════

# Brand
ACCENT = "#9bbcff"
ACCENT_SOFT = "#768caf"

# Semantic roles
TEXT = "#e6e9f2"
TEXT_DIM = "#a0abc0"
TEXT_MUTED = "#78869f"
BG_SCREEN = "#141720"
BG_INPUT = "#1e2432"
BG_USER_MSG = "#232c3e"
BG_TOOL_PENDING = "#212a3b"
BG_TOOL_SUCCESS = "#233036"
BG_TOOL_ERROR = "#382a36"
BORDER = "#30394c"

# Status colors
SUCCESS = "#8ed6b0"
ERROR = "#f19b9b"
WARNING = "#e8c28c"
INFO = "#90bede"
PURPLE = "#b6a7d6"
INVERSE = "#171d2c"

# Backward-compat aliases for existing code
C_ORANGE = ACCENT
C_DIM = TEXT_DIM
C_CYAN = INFO
C_GREEN = SUCCESS
C_RED = ERROR
C_YELLOW = WARNING
C_PURPLE = PURPLE
C_TEXT = TEXT
C_BG = BG_SCREEN
C_SELECTION = "#354568"
C_INVERSE = INVERSE

MODE_BORDER = {
    "default": C_ORANGE,
    "plan": C_CYAN,
    "accept_edits": C_GREEN,
    "bypass": C_PURPLE,
}
MODE_LABEL = {
    "default": "default",
    "plan": "plan",
    "accept_edits": "accept-edits",
    "bypass": "bypass",
}

TOOL_BG = {
    "Read": C_CYAN, "Write": C_ORANGE, "Edit": C_YELLOW,
    "Bash": C_PURPLE, "Cmd": C_PURPLE, "PowerShell": C_PURPLE,
    "Grep": C_CYAN, "Glob": C_CYAN, "WebSearch": C_CYAN, "WebFetch": C_CYAN,
    "TaskCreate": C_GREEN, "TaskUpdate": C_GREEN, "TaskList": C_GREEN,
    "TodoWrite": C_GREEN, "AskUserQuestion": C_ORANGE,
    "Skill": C_CYAN, "Agent": C_ORANGE,
    "EnterPlanMode": C_CYAN, "ExitPlanMode": C_CYAN,
    "ToolSearch": C_DIM, "Sleep": C_DIM,
    "NotebookEdit": C_YELLOW, "TaskStop": C_RED, "TaskOutput": C_CYAN,
}

TOOL_ICON = {
    "Read": "📖", "Write": "✍", "Edit": "✏", "Bash": "$",
    "Grep": "/", "Glob": "*", "WebSearch": "🌐", "WebFetch": "🌐",
    "TaskCreate": "✚", "TaskUpdate": "✚", "TaskList": "☰", "TodoWrite": "☰",
    "AskUserQuestion": "?", "Skill": "★", "Agent": "◆",
    "EnterPlanMode": "◇", "ExitPlanMode": "◇",
    "Sleep": "z", "TaskStop": "■", "TaskOutput": "↩",
}

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
VERBS = [
    "Accomplishing", "Baking", "Brewing", "Calculating", "Churning",
    "Composing", "Computing", "Considering", "Contemplating", "Cooking",
    "Crafting", "Creating", "Crunching", "Deciphering", "Deliberating",
    "Generating", "Ideating", "Inferring", "Musing", "Perusing",
    "Pondering", "Processing", "Reasoning", "Reticulating", "Ruminating",
    "Searching", "Thinking", "Working", "Wrangling",
]
DONE_VERBS = ["Baked", "Brewed", "Churned", "Cogitated", "Cooked",
              "Crunched", "Sautéed", "Worked"]

WELCOME_MSG = """
[dim]Type /help for commands  ·  /bg start for games  ·  Ctrl+C to exit[/]
"""

MODES = ["default", "plan", "accept_edits"]

def _live_slash_commands():
    """Return the user-visible commands from the authoritative registry."""
    from bglab.slash_commands.registry import CommandRegistry

    return [
        command
        for command in CommandRegistry().list_commands()
        if command.user_invocable and not command.is_hidden
    ]

# ══════════════════════════════════════════════════════════════════════
# Utilities
# ══════════════════════════════════════════════════════════════════════

def fmt_ms(ms: float) -> str:
    s = round(ms / 1000)
    if s < 60: return f"{s}s"
    m = s // 60
    if m < 60: return f"{m}m {s % 60}s"
    h = m // 60
    return f"{h}h {m % 60}m"


def fmt_size(bytes_: int) -> str:
    if bytes_ < 1024: return f"{bytes_} B"
    if bytes_ < 1024 * 1024: return f"{bytes_ / 1024:.1f} KB"
    return f"{bytes_ / (1024 * 1024):.1f} MB"


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000: return f"{n / 1_000_000:.1f}M"
    if n >= 1_000: return f"{n / 1_000:.1f}K"
    return str(n)


def relative_time(iso: str) -> str:
    diff = time.time() - _parse_iso(iso)
    mins = int(diff / 60)
    if mins < 1: return "just now"
    if mins < 60: return f"{mins}m ago"
    hours = mins // 60
    if hours < 24: return f"{hours}h ago"
    days = hours // 24
    if days < 7: return f"{days}d ago"
    if days < 30: return f"{days // 7}w ago"
    return iso[:10]


def _parse_iso(iso: str) -> float:
    """Rough ISO parse — returns timestamp."""
    try:
        import datetime
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0


def short_path(p: str, n: int = 52) -> str:
    return p if len(p) <= n else "..." + p[-n:]


def arg_summary(tool: str, input_: Any) -> str:
    if not input_ or not isinstance(input_, dict):
        return ""
    a = input_
    parts = []
    for k, v in list(a.items())[:2]:
        s = str(v)
        if len(s) > 40: s = s[:37] + "..."
        parts.append(f"{k}={s}")
    result = ", ".join(parts)
    if len(a) > 2: result += ", …"
    return result


def preview(content: str) -> tuple[str, str | None]:
    """Return (text, truncated_hint)."""
    if not content: return ("", None)
    lines = content.split("\n")
    if len(lines) > 3:
        return ("\n".join(lines[:3]), f"… +{len(lines) - 3} lines")
    if len(content) > 200:
        return (content[:200] + "...", None)
    return (content, None)


def _extract_text(content) -> str:
    if isinstance(content, str): return content
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return str(content)


def _tool_display_name(name: str) -> str:
    return name


# ══════════════════════════════════════════════════════════════════════
# Task Store
# ══════════════════════════════════════════════════════════════════════

@dataclass
class Task:
    id: int
    subject: str
    description: str = ""
    status: str = "pending"  # pending | in_progress | completed | deleted
    active_form: str | None = None


TASK_STATUS_ICONS = {
    "pending": "○",
    "in_progress": "◔",
    "completed": "✓",
    "deleted": "✕",
}


class TaskStore:
    """Singleton task store"""

    def __init__(self):
        self._tasks: list[Task] = []
        self._next_id = 1

    @property
    def tasks(self) -> list[Task]:
        return list(self._tasks)

    @property
    def current_task(self) -> Task | None:
        for t in self._tasks:
            if t.status == "in_progress":
                return t
        return None

    def handle_tool_result(self, name: str, content: str) -> None:
        if not content:
            return
        lower = name.lower()
        if lower == "taskcreate":
            self._handle_create(content)
        elif lower == "taskupdate":
            self._handle_update(content)
        elif lower == "tasklist":
            self._handle_list(content)
        elif lower == "todowrite":
            self._handle_todo(content)

    def _handle_create(self, content: str) -> None:
        import re
        m = re.search(r"Task #(\d+) created:\s*(.+?)\s*\(status:\s*(\w+)\)", content)
        if not m:
            return
        tid = int(m.group(1))
        task = Task(id=tid, subject=m.group(2).strip(), status=m.group(3).lower())
        existing = [t for t in self._tasks if t.id == tid]
        if existing:
            idx = self._tasks.index(existing[0])
            self._tasks[idx] = task
        else:
            self._tasks.append(task)
            if tid >= self._next_id:
                self._next_id = tid + 1

    def _handle_update(self, content: str) -> None:
        import re
        m = re.search(r"Task #(\d+) updated:\s*(.*)", content)
        if not m:
            return
        tid = int(m.group(1))
        details = m.group(2)
        for t in self._tasks:
            if t.id == tid:
                sm = re.search(r"status\s*=\s*(\w+)", details)
                if sm:
                    t.status = sm.group(1).lower()
                    if t.status == "in_progress":
                        for other in self._tasks:
                            if other.id != tid and other.status == "in_progress":
                                other.status = "pending"
                am = re.search(r"activeForm\s*=\s*(\S+)", details)
                if am:
                    t.active_form = am.group(1)
                break

    def _handle_list(self, content: str) -> None:
        import re
        tasks: list[Task] = []
        for line in content.split("\n"):
            m = re.match(r"[○◔✓✕]\s*\[(\d+)\]\s*(.+?)\s*[—\-]\s*(\w+)", line)
            if not m:
                continue
            status_map = {"○": "pending", "◔": "in_progress", "✓": "completed", "✕": "deleted"}
            sid = int(m.group(1))
            status = status_map.get(m.group(0)[0], "pending")
            subj = m.group(2).strip()
            old = next((t for t in self._tasks if t.id == sid), None)
            tasks.append(Task(id=sid, subject=subj, status=status,
                             active_form=old.active_form if old else None,
                             description=old.description if old else ""))
        if tasks:
            self._tasks = tasks

    def _handle_todo(self, content: str) -> None:
        import re
        tasks: list[Task] = []
        for line in content.split("\n"):
            m = re.match(r"\[(.)\]\s+(.+)", line)
            if not m:
                continue
            marker = m.group(1)
            rest = m.group(2).strip()
            status = "pending"
            if marker in (">", "◔"): status = "in_progress"
            elif marker in ("x", "X", "✓"): status = "completed"
            elif marker in ("-", "✕"): status = "deleted"
            active_form = None
            subject = rest
            if status == "in_progress":
                words = rest.split()
                if len(words) > 1:
                    last = words[-1]
                    if last[0].isupper() or (last[0].islower() and any(c.isupper() for c in last[1:])):
                        active_form = last
                        subject = " ".join(words[:-1])
            tasks.append(Task(id=self._next_id, subject=subject,
                             status=status, active_form=active_form))
            self._next_id += 1
        if tasks:
            self._tasks = tasks

    def reset(self) -> None:
        self._tasks = []
        self._next_id = 1


# Module-level singleton
_task_store = TaskStore()


# ══════════════════════════════════════════════════════════════════════
# Base Widgets
# ══════════════════════════════════════════════════════════════════════

class _SafeStatic(Static):
    """Static that NEVER returns None from render()."""
    def render(self) -> str:
        r = super().render()
        return r if r is not None else " "


class AssistantText(Static):
    """Render model prose, lists and code with the standard Markdown renderer."""

    def __init__(self, source: str = "", **kwargs) -> None:
        super().__init__(" ", **kwargs)
        self.source = ""
        self.update_markdown(source)

    def update_markdown(self, source: str) -> None:
        if source == self.source:
            return
        self.source = source
        self.update(Markdown(source, code_theme="github-dark", hyperlinks=False))


class Divider(_SafeStatic):
    """Full-width horizontal rule with optional centered title — Ink's Divider."""

    def __init__(self, text: str = "", color: str | None = None, **kwargs):
        super().__init__("─", **kwargs)
        self._text = text
        self._color = color

    def on_mount(self) -> None:
        self.redraw()

    def redraw(self) -> None:
        try:
            w = self.size.width if (self.size and self.size.width > 0) else 80
        except Exception:
            w = 80
        w = max(8, w)
        color = self._color or C_DIM
        if self._text:
            s = f" {self._text} "
            l = max(0, (w - len(s)) // 2)
            r = w - l - len(s)
            line = "─" * l + s + "─" * max(0, r)
        else:
            line = "─" * w
        try:
            self.update(f"[{color}]{line}[/]")
        except Exception:
            pass

    def set_text(self, text: str) -> None:
        self._text = text
        self.redraw()


class Pane(_SafeStatic):
    """Bordered section with accent color — Ink's Pane."""

    def __init__(self, title: str = "", color: str = C_ORANGE, children_text: str = "", **kwargs):
        super().__init__(" ", **kwargs)
        self._title = title
        self._color = color
        self._children_text = children_text
        self._rendered = False

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        if self._rendered and not self._title and not self._children_text:
            return
        self._rendered = True
        lines = []
        if self._title:
            lines.append(f"[bold {self._color}]{self._title}[/]")
        if self._children_text:
            lines.append(self._children_text)
        if lines:
            self.update("\n".join(lines))


class StatusIcon(_SafeStatic):
    """Status indicator icon with color — Ink's StatusIcon."""

    ICONS = {
        "success": ("✓", C_GREEN),
        "error": ("✗", C_RED),
        "warning": ("⚠", C_YELLOW),
        "info": ("ℹ", C_CYAN),
        "pending": ("○", None),
    }

    def __init__(self, status: str = "info", with_space: bool = False, **kwargs):
        icon, color = self.ICONS.get(status, ("○", None))
        text = icon + (" " if with_space else "")
        style = f"[{color}]{text}[/]" if color else f"[dim]{text}[/]"
        super().__init__(style, **kwargs)


class ToolPill(_SafeStatic):
    """Colored tool name badge — Ink's ToolPill."""

    def __init__(self, name: str, is_error: bool = False, **kwargs):
        bg = TOOL_BG.get(name, C_DIM)
        text = f"[{C_INVERSE} on {bg}] {rich_escape(_tool_display_name(name))} [/]"
        super().__init__(text, **kwargs)


class Byline(_SafeStatic):
    """Dot-separated metadata line — Ink's Byline."""

    def __init__(self, parts: list[str], **kwargs):
        text = " · ".join(parts)
        super().__init__(f"[dim]{text}[/]", **kwargs)


# ══════════════════════════════════════════════════════════════════════
# PlanModeBanner
# ══════════════════════════════════════════════════════════════════════

class PlanModeBanner(_SafeStatic):
    """Shows plan mode indicator with attachments."""

    def __init__(self, mode: str = "default", attachments: list[str] | None = None, **kwargs):
        super().__init__(" ", **kwargs)
        self._mode = mode
        self._attachments = attachments or []

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self._refresh()

    def set_attachments(self, attachments: list[str]) -> None:
        self._attachments = attachments
        self._refresh()

    def _refresh(self) -> None:
        if self._mode != "plan":
            self.update(" ")
            return
        lines = [f"[bold {C_CYAN}]◇ Plan Mode — read-only, design only[/]"]
        for text in self._attachments:
            if len(text) > 200:
                text = text[:200] + "…"
            lines.append(f"[dim {C_CYAN}]{text}[/]")
        self.update("\n".join(lines))


# ══════════════════════════════════════════════════════════════════════
# TaskBoard
# ══════════════════════════════════════════════════════════════════════

class TaskBoard(_SafeStatic):
    """Task list display — Ink's TaskBoard component."""

    MAX_VISIBLE = 8

    def __init__(self, **kwargs):
        super().__init__(" ", **kwargs)
        self._store = _task_store

    def refresh(self) -> None:
        tasks = self._store.tasks
        if not tasks:
            self.update(" ")
            return

        # Sort: in_progress first, then by id descending
        sorted_tasks = sorted(tasks, key=lambda t: (
            0 if t.status == "in_progress" else
            1 if t.status != "completed" and t.status != "deleted" else 2,
            -t.id
        ))

        # Truncation logic
        if len(sorted_tasks) <= self.MAX_VISIBLE:
            visible = sorted_tasks
            truncated = 0
        else:
            in_progress = [t for t in sorted_tasks if t.status == "in_progress"]
            rest = [t for t in sorted_tasks if t.status not in ("in_progress", "completed", "deleted")]
            completed = [t for t in sorted_tasks if t.status in ("completed", "deleted")]
            combined = in_progress + rest
            if len(combined) >= self.MAX_VISIBLE:
                visible = combined[:self.MAX_VISIBLE]
                truncated = len(sorted_tasks) - self.MAX_VISIBLE
            else:
                slots_left = self.MAX_VISIBLE - len(combined)
                visible = combined + completed[:slots_left]
                truncated = len(sorted_tasks) - len(visible)

        lines = []
        for task in visible:
            icon = TASK_STATUS_ICONS.get(task.status, "○")
            is_active = task.status == "in_progress"
            color = C_ORANGE if is_active else (C_GREEN if task.status == "completed" else
                                                 C_RED if task.status == "deleted" else C_DIM)
            bold_open = "[bold]" if is_active else ""
            bold_close = "[/]" if is_active else ""
            lines.append(
                f"  [{color}]{icon}[/] [{color}]{bold_open}#{task.id}{bold_close}[/] [{color}]{task.subject}[/]"
            )
        if truncated:
            lines.append(f"  [dim]{truncated} more task{'s' if truncated != 1 else ''}...[/]")

        self.update("\n".join(lines))


# ══════════════════════════════════════════════════════════════════════
# Enhanced StatusLine
# ══════════════════════════════════════════════════════════════════════

class StatusLine(_SafeStatic):
    """Status bar — model · mode · turns · tokens · elapsed."""

    def __init__(self, model: str = "", mode: str = "default", **kwargs):
        super().__init__(" ", **kwargs)
        self.model = model
        self.mode = mode
        self.total_input = 0
        self.total_output = 0
        self.total_cache_hit = 0
        self.total_cache_miss = 0
        self.total_cost = 0.0
        self.total_turns = 0
        self.start_time = time.time()
        self._task_count = 0
        self._in_progress_task: str | None = None

    def show(self, turn: int, usage: dict | None = None) -> None:
        if usage:
            self.total_input += usage.get("input_tokens", 0)
            self.total_output += usage.get("output_tokens", 0)
            self.total_cache_hit += usage.get("cache_hit_tokens", 0)
            self.total_cache_miss += usage.get("cache_miss_tokens", 0)
        self.total_turns = max(self.total_turns, turn)
        self._refresh()

    def _refresh(self) -> None:
        elapsed = int(time.time() - self.start_time)
        elapsed_s = f"{elapsed}s" if elapsed < 60 else f"{elapsed // 60}m {elapsed % 60}s"

        parts = [
            self.model,
            MODE_LABEL.get(self.mode, self.mode),
            f"turn {self.total_turns}",
            f"{fmt_tokens(self.total_input)}↓ {fmt_tokens(self.total_output)}↑",
            elapsed_s,
        ]

        cache_total = self.total_cache_hit + self.total_cache_miss
        if cache_total > 0:
            hr = self.total_cache_hit * 100 / cache_total
            parts.append(f"cache {hr:.0f}%")

        if self._task_count > 0:
            parts.append(f"{self._task_count} tasks")

        if self._in_progress_task:
            parts.append(f"↻ {self._in_progress_task}")

        text = " · ".join(parts)
        self.update(f"[dim]{text}[/]")

    def update_task_info(self, task_count: int, in_progress: str | None = None) -> None:
        self._task_count = task_count
        self._in_progress_task = in_progress
        self._refresh()


# ══════════════════════════════════════════════════════════════════════
# Toast — ephemeral notification (new, from Ink concept)
# ══════════════════════════════════════════════════════════════════════

class Toast(_SafeStatic):
    """Ephemeral notification that auto-removes after a timeout."""

    def __init__(self, text: str, color: str = C_ORANGE, timeout_ms: int = 1500, **kwargs):
        super().__init__(f"[bold {color}]● {text}[/]", **kwargs)
        self._timeout_ms = timeout_ms
        self._timer = None

    def on_mount(self) -> None:
        self._timer = self.set_timer(self._timeout_ms / 1000, self._remove)

    def _remove(self) -> None:
        try:
            self.remove()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════════
# ToolCard — OpenClaw-style boxed tool display
# ══════════════════════════════════════════════════════════════════════

class ToolCard(_SafeStatic):
    """A bounded, reducer-owned tool lifecycle row."""

    can_focus = True

    class Toggle(Message):
        def __init__(self, tool_key: ToolKey) -> None:
            super().__init__()
            self.tool_key = tool_key
            self.tool_use_id = tool_key.tool_use_id

    def __init__(self, state: ToolPresentationState, **kwargs: object) -> None:
        self.state = state
        super().__init__(
            self._render_state(state),
            id=self._dom_id(state),
            **kwargs,
        )
        self.with_tooltip(self._card_tooltip(state))

    @staticmethod
    def _dom_component(value: object) -> str:
        text = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value)).strip("-")
        return text[:48] or "x"

    def _dom_id(self, state: ToolPresentationState) -> str:
        raw = (
            f"{state.key.turn.session_id}:{state.key.turn.turn_id}:"
            f"{state.tool_use_id}"
        )
        safe = "-".join(
            self._dom_component(part)
            for part in (
                state.key.turn.session_id,
                state.key.turn.turn_id,
                state.tool_use_id,
            )
        )
        return f"tool-{safe}-{sha1(raw.encode('utf-8')).hexdigest()[:10]}"

    def _render_state(self, state: ToolPresentationState) -> str:
        icon = {
            "running": "⟳", "approval": "?", "success": "✓",
            "failure": "✕", "declined": "⊘", "canceled": "■",
        }.get(state.state, "·")
        body = state.output.detail if state.expanded else state.output.preview
        body_lines = body.splitlines()
        preview_clipped = not state.expanded and len(body_lines) > 3
        if preview_clipped:
            body_lines = body_lines[:3]
        lines = [
            f"{icon} {rich_escape(_tool_display_name(state.name))}  "
            f"[dim]{rich_escape(state.target_summary)}[/]",
        ]
        if body:
            lines.extend(f"  {rich_escape(line)}" for line in body_lines)
        if not state.expanded and (
            preview_clipped or state.output.omitted_lines or state.output.omitted_chars
        ):
            lines.append("  [dim]另有内容，Enter 或点击展开[/]")
        if state.error_summary:
            lines.append(f"  [red]{rich_escape(state.error_summary[:512])}[/]")
        return "\n".join(lines)

    def _card_tooltip(self, state: ToolPresentationState) -> str:
        action = "折叠" if state.expanded else "展开"
        return f"{_tool_display_name(state.name)} · Enter/单击{action}"

    def update_state(self, state: ToolPresentationState) -> None:
        self.state = state
        self.update(self._render_state(state))
        self.with_tooltip(self._card_tooltip(state))

    def action_toggle_expanded(self) -> None:
        expanded = not self.state.expanded
        self.update_state(replace(
            self.state,
            expanded=expanded,
            output=replace(self.state.output, expanded=expanded),
        ))
        self.post_message(self.Toggle(self.state.key))

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter" and self.has_focus:
            event.stop()
            event.prevent_default()
            self.action_toggle_expanded()

    def on_click(self, event: events.Click) -> None:
        event.stop()
        event.prevent_default()
        self.action_toggle_expanded()


# ══════════════════════════════════════════════════════════════════════
# ThinkingLine — spinner with stall detection
# ══════════════════════════════════════════════════════════════════════

class ThinkingLine(_SafeStatic):
    """Animated spinner with stall detection — Ink's Spinner."""

    def __init__(self, **kwargs):
        super().__init__(" ", **kwargs)
        self._frame = 0
        self._running = False
        self._thinking_text = ""
        self._elapsed_ms = 0
        self._in_tokens = 0
        self._out_tokens = 0
        self._stalled_ms = 0
        self._last_activity = time.time()
        self._start_time = time.time()

    def show(self, text: str = "") -> None:
        self._thinking_text = text or random.choice(VERBS)
        self._start_time = time.time()
        self._last_activity = time.time()
        self._running = True
        self._update()

    def _update(self) -> None:
        if not self._running:
            return
        self._elapsed_ms = (time.time() - self._start_time) * 1000
        self._stalled_ms = (time.time() - self._last_activity) * 1000

        stall = max(0, min(1, (self._stalled_ms - 3000) / 4000)) if self._stalled_ms > 3000 and self._out_tokens == 0 else 0

        # Color lerp: purple accent → magenta warning when stalling
        if stall > 0:
            a_r, a_g, a_b = 0xD8, 0x6C, 0xFF
            r_r, r_g, r_b = 0xF3, 0x8B, 0xD8
            m = lambda a, b: round(a + (b - a) * stall)
            color = f"#{m(a_r, r_r):02x}{m(a_g, r_g):02x}{m(a_b, r_b):02x}"
        else:
            color = ACCENT

        frame = SPINNER_FRAMES[self._frame % len(SPINNER_FRAMES)]
        self._frame += 1

        parts = [f"[{color}]{frame} {self._thinking_text}…[/]"]
        if self._elapsed_ms > 0:
            parts.append(f"[dim]{fmt_ms(self._elapsed_ms)}[/]")
        if self._in_tokens or self._out_tokens:
            parts.append(f"[dim]{self._in_tokens}+{self._out_tokens} tk[/]")
        if stall > 0.4:
            parts.append(f"[{C_RED}]slow[/]")

        text = " · ".join(parts)
        self.update(f"  {text}")

    def hide(self) -> None:
        self._running = False
        try:
            self.remove()
        except Exception:
            pass

    def set_activity(self) -> None:
        self._last_activity = time.time()

    def set_tokens(self, in_tokens: int = 0, out_tokens: int = 0) -> None:
        self._in_tokens = in_tokens
        self._out_tokens = out_tokens


# ══════════════════════════════════════════════════════════════════════
# Legacy widget aliases (maintain compatibility)
# ══════════════════════════════════════════════════════════════════════

class TurnCompletion(Divider):
    """Turn completion line with verb + duration + tokens."""

    def show(self, elapsed_ms: float, usage: dict | None = None) -> None:
        verb = random.choice(DONE_VERBS)
        s = elapsed_ms / 1000
        dur = f"{s:.0f}s" if s < 60 else f"{int(s // 60)}m {int(s % 60)}s"
        parts = [f"{verb} for {dur}"]
        if usage:
            it = usage.get("input_tokens", 0)
            ot = usage.get("output_tokens", 0)
            ch = usage.get("cache_hit_tokens", 0)
            cm = usage.get("cache_miss_tokens", 0)
            cache_total = ch + cm
            if it or ot:
                parts.append(f"{it}+{ot} tokens")
            if cache_total > 0:
                hr = ch * 100 / cache_total
                parts.append(f"cache {hr:.0f}%")
        self._text = "  " + " · ".join(parts) + "  "
        self.redraw()


class ToolUseLine(_SafeStatic):
    """Tool use line — cyan tool name + args."""

    def __init__(self, **kwargs):
        super().__init__(" ", **kwargs)

    def show(self, name: str, args: dict) -> None:
        parts = []
        for k, v in list(args.items())[:2]:
            s = str(v)
            parts.append(f"{k}={s[:40]}{'...' if len(s) > 40 else ''}")
        summary = ", ".join(parts) + (", ..." if len(args) > 2 else "")
        self.update(f"\n  [{C_CYAN}]{rich_escape(_tool_display_name(name))}[/]({rich_escape(summary)})")


class ToolResultLine(_SafeStatic):
    """Tool result line — ✓/✗ with truncated content."""

    def __init__(self, **kwargs):
        super().__init__(" ", **kwargs)

    def show(self, content: str, is_error: bool) -> None:
        prefix = "✗" if is_error else "✓"
        lines = content.split("\n")[:3]
        if len(content.split("\n")) > 3: lines.append("...")
        shown = "\n".join(lines)
        if len(shown) > 300: shown = shown[:300] + "..."
        self.update(f"  {prefix} {shown}")


class MessagesArea(VerticalScroll):
    can_focus = False


class InputArea(Container):
    pass


# ══════════════════════════════════════════════════════════════════════
# BG Game Status — 棋盘仅存在于网页，TUI 只显示阻塞状态与战报
# ══════════════════════════════════════════════════════════════════════


@dataclass
class GamePresentationSession:
    """Exact per-game presentation binding and its owned poll task."""

    game_id: str
    generation: int
    game_dir: Path
    manifest_path: Path
    manifest_mtime_ns: int
    report_path: Path
    report_cursor: int
    report_line_count: int
    report_file_size: int
    report_mtime_ns: int
    report_records: list[dict]
    report_cache_loaded: bool
    report_cursor_anchor: bytes
    projection_cache: GameReportProjection | None
    title: str
    status: str
    http_port: int | None
    url: str | None
    player_types: tuple[str, ...]
    ai_seats: tuple[int, ...]
    poll_task: asyncio.Task | None

    @property
    def report_dir(self) -> Path:
        return self.game_dir


BG_HINTS = f" [bold {ACCENT}]/bg stop[/] 关闭网页游戏并返回 Code Agent"

class BgGameView(Static):
    def __init__(self):
        super().__init__("", id="bg-pane")
        self._last_line = 0
        self._entries: list[str] = []
        self._game_id = ""
        self._title = "桌游"
        self._url = "http://localhost:8080"
        self._status = "正在启动"
        self._turn = "?"
        self._player = "?"

    def poll(self) -> bool:
        """Read live events file and update display. Returns True if new content."""
        from pathlib import Path
        live = Path.home() / ".bglab" / "games" / ".live_events"
        if not live.exists():
            return False
        try:
            lines = [
                line for line in live.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except Exception:
            return False
        if len(lines) < self._last_line:
            # A paused-session restart intentionally truncates this shared stream.
            self._last_line = 0
        if len(lines) <= self._last_line:
            return False
        new_lines = lines[self._last_line:]
        self._last_line = len(lines)
        parts = []
        for raw in new_lines[-30:]:
            try:
                evt = __import__("json").loads(raw)
            except Exception:
                continue
            event_type = evt.get("type")
            if event_type == "game_started":
                self._game_id = str(evt.get("game_id", ""))
                self._title = str(evt.get("title", self._title) or self._title)
                self._url = str(evt.get("url", self._url) or self._url)
                self._status = "正在启动"
                parts.append(f"  [bold {WARNING}]正在启动游戏服务与网页[/]")
            elif event_type == "game_resumed":
                self._game_id = str(evt.get("game_id", self._game_id))
                self._title = str(evt.get("title", self._title) or self._title)
                self._url = str(evt.get("url", self._url) or self._url)
                self._status = "已恢复 · 等待网页状态"
                parts.append(f"  [bold {SUCCESS}]已恢复存档 {rich_escape(str(evt.get('turn_id', '')))}[/]")
            elif event_type == "state_snapshot":
                self._turn = str(evt.get("turn", "?"))
                self._player = str(evt.get("current_player", "?"))
                self._status = f"游戏已启动 · 回合 {self._turn} · 当前 P{self._player}"
                parts.append(f"  [bold {SUCCESS}]游戏已启动 · 已确认快照 · 回合 {self._turn} · 当前 P{self._player}[/]")
            elif event_type == "action_committed":
                pid = evt.get("pid", "?")
                action = evt.get("action", {})
                parts.append(
                    f"  [{INFO}]P{pid}[/] 已执行 [bold]{rich_escape(str(action.get('type', '?')))}[/]"
                )
            elif event_type == "tool_result":
                pid = evt.get("pid", "?")
                tool = rich_escape(str(evt.get("tool", "?")))
                if evt.get("ok", False):
                    parts.append(f"  [{INFO}]P{pid}[/] → {tool} [bold {SUCCESS}]✓[/]")
                else:
                    parts.append(f"  [{INFO}]P{pid}[/] → {tool} [bold {C_RED}]✗[/]")
            elif event_type == "game_chat":
                sender = evt.get("from_pid", "?")
                target = evt.get("to_pid", "?")
                message = rich_escape(str(evt.get("message", ""))[:180])
                parts.append(f"  [dim]P{sender} → P{target}[/] {message}")
            elif event_type == "turn_report":
                pid = evt.get("pid", "?")
                txt = rich_escape(str(evt.get("text", ""))[:240])
                parts.append(f"  [dim {PURPLE}]P{pid} 战报[/] {txt}")
            elif event_type == "game_paused":
                pid = evt.get("pid", "?")
                issue = evt.get("issue") if isinstance(evt.get("issue"), dict) else {}
                if evt.get("status") == "paused_api_error" or issue:
                    title = rich_escape(str(issue.get("title") or "API 请求失败"))
                    self._status = f"已暂停 · {title}"
                    parts.append(f"  [bold {C_RED}]P{pid} 对局暂停 · {title}[/]")
                    parts.append(
                        f"  [bold {WARNING}]输入“继续”或 /bg retry，从当前局面重试[/]"
                    )
                else:
                    self._status = "已暂停 · 对局错误"
                    parts.append(f"  [bold {C_RED}]P{pid} 对局暂停[/]")
            elif event_type == "game_finished":
                self._status = "已结束 · 请选择再来一局或结束"
                winner = evt.get("winner", "?")
                parts.append(f"  [bold {SUCCESS}]对局结束 · 胜者 P{winner}[/]")
                parts.append(f"  [bold {WARNING}]输入“再来一局”创建同阵容新对局；输入“结束”关闭[/]")
            elif event_type == "frontend_connected":
                self._status = "网页已连接 · 等待权威首帧"
                parts.append(f"  [dim {SUCCESS}]网页已连接，等待 Adapter 权威首帧[/]")
            elif event_type == "frontend_disconnected" and not evt.get("stopping"):
                self._status = "网页已断开 · 等待重连"
                parts.append(f"  [bold {C_YELLOW}]网页断开，保留最近确认快照并等待重连[/]")
            elif event_type == "game_stopped":
                self._status = "正在保存并关闭"
                parts.append(f"  [bold {WARNING}]正在保存并关闭：snapshot、manifest、AI 状态与恢复元数据[/]")
            elif event_type == "game_closed":
                self._status = "游戏已关闭"
                parts.append(f"  [bold {SUCCESS}]游戏已关闭：进程退出、端口已释放[/]")
        if parts:
            self._entries.extend(parts)
            self._entries = self._entries[-12:]
            title = (
                f"[bold {ACCENT}]正在玩{rich_escape(self._title)}[/]  "
                f"[dim]{self._game_id} · {self._status} · {rich_escape(self._url)}[/]"
            )
            content = title + "\n" + "\n".join(self._entries) + "\n" + BG_HINTS
            self.update(content)
        return bool(parts)


@dataclass(frozen=True)
class GameTheme:
    """Terminal-safe identity for the registered BGLab games."""

    key: str
    title: str
    accent: str
    accent_soft: str
    artwork: str
    compact_title: str


GAME_THEMES = (
    GameTheme(
        key="splendor",
        title="璀璨宝石",
        accent="#d7b56d",
        accent_soft="#f1e1b7",
        artwork=GAME_ARTWORK["splendor"],
        compact_title="SPLENDOR",
    ),
    GameTheme(
        key="azul",
        title="花砖物语",
        accent="#51a8e8",
        accent_soft="#f2e9d5",
        artwork=GAME_ARTWORK["azul"],
        compact_title="AZUL",
    ),
    GameTheme(
        key="white-castle",
        title="姬路城",
        accent="#e9e4d3",
        accent_soft="#d7806f",
        artwork=GAME_ARTWORK["white-castle"],
        compact_title="THE WHITE CASTLE",
    ),
)

GENERIC_GAME_THEME = GameTheme(
    key="generic",
    title="桌游",
    accent="#948ba8",
    accent_soft="#c4b5fd",
    artwork="""BGLAB GAME""",
    compact_title="BGLAB GAME",
)


def _game_theme(title: str) -> GameTheme:
    normalized = title.strip().casefold()
    for theme in GAME_THEMES:
        aliases = {theme.title.casefold(), theme.key, theme.compact_title.casefold()}
        if theme.key == "white-castle":
            aliases.update({"白城堡", "the-white-castle", "white castle"})
        if normalized in aliases:
            return theme
    return GENERIC_GAME_THEME


def _short_game_id(game_id: str) -> str:
    """Return the deterministic user-facing game identity fragment."""
    if not game_id:
        return ""
    return game_id if len(game_id) <= 8 else game_id[:8]


class GameWordmark(Static):
    """Existing game artwork, animated without generating gameplay events."""

    def __init__(self) -> None:
        super().__init__("", id="game-wordmark", markup=False)
        self._theme = GENERIC_GAME_THEME
        self._active = False
        self._quiet = False
        self._frame = 0
        self._available_size: tuple[int, int] | None = None

    def on_mount(self) -> None:
        self.set_interval(0.12, self._tick)

    def set_state(self, title: str, *, active: bool, quiet: bool) -> None:
        theme = _game_theme(title)
        if (self._theme, self._active, self._quiet) == (theme, active, quiet):
            return
        if self._theme != theme:
            self._frame = 0
        self._theme, self._active, self._quiet = theme, active, quiet
        self._refresh()

    def _tick(self) -> None:
        if self._active and motion_enabled(self):
            self._frame = (self._frame + 1) % 60
            self._refresh()

    def on_resize(self, event: events.Resize) -> None:
        # Read the settled content width even when animation is disabled.
        self.call_after_refresh(self._refresh)

    def fit(self, width: int, height: int) -> None:
        self._available_size = (width, height)
        self._refresh()

    def _refresh(self) -> None:
        available_width, height = self._available_size or (
            (self.parent.content_size.width, self.parent.content_size.height) if self.parent else (0, 0)
        )
        # Match the wordmark's CSS width/padding while a resize is settling.
        limit = max(1, min(68, available_width) - 6)
        width = min(self.content_size.width or limit, limit)
        art = self._theme.artwork
        if self._quiet or height < 14 or max(Text(line).cell_len for line in art.splitlines()) > width:
            art = self._theme.compact_title
        art_width = max(Text(line).cell_len for line in art.splitlines())
        inset = " " * max(0, (width - art_width) // 2)
        lines = [inset + line for line in art.splitlines()]
        self.update(shimmer_text(
            "\n".join(lines), self._frame,
            base=self._theme.accent, highlight=self._theme.accent_soft,
            active=self._active and motion_enabled(self),
        ))


class GameHero(Static):
    """Small, user-facing game identity block.

    The browser owns the board.  The TUI intentionally keeps this block to
    the exact presentation contract and never renders session internals.
    """

    def __init__(self) -> None:
        super().__init__("", id="game-hero", markup=False)
        self._title = "桌游"
        self._game_id = ""
        self._status = "starting"
        self._url: str | None = None
        self._status_override: str | None = None
        self._compact = False
        self._player_count = 0
        self._notice: str | None = None
        self._frame = 0

    def on_mount(self) -> None:
        self.set_interval(0.12, self._tick)

    def _tick(self) -> None:
        if motion_enabled(self) and self._is_playing():
            self._frame = (self._frame + 1) % 60
            self._refresh()

    def _is_playing(self) -> bool:
        return not (
            self._status in {"ended", "finished", "completed", "stopped"}
            or self._status.startswith("paused")
            or self._status_override
            or self._notice
        )

    def set_state(
        self,
        *,
        title: str,
        game_id: str,
        status: str,
        url: str | None,
        player_count: int = 0,
    ) -> None:
        """Render the only supported Hero state shape.

        ``url=None`` is the preparing state.  It is important that the Hero
        does not invent a URL while the metadata adapter is still preparing a
        session.
        """
        self._title = title or "桌游"
        self._game_id = game_id
        self._status = status or "starting"
        self._url = url
        self._player_count = player_count
        self._status_override = "退出失败，请重试" if status == "退出失败，请重试" else None
        self._refresh()

    def set_shutdown_error(self) -> None:
        """Show the fixed, retryable shutdown state without provider details."""
        self._status_override = "退出失败，请重试"
        self._refresh()

    def set_notice(self, notice: str | None) -> None:
        self._notice = notice
        self._refresh()

    def set_compact(self, compact: bool) -> None:
        if self._compact == compact:
            return
        self._compact = compact
        if compact:
            self.add_class("compact")
        else:
            self.remove_class("compact")
        self._refresh()

    def on_resize(self, event: events.Resize) -> None:
        self._refresh()

    def _refresh(self) -> None:
        if self._url is None:
            lines = [f"正在准备 {self._title}", "游戏页面准备好后，会在这里显示地址。"]
        else:
            count = f" · {self._player_count} 人局" if self._player_count else ""
            prefix = "对局已结束" if self._status in {"ended", "finished", "completed"} else (
                "对局已暂停" if self._status.startswith("paused") or self._status == "stopped" else "正在游玩"
            )
            parts = urlsplit(self._url)
            display_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            lines = [f"{prefix} {self._title}{count}", "", f"打开棋盘  {display_url}"]
        if self._status_override:
            lines.append(self._status_override)
        elif self._status == "paused_api_error":
            lines.append("API 请求失败 · 输入“继续”或 /bg retry 重试")
        if self._notice:
            lines.append(self._notice)
        lines.append("/bg stop  退出对局")
        from textual.content import Content
        body = [rich_escape(line) for line in lines[1:]]
        if self._url:
            body[1] = f"[@click=app.open_game][underline {ACCENT}]{body[1]}[/][/]"
        moving = self._is_playing() and motion_enabled(self)
        heading = shimmer_text(lines[0], self._frame, base=TEXT, highlight=_game_theme(self._title).accent_soft, active=moving)
        heading.stylize("bold")
        if moving:
            heading.append(f"  {SPINNER_FRAMES[self._frame % len(SPINNER_FRAMES)]}", style=ACCENT)
        heading.append("\n")
        heading.append(Text.from_markup(f"[{TEXT_DIM}]{chr(10).join(body)}[/]"))
        self.update(Content.from_rich_text(heading))
        if self.parent:
            for wordmark in self.parent.query(GameWordmark):
                wordmark.set_state(self._title, active=self._is_playing(), quiet=bool(self._notice or self._status_override))


class GameActivity(VerticalScroll):
    """Full-width durable AI report projection for the active game."""

    def __init__(self) -> None:
        super().__init__(id="game-activity")
        self._projection = GameReportProjection("", (), (), ())

    def compose(self) -> ComposeResult:
        yield from self._rows()

    def set_reports(self, projection: GameReportProjection) -> None:
        """Render only the exact durable projection for this game."""
        if not isinstance(projection, GameReportProjection):
            raise TypeError("GameActivity.set_reports expects GameReportProjection")
        self._projection = projection
        if not self.is_mounted:
            return
        self.remove_children()
        self.mount(*self._rows())

    @staticmethod
    def _label(seat_index: int | None) -> str:
        # Persisted game seats are zero based, while the stable user-facing
        # label is one based.  This is a presentation conversion only; the
        # authoritative ``player_types[i]``/pid mapping remains untouched.
        if seat_index is None:
            return "AI"
        return f"AI {seat_index + 1}"

    def _rows(self) -> list[Static]:
        rows: list[Static] = []
        for entry in self._projection.entries:
            label = self._label(entry.seat_index)
            if entry.waiting:
                text = f"{label}  等待第一份战报"
                row_class = "activity-row activity-dim"
            else:
                turn = "" if entry.turn_id is None else f" · 回合 {entry.turn_id}"
                report = "\n".join(entry.report.splitlines()[:3])
                text = f"{label}{turn}\n{report}"
                row_class = "activity-row activity-report"
            rows.append(Static(text, classes=row_class))
        if not rows and self._projection.diagnostics:
            rows.append(Static("暂时无法读取 AI 战报，请稍后重试", classes="activity-row activity-error"))
        return rows


class GameSurface(Container):
    """Persistent game surface driven by adapter metadata and projection."""

    def __init__(self) -> None:
        super().__init__(id="game-surface")
        # Kept only as a compatibility shim for old focused fixtures.  It is
        # never rendered or polled by the production surface.
        self._model = BgGameView()
        self._metadata: GamePresentationMetadata | None = None
        self._projection = GameReportProjection("", (), (), ())
        self._prepared_title = "桌游"
        self._prepared_url: str | None = None
        self._prepared_status = "starting"
        self._status_override: str | None = None

    @property
    def _title(self) -> str:
        if self._metadata is not None:
            return self._metadata.title
        return self._prepared_title

    @property
    def _url(self) -> str | None:
        if self._metadata is not None:
            return self._metadata.url
        return self._prepared_url

    def compose(self) -> ComposeResult:
        yield GameWordmark()
        yield GameHero()
        yield GameActivity()

    def on_resize(self, event: events.Resize) -> None:
        self.query_one(GameWordmark).fit(event.size.width, event.size.height)

    def activate(self, metadata: GamePresentationMetadata) -> None:
        if not isinstance(metadata, GamePresentationMetadata):
            raise TypeError("GameSurface.activate expects GamePresentationMetadata")
        if self._metadata != metadata:
            self._projection = GameReportProjection("", (), (), ())
            self._status_override = None
        self._metadata = metadata
        self._prepared_title = metadata.title
        self._prepared_url = metadata.url
        self._prepared_status = metadata.status
        self.display = True
        self._sync()

    def prepare(self, *, title: str, url: str | None = None, status: str = "starting") -> None:
        """Show the pre-metadata state while the lifecycle owner starts a game."""
        self._metadata = None
        self._projection = GameReportProjection("", (), (), ())
        self._status_override = None
        self._prepared_title = title or "桌游"
        self._prepared_url = url
        self._prepared_status = status or "starting"
        self.display = True
        self._sync()

    def set_shutdown_error(self) -> None:
        self._status_override = "退出失败，请重试"
        self.display = True
        self._sync()

    def set_reports(self, projection: GameReportProjection) -> None:
        if not isinstance(projection, GameReportProjection):
            raise TypeError("GameSurface.set_reports expects GameReportProjection")
        self._projection = projection
        if self.is_mounted:
            self.query_one(GameActivity).set_reports(projection)

    def poll(self) -> bool:
        # Durable report polling is owned by BglabREPL's per-session poller;
        # never read the shared .live_events stream from this widget.
        return False

    def set_compact(self, compact: bool) -> None:
        self.query_one(GameHero).set_compact(compact)

    def _sync(self) -> None:
        if not self.is_mounted:
            return
        if self._metadata is not None:
            self.query_one(GameHero).set_state(
                title=self._metadata.title,
                game_id=self._metadata.game_id,
                status=self._status_override or self._metadata.status,
                url=self._metadata.url,
                player_count=len(self._metadata.player_types),
            )
        else:
            self.query_one(GameHero).set_state(
                title=self._prepared_title,
                game_id="",
                status=self._status_override or self._prepared_status,
                url=self._prepared_url,
            )
        self.query_one(GameActivity).set_reports(self._projection)


def _renderable_plain(widget: Any) -> str:
    """Read a timeline child's plain text without exposing rich internals."""
    renderable = getattr(widget, "renderable", None)
    plain = getattr(renderable, "plain", None)
    if isinstance(plain, str):
        return plain
    try:
        return str(widget.render())
    except Exception:
        return str(widget)


def append_exit_row(
    timeline: Any,
    title: str,
    report_count: int,
    *,
    session_token: object | None = None,
) -> None:
    """Append one passive exit row after the existing Code timeline closure."""
    marker = f"✓ 已退出 {title}"
    if session_token is not None:
        if getattr(timeline, "_bglab_exit_session_token", None) == session_token:
            return
    else:
        # Keep the small helper's fixture-level idempotency contract for
        # callers that do not own a game-session epoch.
        children = list(getattr(timeline, "children", ()))
        if children and marker in _renderable_plain(children[-1]):
            return
    row = Static(
        f"{marker} · 已返回终端",
        classes="log game-exit-row",
    )
    timeline.mount(row)
    if session_token is not None:
        setattr(timeline, "_bglab_exit_session_token", session_token)


class BrandSidebar(Container):
    """Crush-like information rail with the accepted Bit wordmark."""

    def __init__(self) -> None:
        super().__init__(id="brand-sidebar")
        self._mode = "code"
        self._surface: GameSurface | None = None
        self._cwd = ""
        self._model = ""
        self._thinking = False
        self._usage: dict[str, int | float] = {}
        self._modified_files: list[str] = []
        self._compact = False

    def compose(self) -> ComposeResult:
        yield Static(
            self._logo_renderable(),
            id="brand-wordmark",
        )
        yield Static(self._body(), id="sidebar-body")

    def _logo_renderable(self) -> Text:
        if self._compact:
            return Text("BGLAB", style=f"bold {ACCENT}")
        return Text.from_ansi(BGLAB_WORDMARK_ANSI)

    def set_context(
        self,
        *,
        cwd: str | None = None,
        model: str | None = None,
        thinking: bool | None = None,
        usage: dict[str, int | float] | None = None,
        modified_files: list[str] | None = None,
    ) -> None:
        if cwd is not None:
            self._cwd = cwd
        if model is not None:
            self._model = model
        if thinking is not None:
            self._thinking = thinking
        if usage is not None:
            self._usage = dict(usage)
        if modified_files is not None:
            self._modified_files = list(modified_files)
        self._refresh()

    def set_mode(self, mode: str, surface: GameSurface | None = None) -> None:
        self._mode = mode
        self._surface = surface
        self._refresh()

    def set_compact(self, compact: bool) -> None:
        if self._compact == compact:
            return
        self._compact = compact
        if compact:
            self.add_class("compact")
        else:
            self.remove_class("compact")
        try:
            self.query_one("#brand-wordmark", Static).update(self._logo_renderable())
        except Exception:
            pass
        self._refresh()

    def _refresh(self) -> None:
        if not self.is_mounted:
            return
        self.query_one("#sidebar-body", Static).update(self._body())

    def _display_cwd(self) -> str:
        if not self._cwd:
            return "~"
        from pathlib import Path
        try:
            cwd = Path(self._cwd).resolve()
            home = Path.home().resolve()
            try:
                value = "~/" + cwd.relative_to(home).as_posix()
            except ValueError:
                value = cwd.as_posix()
        except Exception:
            value = self._cwd.replace("\\", "/")
        if len(value) > 28:
            parts = value.rstrip("/").split("/")
            value = f"{parts[0]}/…/{parts[-1]}"
        return value

    def _usage_line(self) -> str:
        input_tokens = int(self._usage.get("input_tokens", 0))
        output_tokens = int(self._usage.get("output_tokens", 0))
        cache_hit = int(self._usage.get("cache_hit_tokens", 0))
        cache_miss = int(self._usage.get("cache_miss_tokens", 0))
        cache_total = cache_hit + cache_miss
        cache_rate = round(cache_hit * 100 / cache_total) if cache_total else 0
        total = input_tokens + output_tokens
        cost = float(self._usage.get("cost", 0.0))
        return f"{cache_rate}% ({fmt_tokens(total)}) ${cost:.2f}"

    def _modified_file_lines(self) -> str:
        if not self._modified_files:
            return "[dim]None[/]"
        return "\n".join(
            f"[dim]· {escape(path)}[/]" for path in self._modified_files[-5:]
        )

    def _body(self) -> str:
        session = "Game Session" if self._mode == "bg" else "New Session"
        model = self._model or "deepseek-chat"
        thinking = "Thinking On" if self._thinking else "Thinking Off"
        separator = "────" if self._compact else "──────────"
        return (
            f"[dim]{session}[/]\n"
            f"[dim]{escape(self._display_cwd())}[/]\n"
            f"[bold {INFO}]◇[/] [bold {TEXT}]{escape(model)}[/]\n"
            f"[dim]{thinking}[/]\n"
            f"[dim]{self._usage_line()}[/]\n\n"
            f"[bold {TEXT_DIM}]Modified Files[/] [dim]{separator}[/]\n"
            f"{self._modified_file_lines()}\n\n"
            f"[bold {TEXT_DIM}]LSPs[/] [dim]{separator}[/]\n"
            f"[bold {SUCCESS}]●[/] [dim]Textual[/]\n"
            f"[bold {SUCCESS}]●[/] [dim]BGLab[/]\n\n"
            f"[bold {TEXT_DIM}]MCPs[/] [dim]{separator}[/]\n"
            "[dim]None[/]"
        )

# ══════════════════════════════════════════════════════════════════════
# App
# ══════════════════════════════════════════════════════════════════════

class BglabREPL(App):
    CSS = f"""
    Screen {{ background: {BG_SCREEN}; }}

    #app-header {{ height: auto; padding: 1 3 0 3; color: {TEXT_DIM}; }}
    #welcome {{ height: 1fr; align: center middle; }}
    #welcome-content {{ width: 58; max-width: 100%; height: auto; padding: 0 3; }}
    #welcome-brand {{ height: 4; color: {ACCENT}; }}
    #welcome-caption {{ height: 1; color: {TEXT_DIM}; margin: 1 0 2 0; }}
    #welcome-actions {{ height: auto; color: {TEXT_DIM}; }}
    #welcome.compact #welcome-brand {{ display: none; }}
    #welcome.compact #welcome-caption {{ margin: 0 0 1 0; color: {ACCENT}; text-style: bold; }}
    #welcome.compact #welcome-content {{ padding: 0 2; }}

    #root-frame {{
        height: 1fr;
        width: 1fr;
        layout: horizontal;
        background: {BG_SCREEN};
    }}

    #main-column {{
        height: 1fr;
        width: 1fr;
        min-width: 0;
        layout: vertical;
        background: {BG_SCREEN};
    }}

    MessagesArea {{
        height: 1fr;
        overflow-y: auto;
        padding: 1 3;
        scrollbar-size: 1 1;
        scrollbar-color: {BORDER};
        scrollbar-background: {BG_SCREEN};
    }}

    #code-surface {{
        height: 1fr;
        min-height: 0;
    }}

    #game-surface {{
        display: none;
        height: 1fr;
        min-height: 0;
        background: {BG_SCREEN};
        align: center middle;
    }}

    #game-hero {{
        width: 68; max-width: 100%;
        height: auto;
        min-height: 5;
        padding: 1 3;
        text-align: center;
        background: {BG_SCREEN};
        overflow-y: auto;
    }}

    #game-hero.compact {{
        height: auto;
        min-height: 1;
    }}

    #game-wordmark {{
        width: 68; max-width: 100%;
        height: auto;
        padding: 0 3;
        color: {ACCENT};
    }}

    #game-activity {{
        display: none;
        height: 1fr;
        padding: 1 2;
        overflow-y: auto;
        scrollbar-size: 1 1;
    }}

    .activity-header {{
        height: 2;
        padding: 1 0 0 0;
    }}

    .activity-row {{
        height: auto;
        min-height: 1;
        padding: 0 1 1 1;
        color: {TEXT_DIM};
        overflow-x: hidden;
        text-overflow: ellipsis;
    }}

    .activity-success {{ color: {TEXT}; }}
    .activity-error {{ color: {ERROR}; }}
    .activity-dim {{ color: {TEXT_DIM}; }}
    .activity-report {{ color: {TEXT}; }}

    #brand-sidebar {{
        width: 32;
        min-width: 32;
        height: 1fr;
        padding: 1 2;
        background: {BG_SCREEN};
        border-left: solid {BORDER};
        overflow-y: auto;
        scrollbar-size: 1 1;
    }}

    #brand-sidebar.compact {{
        width: 27;
        min-width: 27;
        padding: 1;
    }}

    #brand-wordmark {{
        height: 5;
        padding: 0;
        color: {TEXT};
    }}

    #brand-sidebar.compact #brand-wordmark {{
        height: 1;
    }}

    #sidebar-body {{
        height: auto;
        color: {TEXT_DIM};
    }}

    #status-bar {{
        dock: bottom;
        height: 1;
        background: {BG_INPUT};
        color: {TEXT_DIM};
        padding: 0 1;
    }}

    #input-bar {{
        height: 5;
        min-height: 5;
        layout: vertical;
        background: {BG_SCREEN};
        padding: 0 3;
        border: none;
    }}

    #input-bar > Input {{
        background: {BG_INPUT};
        border: none;
        border-left: thick {BORDER};
        padding: 1 2;
        height: 3;
        color: {TEXT};
    }}
    #input-bar > Input:focus {{ border-left: thick {ACCENT}; }}

    #composer-hints {{
        height: 1;
        padding: 0 1;
        background: {BG_SCREEN};
        color: {TEXT_MUTED};
        overflow-x: hidden;
        text-overflow: ellipsis;
    }}

    Divider           {{ height: 1; padding: 0; color: {TEXT_MUTED}; }}
    TurnCompletion    {{ height: 1; padding: 0; }}
    ThinkingLine      {{ height: 1; padding: 0 2; }}
    StatusLine        {{ display: none; height: 0; }}
    Static.log        {{ padding: 0 2; height: auto; color: {TEXT}; }}
    Static.user-msg   {{ padding: 1 2; height: auto; background: {BG_USER_MSG}; margin: 1 0; }}
    Static.assistant-text {{ height: auto; padding: 1 0; color: {TEXT}; }}
    Static.turn-closure {{ height: auto; padding: 1 0; color: {TEXT_DIM}; }}
    Static.provider-issue, Static.error-summary {{ height: auto; padding: 1 0; }}
    RichLog           {{ padding: 0 2; }}
    ToolCard          {{ height: auto; padding: 0 2; background: {BG_SCREEN}; border: none; }}
    ToolCard:focus    {{ background: {BG_INPUT}; border-left: solid {ACCENT}; padding-left: 1; }}
    Toast             {{ height: auto; padding: 0 2; }}

    #bg-pane {{
        height: auto; padding: 1 2; margin: 1 0;
        background: {BG_INPUT}; border: solid {ACCENT};
        color: {TEXT};
    }}

    .tool-pending {{ background: {BG_TOOL_PENDING}; border: solid {INFO}; }}
    .tool-success {{ background: {BG_TOOL_SUCCESS}; border: solid {SUCCESS}; }}
    .tool-error   {{ background: {BG_TOOL_ERROR}; border: solid {ERROR}; }}
    """

    BINDINGS = [
        Binding("ctrl+c", "cancel_or_quit", "Cancel or quit", show=False, priority=True),
        Binding("ctrl+o", "open_game", "打开棋盘", show=False),
    ]

    # session picker state
    _pk_sessions: list | None = None
    _pk_mode: str = ""
    _pk_idx: int = 0
    _pk_selected: set = set()
    _pk_widgets: list = []

    _slash_widgets: list = []  # widgets for slash command popup


    # permission picker state
    _perm_future: asyncio.Future | None = None
    _perm_idx: int = 0
    _perm_widgets: list = []
    _perm_info: tuple = ("", {}, "")
    _perm_session_allow: set = set()

    _toast_timer: Any = None

    def __init__(self, model: str = "deepseek-chat", cwd: str | None = None,
                 one_shot_prompt: str | None = None):
        super().__init__()
        self.cwd = cwd or os.getcwd()
        self.register_theme(AppTheme(
            name="bglab", primary=ACCENT, secondary=ACCENT_SOFT, accent=ACCENT,
            foreground=TEXT, background=BG_SCREEN, surface=BG_INPUT, panel=BG_USER_MSG,
            success=SUCCESS, warning=WARNING, error=ERROR,
        ))
        self.theme = "bglab"
        from bglab.llm.providers import canonical_model_reference
        from bglab.session.settings import load_settings
        persisted = load_settings(cwd=self.cwd)
        from bglab.session.feature_flags import FeatureFlags
        self._feature_flags = FeatureFlags.from_dict(
            persisted.get("feature_flags"),
        )
        self._language = persisted.get("language")
        selected_model = model
        if model == "deepseek-chat":
            selected_model = persisted.get("model", model)
        try:
            selected_model = canonical_model_reference(selected_model)
        except ValueError:
            pass
        self.model = selected_model
        try:
            self.provider_slots = load_provider_slot_policy(persisted)
        except ValueError:
            self.provider_slots = load_provider_slot_policy({"model": self.model})
        self._one_shot_prompt = one_shot_prompt
        self._messages: list[dict] = []
        self._tool_registry = build_registry()
        ask_user_tool = self._tool_registry.get("AskUserQuestion")
        if ask_user_tool is not None:
            ask_user_tool.call = self._ask_user_interactively
        self._deps = self._make_deps()
        self._status_line: StatusLine | None = None
        self._plan_banner: PlanModeBanner | None = None
        self._task_board: TaskBoard | None = None
        self._total_turns = 0
        self._perm_mode = "default"
        self._perm_future = None
        self._perm_idx = 0
        self._perm_widgets = []
        self._perm_info = ("", {}, "")
        self._perm_session_allow = set()
        self._history: list[str] = []
        self._history_idx = -1
        self._show_help = False
        self._cmd_card: dict | None = None
        self._thinking_worker: asyncio.Task | None = None
        self._bg_view: GameSurface | None = None
        self._bg_poll_timer: asyncio.Task | None = None
        self._bg_last_read = 0
        self._game_generation = 0
        self._game_session: GamePresentationSession | None = None
        self._game_ui_metadata: GamePresentationMetadata | None = None
        self._game_enter_epoch = 0
        self._game_exit_epoch = -1
        self._game_metadata_task: asyncio.Task | None = None
        self._last_game_exit_title = "桌游"
        self._last_game_report_count = 0
        # Session transitions share one coordinator lock.  The lock is created
        # here for real app instances and lazily repaired for object.__new__-
        # based focused fixtures (and for a fixture crossing event loops).
        self._game_session_lock = asyncio.Lock()
        self._game_transition_lock = self._game_session_lock
        self._game_diagnostics: list[str] = []
        self._game_blocked = False
        self._bg_starting = False
        self._pending_game_exit_input: str | None = None
        self._active_query_worker: Any = None
        self._active_thinking: ThinkingLine | None = None
        self._ask_user_future: asyncio.Future | None = None
        self._ask_user_previous_placeholder = ""
        self._modified_files: list[str] = []
        self._presentation_session_id = f"app-{uuid.uuid4().hex}"
        self._transcript_session_id = str(uuid.uuid4())
        self._presentation_turn_id = 0
        self._active_reducer: PresentationReducer | None = None
        self._active_turn_widgets: list[Static] = []
        self._raw_done_events = 0
        self._rebuild_query_engine()

        from bglab.session.state import session_state
        session_state.model = self.model
        session_state.cwd = self.cwd

    def _make_deps(self) -> QueryDeps:
        from bglab.llm.client import call_model as _call_model
        from bglab.compaction.autocompact import CompactTracker
        from bglab.loop_detector import LoopDetector
        from bglab.hooks.state import StopHooksState
        return QueryDeps(
            call_model=_call_model,
            compact_tracker=CompactTracker(),
            loop_detector=LoopDetector(),
            stop_hooks_state=StopHooksState(),
        )

    def _rebuild_query_engine(
        self,
        messages: list[dict] | None = None,
    ) -> None:
        """Bind one persistent Code QueryEngine to the current TUI task."""

        previous = getattr(self, "_query_engine", None)
        if previous is not None:
            previous.invalidate_prompt_context("session_rebuild")
        from bglab.prompt.system_prompt_sections import clear_system_prompt_sections

        clear_system_prompt_sections(self._transcript_session_id)
        self._query_engine = QueryEngine(
            cwd=self.cwd,
            model=self.model,
            max_turns=50,
            permission_mode=self._perm_mode,
            deps=self._deps,
            session_id=self._transcript_session_id,
            ask_callback=self._tui_ask_callback,
            tool_registry=self._tool_registry,
            feature_flags=self._feature_flags,
            language=self._language,
        )
        self._query_engine.mutable_messages = list(messages or [])
        self._query_engine.set_permission_mode(self._perm_mode)
        self._messages = self._query_engine.mutable_messages

    def _next_presentation_key(self) -> TurnKey:
        self._presentation_turn_id += 1
        return TurnKey(self._presentation_session_id, self._presentation_turn_id)

    @staticmethod
    def _game_report_stat(path: Path) -> tuple[int, int]:
        if not path.is_file():
            return 0, 0
        stat = path.stat()
        return int(stat.st_size), int(stat.st_mtime_ns)

    def _ensure_game_session_lock(self) -> asyncio.Lock:
        """Return the transition lock, tolerating lightweight test fixtures."""
        lock = getattr(self, "_game_session_lock", None)
        if lock is None:
            lock = getattr(self, "_game_transition_lock", None)
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        lock_loop = getattr(lock, "_loop", None)
        if (
            lock is None
            or (
                current_loop is not None
                and lock_loop is not None
                and lock_loop is not current_loop
                and not lock.locked()
            )
        ):
            lock = asyncio.Lock()
        self._game_session_lock = lock
        self._game_transition_lock = lock
        return lock

    def _record_game_diagnostic(self, stage: str, error: BaseException) -> None:
        """Keep a bounded, non-secret coordinator diagnostic."""
        name = type(error).__name__
        message = f"{stage}: {name}"
        diagnostics = getattr(self, "_game_diagnostics", None)
        if diagnostics is None:
            diagnostics = []
            self._game_diagnostics = diagnostics
        diagnostics.append(message[:160])
        del diagnostics[:-32]

    def _make_game_session(
        self, metadata: GamePresentationMetadata, generation: int,
    ) -> GamePresentationSession:
        _, manifest_mtime_ns = self._game_report_stat(metadata.manifest_path)
        size, mtime_ns = self._game_report_stat(metadata.report_path)
        ai_seats = tuple(
            index
            for index, player_type in enumerate(metadata.player_types)
            if player_type == "ai"
        )
        return GamePresentationSession(
            game_id=metadata.game_id,
            generation=generation,
            game_dir=metadata.game_dir,
            manifest_path=metadata.manifest_path,
            manifest_mtime_ns=manifest_mtime_ns,
            report_path=metadata.report_path,
            report_cursor=0,
            report_line_count=0,
            report_file_size=size,
            report_mtime_ns=mtime_ns,
            report_records=[],
            report_cache_loaded=False,
            report_cursor_anchor=b"",
            projection_cache=None,
            title=metadata.title,
            status=metadata.status,
            http_port=metadata.http_port,
            url=metadata.url,
            player_types=metadata.player_types,
            ai_seats=ai_seats,
            poll_task=None,
        )

    def _session_is_current(self, session: GamePresentationSession) -> bool:
        return (
            session is self._game_session
            and session.generation == self._game_generation
        )

    def _clear_game_surface(self) -> None:
        """Reset coordinator-owned surface hooks without touching lifecycle."""
        self._bg_last_read = 0

    def _cancel_game_metadata_watcher(self) -> asyncio.Task | None:
        """Cancel a model-driven metadata lookup owned by the current epoch."""
        task = getattr(self, "_game_metadata_task", None)
        if task is not None and not task.done():
            task.cancel()
        self._game_metadata_task = None
        return task

    def _show_game_shutdown_error(self) -> None:
        """Keep the game surface retryable with a fixed, non-secret message."""
        surface = self._mounted_game_surface()
        if surface is None:
            try:
                surface = self.query_one(GameSurface)
            except Exception:
                surface = None
        if surface is not None:
            surface.set_shutdown_error()
            surface.display = True
            self._bg_view = surface
        try:
            self.query_one(MessagesArea).display = False
            input_widget = self.query_one("#user-input")
            input_widget.disabled = False
            input_widget.placeholder = "退出失败，请重试 · /bg stop 再试"
            input_widget.focus()
        except Exception:
            return
        self._refresh_composer_hints()

    @staticmethod
    def _game_stop_succeeded(result: str) -> bool:
        return result.startswith((
            "Game stopped",
            "No active",
            "Game start cancelled",
        ))

    async def _complete_game_shutdown(self, result: str) -> bool:
        """Close one owned game epoch, or leave it visibly retryable."""
        if not getattr(self, "_game_blocked", False):
            return False
        if not self._game_stop_succeeded(result):
            self._show_game_shutdown_error()
            return False
        try:
            report_count = await self._close_game_session()
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            self._record_game_diagnostic("game close", exc)
            self._show_game_shutdown_error()
            return False
        # _leave_game_blocked appends the single user-facing exit row.
        metadata_task = getattr(self, "_game_metadata_task", None)
        self._leave_game_blocked(report_count=report_count)
        if metadata_task is not None and metadata_task is not asyncio.current_task():
            await asyncio.gather(metadata_task, return_exceptions=True)
        return True

    def _start_game_metadata_watcher(self) -> None:
        """Resolve model-driven BgPlay metadata without reading live events."""
        self._cancel_game_metadata_watcher()
        epoch = getattr(self, "_game_enter_epoch", 0)

        async def _watch() -> None:
            try:
                from bglab.tools.bg_play import get_presentation_metadata

                while (
                    getattr(self, "_game_blocked", False)
                    and getattr(self, "_game_enter_epoch", -1) == epoch
                ):
                    try:
                        metadata = await asyncio.to_thread(
                            get_presentation_metadata, None,
                        )
                    except (OSError, ValueError, TypeError):
                        metadata = None
                    if metadata is not None:
                        if (
                            not getattr(self, "_game_blocked", False)
                            or getattr(self, "_game_enter_epoch", -1) != epoch
                        ):
                            return
                        try:
                            await self._activate_game_session(metadata)
                        except (OSError, ValueError, TypeError, RuntimeError) as exc:
                            self._record_game_diagnostic("game metadata watcher", exc)
                        return
                    await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._record_game_diagnostic("game metadata watcher", exc)
            finally:
                if getattr(self, "_game_metadata_task", None) is asyncio.current_task():
                    self._game_metadata_task = None

        self._game_metadata_task = asyncio.create_task(_watch())

    def _mounted_game_surface(self) -> GameSurface | None:
        """Return the mounted surface for real apps, or ``None`` for fixtures."""
        try:
            if not self.is_mounted:
                return None
            return self.query_one(GameSurface)
        except Exception:
            return None

    def _sync_game_session_ui(self, session: GamePresentationSession) -> None:
        """Project one current session into the already-mounted game surface."""
        if not self._session_is_current(session):
            return
        surface = self._mounted_game_surface()
        metadata = getattr(self, "_game_ui_metadata", None)
        projection = session.projection_cache
        if surface is None or metadata is None or metadata.game_id != session.game_id:
            return
        surface.activate(metadata)
        if projection is not None:
            surface.set_reports(projection)
        try:
            self.query_one(MessagesArea).display = False
            surface.display = True
        except Exception:
            pass
        self._bg_view = surface

    def _refresh_game_session_metadata_if_changed(
        self, session: GamePresentationSession,
    ) -> bool:
        """Refresh status only after the exact bound manifest changes."""
        _, manifest_mtime_ns = self._game_report_stat(session.manifest_path)
        if manifest_mtime_ns == session.manifest_mtime_ns:
            return True

        from bglab.tools.bg_play import get_presentation_metadata

        metadata = get_presentation_metadata(session.game_id)
        if metadata.game_id != session.game_id:
            raise ValueError("game metadata identity changed while polling")
        if Path(metadata.game_dir).resolve() != session.game_dir.resolve():
            raise ValueError("game directory changed while polling")
        if Path(metadata.manifest_path).resolve() != session.manifest_path.resolve():
            raise ValueError("manifest path changed while polling")
        if Path(metadata.report_path).resolve() != session.report_path.resolve():
            raise ValueError("report path changed while polling")
        if not self._session_is_current(session):
            return False

        session.game_id = metadata.game_id
        session.game_dir = metadata.game_dir
        session.manifest_path = metadata.manifest_path
        session.report_path = metadata.report_path
        session.title = metadata.title
        session.status = metadata.status
        session.http_port = metadata.http_port
        session.url = metadata.url
        session.player_types = metadata.player_types
        session.ai_seats = tuple(
            index
            for index, player_type in enumerate(metadata.player_types)
            if player_type == "ai"
        )
        _, session.manifest_mtime_ns = self._game_report_stat(
            session.manifest_path,
        )
        self._game_ui_metadata = metadata
        self._sync_game_session_ui(session)
        return True

    def _session_store(self, session: GamePresentationSession):
        from bglab.games.persistence.store import GameStore

        if not session.game_dir.is_dir():
            raise ValueError(f"game directory is missing: {session.game_dir}")
        store = GameStore.from_existing_dir(session.game_id, session.game_dir)
        if Path(store.dir).resolve() != session.game_dir.resolve():
            raise ValueError("game directory changed while polling")
        return store

    def _read_projection(self, session: GamePresentationSession) -> GameReportProjection:
        if session.report_cache_loaded:
            return project_game_report_records(
                session.report_records,
                game_id=session.game_id,
                ai_seats=session.ai_seats,
            )
        store = self._session_store(session)
        return project_game_reports(store, game_id=session.game_id)

    @staticmethod
    def _report_cursor_anchor(path: Path, cursor: int) -> bytes:
        if cursor <= 0:
            return b""
        try:
            with path.open("rb") as file:
                start = max(0, cursor - 64)
                file.seek(start)
                return file.read(cursor - start)
        except OSError:
            return b""

    @staticmethod
    def _clear_report_cache(session: GamePresentationSession) -> None:
        session.report_records = []
        session.report_line_count = 0
        session.report_cursor = 0
        session.report_cache_loaded = False
        session.report_cursor_anchor = b""
        session.projection_cache = None

    def _project_report_records(
        self,
        session: GamePresentationSession,
        records: list[dict],
    ) -> GameReportProjection:
        previous_records = session.report_records
        previous_loaded = session.report_cache_loaded
        session.report_records = records
        session.report_cache_loaded = True
        try:
            return self._read_projection(session)
        finally:
            session.report_records = previous_records
            session.report_cache_loaded = previous_loaded

    def _commit_report_cache(
        self,
        session: GamePresentationSession,
        *,
        records: list[dict],
        cursor: int,
        line_count: int,
        projection: GameReportProjection,
        signature: tuple[int, int],
    ) -> bool:
        if not self._session_is_current(session):
            return False
        session.report_records = list(records)
        session.report_line_count = line_count
        session.report_cursor = cursor
        session.report_file_size, session.report_mtime_ns = signature
        session.report_cursor_anchor = self._report_cursor_anchor(
            session.report_path, cursor,
        )
        session.report_cache_loaded = True
        return self._apply_poll_result(session, projection)

    def _refresh_report_cache(
        self,
        session: GamePresentationSession,
        before: tuple[int, int],
        *,
        force_full: bool = False,
    ) -> bool:
        append = False
        if not force_full and session.report_cache_loaded:
            append = (
                before[0] >= session.report_cursor
                and self._report_cursor_anchor(
                    session.report_path, session.report_cursor,
                ) == session.report_cursor_anchor
                and not (
                    before[0] == session.report_cursor
                    and before[1] != session.report_mtime_ns
                )
            )
        store = self._session_store(session)
        candidate_records: list[dict]
        candidate_cursor: int
        candidate_line_count: int
        projection: GameReportProjection | None = None
        read_error: Exception | None = None
        try:
            if append:
                delta, candidate_cursor, delta_lines = (
                    store.read_turn_reports_from(
                        session.report_cursor,
                        line_number_start=session.report_line_count,
                    )
                )
                candidate_records = [*session.report_records, *delta]
                candidate_line_count = session.report_line_count + delta_lines
                if not delta and session.projection_cache is not None:
                    projection = session.projection_cache
                else:
                    projection = None  # projected below after the read
            else:
                candidate_records, candidate_cursor, candidate_line_count = (
                    store.read_turn_reports_from(0)
                )
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            candidate_records = []
            candidate_cursor = 0
            candidate_line_count = 0
            read_error = exc

        if read_error is not None:
            projection = project_game_report_records(
                [],
                game_id=session.game_id,
                ai_seats=session.ai_seats,
                diagnostics=(f"turn_reports.jsonl read failed: {read_error}",),
            )
        elif not append or projection is None:
            projection = self._project_report_records(
                session, candidate_records,
            )

        after = self._game_report_stat(session.report_path)
        if not self._session_is_current(session):
            return False
        if after != before:
            self._clear_report_cache(session)
            session.report_file_size, session.report_mtime_ns = after
            return False
        return self._commit_report_cache(
            session,
            records=candidate_records,
            cursor=candidate_cursor,
            line_count=candidate_line_count,
            projection=projection,
            signature=before,
        )

    def _invalidate_projection_if_size_or_mtime_changed(
        self, session: GamePresentationSession,
        signature: tuple[int, int] | None = None,
    ) -> None:
        size, mtime_ns = (
            signature
            if signature is not None
            else self._game_report_stat(session.report_path)
        )
        # A shrink, or a same-size rewrite, invalidates the byte cursor.  An
        # mtime change with a larger file can still be a valid append; the
        # incremental refresh verifies the cached boundary before accepting
        # it.
        if (
            size < session.report_cursor
            or (
                mtime_ns != session.report_mtime_ns
                and size <= session.report_cursor
            )
        ):
            self._clear_report_cache(session)
        session.report_file_size = size
        session.report_mtime_ns = mtime_ns

    def _apply_poll_result(
        self,
        session: GamePresentationSession,
        projection: GameReportProjection,
    ) -> bool:
        if not self._session_is_current(session):
            return False
        session.projection_cache = projection
        self._sync_game_session_ui(session)
        return True

    async def _poll_game_session(self, session: GamePresentationSession) -> bool:
        if not self._session_is_current(session):
            return False
        if not self._refresh_game_session_metadata_if_changed(session):
            return False
        before = self._game_report_stat(session.report_path)
        cached_signature = (session.report_file_size, session.report_mtime_ns)
        self._invalidate_projection_if_size_or_mtime_changed(session, before)
        if (
            session.report_cache_loaded
            and before == cached_signature
        ):
            # The durable report is unchanged; keep the existing projection
            # and avoid reading, parsing, or projecting it again.
            return True
        return self._refresh_report_cache(session, before)

    async def _run_game_session_poller(
        self, session: GamePresentationSession,
    ) -> None:
        while self._session_is_current(session):
            try:
                await self._poll_game_session(session)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never leave stale durable rows visible after an I/O/path
                # failure.  A later poll can recover the exact session.
                if self._session_is_current(session):
                    self._clear_report_cache(session)
                    try:
                        session.report_file_size, session.report_mtime_ns = (
                            self._game_report_stat(session.report_path)
                        )
                    except OSError:
                        session.report_file_size = 0
                        session.report_mtime_ns = 0
                    self._record_game_diagnostic("game poll", exc)
            await asyncio.sleep(0.2)

    def _load_projection_and_start_captured_poller(
        self, session: GamePresentationSession,
    ) -> None:
        if not self._session_is_current(session):
            return
        before = self._game_report_stat(session.report_path)
        self._invalidate_projection_if_size_or_mtime_changed(session, before)
        if not self._refresh_report_cache(session, before, force_full=True):
            return
        if self._session_is_current(session):
            poller = self._run_game_session_poller(session)
            try:
                session.poll_task = asyncio.create_task(poller)
            except BaseException:
                poller.close()
                raise

    async def _cancel_and_await_old_poller(
        self,
    ) -> GamePresentationSession | None:
        old_session = self._game_session
        # Remove identity before cancellation so a callback that wakes during
        # cancellation cannot write into the outgoing surface.
        self._game_session = None
        if old_session is None or old_session.poll_task is None:
            return old_session
        task = old_session.poll_task
        old_session.poll_task = None
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            # A completed poller may still carry a non-cancellation failure.
            # Drain it without aborting the transition and retain only the
            # exception class as a safe coordinator diagnostic.
            self._record_game_diagnostic("old game poller", exc)
        return old_session

    async def _activate_game_session(
        self, metadata: GamePresentationMetadata,
    ) -> None:
        async with self._ensure_game_session_lock():
            await self._activate_game_session_unlocked(metadata)

    async def _activate_game_session_unlocked(
        self, metadata: GamePresentationMetadata,
    ) -> None:
        await self._cancel_and_await_old_poller()
        self._game_generation = getattr(self, "_game_generation", 0) + 1
        session: GamePresentationSession | None = None
        try:
            session = self._make_game_session(metadata, self._game_generation)
            self._game_session = session
            self._game_ui_metadata = metadata
            self._clear_game_surface()
            self._load_projection_and_start_captured_poller(session)
            self._sync_game_session_ui(session)
        except BaseException:
            if session is not None:
                await self._cancel_session_poller(session)
            self._game_session = None
            self._game_ui_metadata = None
            # A failed activation must not leave an outgoing game identity on
            # the surface hooks, and generation remains monotonically advanced.
            self._clear_game_surface()
            if hasattr(self, "_bg_view"):
                self._bg_view = None
            raise

    async def _cancel_session_poller(
        self, session: GamePresentationSession,
    ) -> None:
        task = session.poll_task
        session.poll_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._record_game_diagnostic("new game poller", exc)

    def _durable_report_count(
        self, session: GamePresentationSession | None = None,
    ) -> int:
        target = session or self._game_session
        if target is None:
            return 0
        projection = self._read_projection(target)
        return sum(1 for entry in projection.entries if not entry.waiting)

    async def _close_game_session(self) -> int:
        async with self._ensure_game_session_lock():
            old_session = await self._cancel_and_await_old_poller()
            if old_session is None:
                return 0
            try:
                count = self._durable_report_count(old_session)
            except BaseException:
                # Counting is the durable close gate.  Restore the exact
                # session (same generation/cache/cursor) so a later close can
                # retry after the underlying read is repaired.
                old_session.poll_task = None
                self._game_session = old_session
                raise
            self._last_game_exit_title = old_session.title
            self._last_game_report_count = count
            self._game_generation = getattr(self, "_game_generation", 0) + 1
            self._game_session = None
            self._game_ui_metadata = None
            self._clear_game_surface()
            if hasattr(self, "_bg_view"):
                self._bg_view = None
            return count

    def compose(self) -> ComposeResult:
        with Container(id="root-frame"):
            with Container(id="main-column"):
                yield Static("", id="app-header")
                with Container(id="welcome"):
                    with Container(id="welcome-content"):
                        yield Static(
                            "█▀▀▄  ▄▀▀▀  █      ▄▀▄   █▀▀▄\n"
                            "█▄▄▀  █ ▄▄  █     █▄▄▄█  █▄▄▀\n"
                            "█  █  █  █  █     █   █  █  █\n"
                            "▀▀▀    ▀▀   ▀▀▀▀  ▀   ▀  ▀▀▀",
                            id="welcome-brand",
                        )
                        yield Static("桌游实验室", id="welcome-caption")
                        yield Static(
                            f"[bold {TEXT}]/model[/]       连接模型与 API Key\n\n"
                            f"[bold {TEXT}]/bg start[/]   选择桌游，开始一局\n\n"
                            f"[bold {TEXT}]/bg resume[/]  继续上次的对局",
                            id="welcome-actions",
                        )
                yield MessagesArea(id="code-surface")
                yield GameSurface()
                with InputArea(id="input-bar"):
                    yield Input(
                        placeholder="输入 /model 开始配置，或 /help 查看命令",
                        id="user-input",
                    )
                    yield Static(
                        "Enter 发送 · / 查看命令 · Ctrl+C 退出",
                        id="composer-hints",
                    )

    def on_mount(self) -> None:
        self.console.push_theme(RichTheme({
            **{f"markdown.h{level}": f"bold {ACCENT}" for level in range(1, 7)},
            "markdown.code": ACCENT, "markdown.link": f"underline {ACCENT}",
            "markdown.block_quote": TEXT_DIM,
        }))
        self._status_line = StatusLine(model=self.model)
        if self._one_shot_prompt:
            self.set_timer(0.1, self._submit_one_shot)
        else:
            self.query_one("#user-input").focus()
        self.query_one(MessagesArea).display = False
        self._refresh_sidebar()
        self._apply_breakpoint(self.size.width, self.size.height)

    def on_resize(self, event: events.Resize) -> None:
        self._apply_breakpoint(event.size.width, event.size.height)

    def _apply_breakpoint(self, width: int, height: int) -> None:
        compact = width < 64 or height < 22
        welcome = self.query_one("#welcome")
        welcome.set_class(compact, "compact")
        self.query_one(GameSurface).set_compact(compact)
        sidebar_query = self.query(BrandSidebar)
        if sidebar_query.nodes:
            sidebar_query.nodes[0].set_compact(compact)
        self._refresh_composer_hints(compact)

    def _refresh_composer_hints(self, compact: bool | None = None) -> None:
        if not self.is_mounted:
            return
        if compact is None:
            compact = self.size.width < 64 or self.size.height < 22
        if compact:
            text = "Ctrl+O 打开棋盘 · Enter 执行命令" if self._game_blocked else "Enter 发送 · /help 帮助 · Ctrl+C 退出"
        elif self._game_blocked:
            text = "Ctrl+O 打开棋盘 · Enter 执行命令 · Ctrl+C 退出"
        else:
            text = "Enter 发送 · / 查看命令 · Esc 取消 · Ctrl+C 退出"
        self.query_one("#composer-hints", Static).update(text)

    def _refresh_sidebar(self) -> None:
        if not self.is_mounted:
            return
        from bglab.llm.credentials import credential_status
        configured = credential_status(self.provider_slots.primary.credential_env, start=self.cwd).configured
        readiness = f"[{SUCCESS}]● Key 已配置[/]" if configured else f"[{WARNING}]○ 待配置 API Key[/]"
        from bglab.llm.providers import resolve_model
        try:
            resolved = resolve_model(self.model)
            model_label = resolved.model.display_name
            if resolved.provider.id != "deepseek":
                model_label += f" / {resolved.provider.display_name}"
        except ValueError:
            model_label = self.model
        self.query_one("#app-header", Static).update(
            f"[bold {TEXT}]BGLab[/]   {rich_escape(model_label)}   {readiness}",
        )
        sidebar_query = self.query(BrandSidebar)
        if not sidebar_query.nodes:
            return
        usage: dict[str, int | float] = {}
        if self._status_line is not None:
            usage = {
                "input_tokens": self._status_line.total_input,
                "output_tokens": self._status_line.total_output,
                "cache_hit_tokens": self._status_line.total_cache_hit,
                "cache_miss_tokens": self._status_line.total_cache_miss,
                "cost": self._status_line.total_cost,
            }
        sidebar_query.nodes[0].set_context(
            cwd=self.cwd,
            model=self.model,
            thinking=self._active_thinking is not None,
            usage=usage,
            modified_files=self._modified_files,
        )

    def _record_modified_file(self, tool_name: str, tool_input: Any) -> None:
        if tool_name not in {"Write", "Edit", "NotebookEdit"}:
            return
        if not isinstance(tool_input, dict):
            return
        path = next(
            (tool_input.get(key) for key in ("file_path", "path", "filename")
             if tool_input.get(key)),
            None,
        )
        if not path:
            return
        shown = str(path).replace("\\", "/")
        if shown not in self._modified_files:
            self._modified_files.append(shown)
            self._modified_files = self._modified_files[-8:]
            self._refresh_sidebar()

    # ── Toast ────────────────────────────────────────────────────────

    def show_toast(self, text: str, color: str = C_ORANGE, ttl_ms: int = 1500) -> None:
        msgs = self.query_one(MessagesArea)
        if self._toast_timer:
            try:
                self._toast_timer.remove()
            except Exception:
                pass
        toast = Toast(text, color, timeout_ms=ttl_ms)
        msgs.mount(toast)
        self._toast_timer = toast

    # ── Key Handler ──────────────────────────────────────────────────

    async def action_cancel_or_quit(self) -> None:
        """Cancel an active Code Agent request without losing the TUI."""
        if self._game_blocked:
            from bglab.tools.bg_play import stop_game

            result = await asyncio.to_thread(stop_game)
            await self._complete_game_shutdown(result)
            return
        worker = self._active_query_worker
        if (
            worker is not None
            and not worker.is_finished
            and not self._game_blocked
        ):
            worker.cancel()
            return
        await self.action_quit()

    def on_key(self, event: events.Key) -> None:
        """Intercept keys before widgets process them. Use stop()+prevent_default() to consume."""
        # A background screen can retain its focused widget while a modal is
        # active. Its history/navigation shortcuts must not consume modal keys.
        if len(self.screen_stack) > 1:
            return
        key = event.key

        if key == "escape" and self._pending_game_exit_input is not None:
            event.stop()
            event.prevent_default()
            self._pending_game_exit_input = None
            self._restore_game_input_placeholder()
            return
        if (
            key == "escape" and not self._game_blocked
            and self._perm_future is None and self._pk_sessions is None
            and self._active_query_worker is not None
            and not self._active_query_worker.is_finished
        ):
            event.stop()
            event.prevent_default()
            self._active_query_worker.cancel()
            return

        # Permission dialog active — ↑↓/enter/esc/y/n
        if self._perm_future is not None and not self._perm_future.done():
            event.stop()
            event.prevent_default()
            if key == "escape":
                self._perm_dismiss(False)
            elif key == "up":
                self._perm_idx = max(0, self._perm_idx - 1)
                self._perm_render()
            elif key == "down":
                self._perm_idx = min(2, self._perm_idx + 1)
                self._perm_render()
            elif key == "enter":
                self._perm_dismiss(self._perm_idx != 2)
            elif key in ("y", "Y"):
                self._perm_dismiss(True)
            elif key in ("n", "N"):
                self._perm_dismiss(False)
            return

        # Session picker active — ↑↓/enter/esc/space
        if self._pk_sessions is not None:
            event.stop()
            event.prevent_default()
            if key == "escape":
                self._pk_dismiss()
            elif key == "up":
                self._pk_idx = max(0, self._pk_idx - 1); self._pk_render()
            elif key == "down":
                self._pk_idx = min(len(self._pk_sessions) - 1, self._pk_idx + 1); self._pk_render()
            elif key == "space" and self._pk_mode == "delete":
                idx = self._pk_idx
                if idx in self._pk_selected:
                    self._pk_selected.discard(idx)
                else:
                    self._pk_selected.add(idx)
                self._pk_render()
            elif key == "enter":
                self.run_worker(self._pk_confirm())
            return

        # Normal input mode — history + tab mode
        inp = self.query_one("#user-input")
        if not inp.has_focus:
            return
        if key == "up":
            event.stop()
            event.prevent_default()
            if self._history:
                self._history_idx = max(0, self._history_idx - 1)
                inp.value = self._history[self._history_idx]
                inp.action_end()
        elif key == "down":
            event.stop()
            event.prevent_default()
            if self._history_idx < len(self._history) - 1:
                self._history_idx += 1
                inp.value = self._history[self._history_idx]
                inp.action_end()
            else:
                self._history_idx = len(self._history); inp.value = ""
        elif key == "tab":
            # With a visible lifecycle card, keep Tab as standard Textual
            # focus navigation (Composer -> card). Mode cycling remains the
            # idle/no-card shortcut; Shift+Tab is never consumed here and
            # therefore always follows the reverse focus chain.
            if any(
                getattr(card, "display", True) and card.can_focus
                for card in self.query(ToolCard)
            ):
                return
            idx = MODES.index(self._perm_mode) if self._perm_mode in MODES else 0
            self._perm_mode = MODES[(idx + 1) % len(MODES)]
            self._notify_mode()
            event.stop()
            event.prevent_default()

    # ── Input ────────────────────────────────────────────────────────

    @on(Input.Changed)
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "user-input":
            return
        val = event.value.strip()
        self._slash_dismiss()
        if val.startswith("/"):
            self._slash_show(val)

    @on(Input.Submitted)
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "user-input":
            return
        user_input = event.value.strip()
        if not user_input:
            return
        if user_input.casefold().split() in (["bg", "stop"], ["bg", "stop"]):
            user_input = "/bg stop"
        self.query_one("#welcome").display = False
        if not self._game_blocked:
            self.query_one(MessagesArea).display = True
        event.input.clear()
        if self._ask_user_future is not None and not self._ask_user_future.done():
            self._ask_user_future.set_result(user_input)
            return
        if self._record_input_in_history(user_input):
            self._history.append(user_input)
            self._history_idx = len(self._history)
        self._slash_dismiss()
        if self._pending_game_exit_input is not None:
            answer = user_input.casefold()
            if answer in {"y", "yes", "是"}:
                event.input.disabled = True
                leader_worker = self._active_query_worker
                self._active_query_worker = self.run_worker(
                    self._confirm_game_exit_and_continue(leader_worker),
                    name="confirmed-game-exit",
                    group="leader-query",
                    exit_on_error=False,
                )
                return

            if answer in {"n", "no", "否"}:
                self._pending_game_exit_input = None
                self._restore_game_input_placeholder()
                return
            if self._is_game_control_input(user_input):
                self._pending_game_exit_input = None
            else:
                self.query_one(GameHero).set_notice("输入“是”保存并离开，或输入“否”继续当前对局。")
                return
        if self._game_blocked and not self._is_game_control_input(user_input):
            self._queue_game_exit_confirmation(user_input)
            return
        if not self._game_blocked:
            event.input.disabled = True
        is_game_control = self._game_blocked
        previous_worker = None if is_game_control else self._active_query_worker
        worker = self.run_worker(
            self._dispatch_submitted_input(user_input, previous_worker=previous_worker),
            name="submitted-input",
            group="game-control" if is_game_control else "leader-query",
            exclusive=is_game_control,
            exit_on_error=False,
        )
        if not is_game_control:
            self._active_query_worker = worker

    @staticmethod
    def _record_input_in_history(user_input: str) -> bool:
        """Keep inline provider secrets out of the local input history."""
        parts = user_input.split()
        if not parts or parts[0].lower() != "/models":
            return True
        if len(parts) >= 3 and parts[1].lower() in {"primary", "standby"}:
            subcommand = parts[2].lower()
            if subcommand in {"provider", "url", "model"}:
                return True
            if subcommand == "key":
                return len(parts) == 3
            return False
        return True

    @staticmethod
    def _is_game_control_input(user_input: str) -> bool:
        parts = user_input.split()
        return (
            user_input in {"再来一局", "结束", "继续", "重试", "/exit", "/q", "/quit"}
            or (bool(parts) and parts[0].lower() in {"/bg", "/bg"} and (len(parts) == 1 or parts[1].lower() not in {"start", "replay"}))
        )

    def _queue_game_exit_confirmation(self, user_input: str) -> None:
        self._pending_game_exit_input = user_input
        inp = self.query_one("#user-input")
        inp.placeholder = "是 / 否"
        self.query_one(GameHero).set_notice("是否结束对局并处理刚才的消息？已保存的进度可恢复。输入 是 / 否")
        inp.focus()

    def _restore_game_input_placeholder(self) -> None:
        inp = self.query_one("#user-input")
        inp.disabled = False
        inp.placeholder = "输入命令…"
        self.query_one(GameHero).set_notice(None)
        inp.focus()

    async def _confirm_game_exit_and_continue(self, leader_worker=None) -> None:
        pending = self._pending_game_exit_input
        if pending is None:
            return
        from bglab.tools.bg_play import stop_game
        try:
            result = await asyncio.to_thread(stop_game)
            if not await self._complete_game_shutdown(result):
                return
            self._pending_game_exit_input = None
            if leader_worker is not None and not leader_worker.is_finished:
                try:
                    await leader_worker.wait()
                except Exception:
                    logger.exception("Game leader cleanup failed")
                    self.query_one(MessagesArea).mount(Static(
                        f"  [dim]游戏启动轮收尾失败：{RUNTIME_FAILURE}[/]",
                        classes="log",
                    ))
            self.query_one("#user-input").disabled = True
            await self._dispatch_submitted_input(pending)
        finally:
            inp = self.query_one("#user-input")
            inp.disabled = False
            inp.focus()

    async def _dispatch_submitted_input(self, user_input: str, *, previous_worker=None) -> None:
        try:
            # BgPlay releases its leader after shutdown. Finish that history
            # write before another request uses the same conversation engine.
            # Game controls must bypass this wait so they can stop BgPlay.
            if previous_worker is not None and not previous_worker.is_finished:
                from textual.worker import WorkerCancelled, WorkerFailed
                try:
                    await previous_worker.wait()
                except (WorkerCancelled, WorkerFailed):
                    # The prior worker has finished unwinding; the conversation
                    # can now accept a fresh request after cancellation/failure.
                    pass
            await self._route_submitted_input(user_input)
        except Exception:
            logger.exception("Submitted command failed")
            self._show_game_command_error("操作未完成，请重试。详细原因已记录到本地日志。")
        finally:
            if not self._game_blocked:
                inp = self.query_one("#user-input")
                inp.disabled = False
                inp.focus()

    async def _route_submitted_input(self, user_input: str) -> None:
        if self._game_blocked and user_input in {"继续", "重试"}:
            await self._handle_bg("/bg", "retry")
            return
        if self._game_blocked and user_input == "再来一局":
            from bglab.tools.bg_play import rematch_game
            result = await asyncio.to_thread(rematch_game)
            if result.startswith("ERROR:"):
                self._show_game_command_error(result)
                self.query_one("#user-input").focus()
                return
            self._enter_game_blocked()
            await self._activate_metadata_from_result(result)
            self.query_one("#user-input").focus()
            return
        if self._game_blocked and user_input == "结束":
            from bglab.tools.bg_play import stop_game
            result = await asyncio.to_thread(stop_game)
            await self._complete_game_shutdown(result)
            self.query_one("#user-input").focus()
            return
        if self._game_blocked and not self._is_game_control_input(user_input):
            self.query_one(GameHero).set_notice("请在网页操作；/bg stop 保存并离开对局。")
            self.query_one("#user-input").focus()
            return
        if user_input.startswith("/"):
            await self._handle_slash(user_input)
        else:
            from bglab.slash_commands.bg import (
                GameStartArgumentError,
                parse_bg_manual_test_request,
            )
            try:
                manual_request = parse_bg_manual_test_request(user_input)
            except GameStartArgumentError as exc:
                self.query_one(MessagesArea).mount(Static(
                    f"  [bold {C_RED}]ERROR: {rich_escape(str(exc))}[/]",
                    classes="log",
                ))
                self.query_one("#user-input").focus()
                return
            if manual_request is not None:
                await self._handle_bg(
                    "/bg",
                    f"start manual-test {manual_request['engine']}",
                )
            else:
                await self._process_turn(user_input)

    async def on_unmount(self) -> None:
        """Closing the TUI also closes the owned game frontend and both ports."""
        if self._game_blocked:
            from bglab.tools.bg_play import stop_game
            await asyncio.to_thread(stop_game)
            self._game_blocked = False

    # ── Turn processing ──────────────────────────────────────────────

    def _remove_active_turn_widgets(self) -> None:
        for widget in self._active_turn_widgets:
            try:
                widget.remove()
            except Exception:
                pass
        self._active_turn_widgets = []

    @staticmethod
    def _closure_text(state) -> str | None:
        labels = {
            "completed": "✓ 本轮完成",
            "failed": "✕ 本轮失败",
            "canceled": "取消本轮",
            "stopped": "已停止本轮",
            "protocol_error": "协议异常",
        }
        if state.closure is None:
            return None
        label = labels[state.closure]
        return label

    def _rerender_active_turn(self, state=None) -> None:
        if not self.is_mounted or self._active_reducer is None:
            return
        state = state or self._active_reducer.snapshot()
        msgs = self.query_one(MessagesArea)
        if not self._game_blocked:
            self.query_one("#welcome").display = False
            msgs.display = True
        # Keep mounted ToolCards in place and update their reducer state. A
        # full remove-and-remount pass races Textual's deferred DOM removal:
        # reusing a stable card id in the same message batch raises
        # DuplicateIds and leaves the query worker unfinished. Non-card rows
        # have no ids, so they may be replaced safely.
        previous = list(self._active_turn_widgets)
        retained: list[Static] = []

        def remove(widget: Static) -> None:
            try:
                widget.remove()
            except Exception:
                pass

        def first_matching(predicate) -> Static | None:
            for widget in previous:
                if widget not in retained and predicate(widget):
                    return widget
            return None

        def mount(widget: Static) -> Static:
            msgs.mount(widget)
            retained.append(widget)
            return widget

        assistant_text = "".join(state.assistant_text_segments).strip()
        assistant = first_matching(lambda widget: "assistant-text" in widget.classes)
        # Keep a stable assistant lane from the first lifecycle event. This
        # reserves the timeline slot before a ToolCard is mounted, so a late
        # TEXT frame (END -> RESULT -> TEXT) updates the existing row instead
        # of appending it after the card or racing a deferred DOM removal.
        if assistant is None:
            assistant = mount(AssistantText(classes="assistant-text"))
        assistant.update_markdown(assistant_text)
        if assistant not in retained:
            retained.append(assistant)

        preparing = first_matching(lambda widget: "lifecycle" in widget.classes)
        if state.closure is None:
            if preparing is None:
                preparing = mount(ActivityLine(classes="lifecycle"))
            waiting = self._ask_user_future is not None and not self._ask_user_future.done()
            approval = state.pending_permission is not None or any(tool.state == "approval" for tool in state.tools)
            if waiting:
                label = "等待你的回答"
            elif approval:
                label = "等待你的确认"
            elif any(tool.state == "running" for tool in state.tools):
                label = "正在运行工具"
            elif state.preparing_tool:
                label = "正在准备工具"
            else:
                label = "正在处理请求"
            preparing.set_status(label, active=not (waiting or approval))
            if preparing not in retained:
                retained.append(preparing)
        elif preparing is not None:
            remove(preparing)

        for tool in state.tools:
            card = first_matching(
                lambda widget, tool_use_id=tool.tool_use_id:
                    isinstance(widget, ToolCard)
                    and widget.state.tool_use_id == tool_use_id,
            )
            if card is None:
                card = mount(ToolCard(tool))
            else:
                card.update_state(tool)
                retained.append(card)

        summaries = [*state.file_summaries, *state.test_summaries]
        summary_widgets = [
            widget for widget in previous
            if "summary" in widget.classes and widget not in retained
        ]
        for index, summary in enumerate(summaries):
            if index < len(summary_widgets):
                summary_widget = summary_widgets[index]
                summary_widget.update(f"  [dim]{rich_escape(summary)}[/]")
                retained.append(summary_widget)
            else:
                mount(Static(f"  [dim]{rich_escape(summary)}[/]", classes="summary"))
        for summary_widget in summary_widgets[len(summaries):]:
            remove(summary_widget)

        error_widget = first_matching(lambda widget: "error-summary" in widget.classes)
        if state.turn_error_summary:
            if error_widget is None:
                error_widget = mount(Static("", classes="error-summary"))
            message = "本轮未能完成，请重试。" if state.turn_error_summary == RUNTIME_FAILURE else state.turn_error_summary
            error_widget.update(f"  [{C_RED}]✗[/] {rich_escape(message)}")
            if error_widget not in retained:
                retained.append(error_widget)
        elif error_widget is not None:
            remove(error_widget)

        provider_widget = first_matching(
            lambda widget: "provider-issue" in widget.classes,
        )
        if state.provider_issue is not None:
            issue = state.provider_issue
            if provider_widget is None:
                provider_widget = mount(Static("", classes="provider-issue"))
            provider_widget.update(
                f"  [{C_RED}]✗ {rich_escape(issue.title)}[/]\n"
                f"  [dim]{rich_escape(issue.detail)}[/]",
            )
            if provider_widget not in retained:
                retained.append(provider_widget)
        elif provider_widget is not None:
            remove(provider_widget)

        closure = self._closure_text(state)
        closure_widget = first_matching(lambda widget: "turn-closure" in widget.classes)
        if closure is not None:
            if closure_widget is None:
                closure_widget = mount(Static("", classes="turn-closure"))
            closure_widget.update(f"  {closure}")
            if closure_widget not in retained:
                retained.append(closure_widget)
        elif closure_widget is not None:
            remove(closure_widget)

        for widget in previous:
            if widget not in retained:
                remove(widget)
        self._active_turn_widgets = retained

    @on(ToolCard.Toggle)
    def _on_tool_card_toggle(self, event: ToolCard.Toggle) -> None:
        event.stop()
        if self._active_reducer is None:
            return
        before = self._active_reducer.snapshot()
        if event.tool_key.turn != before.key:
            return
        updated = self._active_reducer.toggle_tool(event.tool_use_id)
        if updated != before:
            self._rerender_active_turn(updated)

    def _reduce_protocol_terminal(self, reason: str = "protocol_error") -> None:
        if self._active_reducer is None:
            return
        state = self._active_reducer.reduce(LoopEvent(
            type=LoopEventType.DONE,
            terminal=TerminalInfo(reason=reason),
        ))
        self._rerender_active_turn(state)

    async def _process_turn(self, user_input: str) -> None:
        self.query_one("#welcome").display = False
        msgs = self.query_one(MessagesArea)
        msgs.display = True
        key = self._next_presentation_key()
        self._active_reducer = PresentationReducer(
            key.session_id, key.turn_id, user_input,
        )
        self._active_turn_widgets = []
        msgs.mount(Static(f"  {rich_escape(user_input)}", classes="user-msg"))
        self._rerender_active_turn()

        final_turns = 0
        authoritative_terminal = False
        assistant_text = ""
        tool_uses: list[ToolUseBlock] = []
        tool_use_ids: set[str] = set()
        tool_results: list[ToolResultBlock] = []
        tool_result_indexes: dict[str, int] = {}
        bg_tool_ids: set[str] = set()
        bg_stop_result = False
        bg_stop_content = ""
        final_usage = None

        try:
            self._query_engine.set_model(self.model)
            self._query_engine.set_permission_mode(self._perm_mode)
            async for event in submit_message(
                query_engine=self._query_engine,
                user_message=user_input,
                messages=self._messages,
                tool_registry=self._tool_registry,
                cwd=self.cwd,
                model=self.model,
                max_turns=50,
                permission_mode=self._perm_mode,
                deps=self._deps,
                session_id=self._transcript_session_id,
                ask_callback=self._tui_ask_callback,
            ):
                if event.type is LoopEventType.DONE and event.terminal is None:
                    self._raw_done_events += 1
                    continue
                if event.type is LoopEventType.TOOL_USE_END and event.tool_use:
                    if event.tool_use.id not in tool_use_ids:
                        tool_use_ids.add(event.tool_use.id)
                        tool_uses.append(event.tool_use)
                    self._record_modified_file(event.tool_use.name, event.tool_use.input)
                    if event.tool_use.name == "BgPlay":
                        bg_tool_ids.add(event.tool_use.id)
                        title = "桌游"
                        from bglab.tools.bg_play import prepare_blocking_bg_tool_call
                        prepare_blocking_bg_tool_call()
                        try:
                            from bglab.games.registry import get_game
                            engine = str(event.tool_use.input.get("engine", "splendor"))
                            title = get_game(engine).title
                        except Exception:
                            pass
                        self._enter_game_blocked(title=title)
                        self._start_game_metadata_watcher()
                if event.type is LoopEventType.TOOL_RESULT and event.tool_result:
                    result = event.tool_result
                    result_index = tool_result_indexes.get(result.tool_use_id)
                    if result_index is None:
                        tool_result_indexes[result.tool_use_id] = len(tool_results)
                        tool_results.append(result)
                    else:
                        tool_results[result_index] = result
                    if result.tool_use_id in bg_tool_ids and (
                        result.metadata.get("game_session_closed") is True
                        or self._game_stop_succeeded(result.content)
                    ):
                        bg_stop_result = True
                        bg_stop_content = "Game stopped" if (
                            result.metadata.get("game_session_closed") is True
                        ) else result.content
                state = self._active_reducer.reduce(event)
                self._rerender_active_turn(state)
                if event.type is LoopEventType.TEXT and event.text:
                    assistant_text += event.text
                if event.type is LoopEventType.DONE and event.terminal is not None:
                    authoritative_terminal = True
                    final_turns = event.terminal.turn_count
                    final_usage = event.usage or event.terminal.total_usage

        except asyncio.CancelledError:
            self._active_reducer.reduce(LoopEvent(
                type=LoopEventType.ERROR, error="Interrupted",
            ))
            self._reduce_protocol_terminal("user_abort")
            self._stop_thinking()
            return
        except Exception:
            logger.exception("Unexpected TUI query failure")
            self._active_reducer.reduce(LoopEvent(
                type=LoopEventType.ERROR, error=RUNTIME_FAILURE,
            ))
            self._rerender_active_turn()

        if not authoritative_terminal:
            self._reduce_protocol_terminal()

        # A BgPlay tool may own the stop lifecycle and return its terminal
        # result directly through the model stream.  Close the presentation
        # only after the reducer has mounted the authoritative terminal row so
        # the passive exit row remains last and idempotent.
        if bg_stop_result and self._game_blocked:
            await self._complete_game_shutdown(bg_stop_content or "Game stopped")

        self._stop_thinking()
        state = self._active_reducer.snapshot()
        visible_text = "".join(state.assistant_text_segments).strip()
        self._total_turns += state.terminal.turn_count if state.terminal else final_turns
        if submit_message is _DEFAULT_TUI_SUBMIT_MESSAGE:
            self._messages = self._query_engine.mutable_messages
            self._perm_mode = self._query_engine.permission_mode
            self.cwd = self._query_engine.cwd
        else:
            # Explicit injected submitters are a deterministic UI-test seam;
            # reconstruct their emitted canonical transcript only for the
            # adapter, never for the production persistent QueryEngine path.
            self._messages.append({
                "role": "user",
                "content": [{"type": "text", "text": user_input}],
            })
            assistant_blocks: list[dict[str, object]] = []
            if visible_text:
                assistant_blocks.append({"type": "text", "text": visible_text})
            assistant_blocks.extend({
                "type": "tool_use",
                "id": tool.id,
                "name": tool.name,
                "input": tool.input,
            } for tool in tool_uses)
            if assistant_blocks:
                self._messages.append({
                    "role": "assistant",
                    "content": assistant_blocks,
                })
            self._messages.extend({
                "role": "user",
                "type": "tool_result",
                "tool_use_id": result.tool_use_id,
                "content": result.content,
                "is_error": result.is_error,
            } for result in tool_results)
            self._query_engine.mutable_messages = self._messages
        if self._status_line:
            self._status_line.show(self._total_turns, final_usage)
        self._refresh_sidebar()

    def _stop_thinking(self) -> None:
        if self._active_thinking:
            self._active_thinking.hide()
            self._active_thinking = None
        if self._thinking_worker:
            self._thinking_worker.cancel()
            self._thinking_worker = None
        self._refresh_sidebar()

    # ── Permission ask callback ──────────────────────────────────────

    async def _ask_user_interactively(self, tool_input: dict) -> str:
        """Pause a model clarification tool until the real input box answers."""
        questions = tool_input.get("questions", []) if isinstance(tool_input, dict) else []
        if not questions:
            return "用户未提供回答。"

        msgs = self.query_one(MessagesArea)
        lines = [f"  [bold {WARNING}]需要你的回答：[/]"]
        for idx, question in enumerate(questions[:4], start=1):
            prompt = str(question.get("question", ""))
            lines.append(f"  {idx}. {rich_escape(prompt)}")
            for option_idx, option in enumerate(question.get("options", [])[:4], start=1):
                label = rich_escape(str(option.get("label", "")))
                description = rich_escape(str(option.get("description", "")))
                lines.append(f"     [{option_idx}] {label} — {description}")
        await msgs.mount(Static("\n".join(lines), classes="log"))
        msgs.scroll_end(animate=False)

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._ask_user_future = future
        for activity in self.query(ActivityLine):
            activity.set_status("等待你的回答", active=False)
        inp = self.query_one("#user-input")
        self._ask_user_previous_placeholder = inp.placeholder
        inp.disabled = False
        inp.placeholder = "请回答上方问题 · Enter 提交 · Ctrl+C 取消"
        inp.focus()
        try:
            answer = await future
            msgs.mount(Static(
                f"  [dim]回答：{rich_escape(str(answer))}[/]",
                classes="log",
            ))
            return f"用户回答：{answer}"
        finally:
            if self._ask_user_future is future:
                self._ask_user_future = None
            for activity in self.query(ActivityLine):
                activity.set_status("正在处理请求")
            if not self._game_blocked:
                inp.disabled = True
            inp.placeholder = self._ask_user_previous_placeholder
            inp.focus()

    async def _ask_permission_via_existing_ui(
        self,
        tool_name: str,
        tool_input: dict,
        reason: str,
    ) -> bool:
        """Await the existing three-choice permission picker."""
        composer = self.query_one("#user-input")
        original_disabled = composer.disabled
        original_placeholder = composer.placeholder
        original_focus = self.focused
        # Keep the Composer disabled and explicitly remove focus while the
        # picker is active. This lets the App-level key handler consume
        # printable y/n, Escape, and arrows instead of the Input widget;
        # finally restores the exact pre-picker state.
        composer.disabled = True
        self.set_focus(None)
        self._perm_info = (tool_name, tool_input, reason)
        self._perm_idx = 0
        try:
            # Render FIRST (dismiss clears _perm_future to None - that's ok,
            # future is created immediately afterwards).
            self._perm_render()
            self._perm_future = asyncio.get_running_loop().create_future()
            result = await self._perm_future
            if result and self._perm_idx == 1:
                
                self._perm_session_allow.add(tool_name)
            return bool(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        finally:
            self._perm_cleanup()
            try:
                composer.placeholder = original_placeholder
                composer.disabled = original_disabled
                if (
                    original_focus is not None
                    and getattr(original_focus, "is_attached", False)
                    and not getattr(original_focus, "disabled", False)
                ):
                    self.set_focus(original_focus)
                else:
                    self.set_focus(None)
            except Exception:
                pass

    async def _tui_ask_callback(self, tool_name: str, tool_input: dict, reason: str) -> bool:
        reducer = self._active_reducer
        if reducer is not None:
            reducer.begin_permission(tool_name, tool_input, reason)
            self._rerender_active_turn()
        try:
            if self._perm_mode == "plan":
                allowed = False
            elif self._perm_mode == "bypass":
                allowed = True
            elif tool_name in self._perm_session_allow:
                allowed = True
            else:
                # Check project-level settings for always-allow.
                from bglab.session.settings import load_settings
                project_rules = load_settings(cwd=self.cwd).get("permission_rules", {})
                if project_rules.get("allow", {}).get(tool_name):
                    allowed = True
                else:
                    allowed = await self._ask_permission_via_existing_ui(
                        tool_name, tool_input, reason,
                    )
        except asyncio.CancelledError:
            if reducer is not None:
                reducer.resolve_permission(False)
                self._rerender_active_turn()
            raise
        if reducer is not None:
            reducer.resolve_permission(bool(allowed))
            self._rerender_active_turn()
        return bool(allowed)

    def _perm_render(self) -> None:
        msgs = self.query_one(MessagesArea)
        self._perm_dismiss_widgets()

        tool_name, tool_input, reason = self._perm_info
        summary = arg_summary(tool_name, tool_input)

        options = [
            ("Allow",             "Allow this tool call once",            C_GREEN),
            ("Allow for session", "Always allow for this session",        C_ORANGE),
            ("Deny",              "Block this tool call",                 C_RED),
        ]

        # Await an explicit answer; there is no automatic approval or timeout.
        head = f"[bold {C_ORANGE}]◉ Permission: [/][{C_CYAN}]{rich_escape(_tool_display_name(tool_name))}[/]({summary})"
        sep = Divider(head)
        msgs.mount(sep); self._perm_widgets.append(sep)

        if reason:
            w = Static(f"  [dim]Reason: {reason[:120]}[/]", classes="log")
            msgs.mount(w); self._perm_widgets.append(w)

        for i, (label, desc, color) in enumerate(options):
            mark = "❯" if i == self._perm_idx else " "
            txt = f"  [{color}]{mark} {label}[/] — [dim]{desc}[/]"
            w = Static(txt, classes="log")
            msgs.mount(w); self._perm_widgets.append(w)

        # Input guide line
        guide = "  [dim]↑↓ 选择 · Enter 确认 · y 同意 · n / Esc 拒绝 · Ctrl+C 取消本次请求[/]"
        w = Static(guide, classes="log")
        msgs.mount(w); self._perm_widgets.append(w)
        tail = Divider()
        msgs.mount(tail); self._perm_widgets.append(tail)

    def _perm_dismiss(self, result: bool) -> None:
        fut = self._perm_future
        if fut and not fut.done():
            fut.set_result(result)

    def _perm_dismiss_widgets(self) -> None:
        """Remove rendered widgets only. Does NOT clear _perm_future (used by ↑↓ re-render)."""
        for w in self._perm_widgets:
            try: w.remove()
            except Exception: pass
        self._perm_widgets = []

    def _perm_cleanup(self) -> None:
        """Full cleanup: dismiss widgets AND clear future. Called when permission dialog ends."""
        self._perm_dismiss_widgets()
        self._perm_future = None



    # ── Slash commands ───────────────────────────────────────────────

    async def _handle_slash(self, user_input: str) -> None:
        self.query_one("#welcome").display = False
        msgs = self.query_one(MessagesArea)
        msgs.display = True
        parts = user_input.split(maxsplit=1)
        cmd = parts[0].lower()
        if cmd == "/bg":
            cmd = "/bg"
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("/help", "/h", "/?"):
            lines = [f"[bold {ACCENT}]Commands:[/]"]
            groups: dict[str, list] = {}
            for command in _live_slash_commands():
                group = command.category or "other"
                aliases = f" ({' '.join(command.aliases)})" if command.aliases else ""
                label = f"  {command.name}{aliases}"
                groups.setdefault(group, []).append(
                    f"{label.ljust(28)} [dim]{command.description}[/]"
                )
            for g, entries in groups.items():
                lines.append(f"\n[bold]{g}[/]")
                lines.extend(entries)
            msgs.mount(Static("\n".join(lines), classes="log"))
            msgs.mount(Divider())

        elif cmd in ("/clear", "/cls"):
            self._transcript_session_id = str(uuid.uuid4())
            self._deps = self._make_deps()
            self._rebuild_query_engine()
            self._perm_session_allow.clear()
            for child in list(msgs.children):
                child.remove()
            self._status_line = StatusLine(model=self.model)
            self._total_turns = 0
            self._refresh_sidebar()
            self.show_toast("Conversation cleared", C_CYAN)

        elif cmd in ("/exit", "/q", "/quit"):
            self.exit()

        elif cmd == "/models":
            await self._handle_models_command(arg, msgs)

        elif cmd == "/model":
            if arg:
                try:
                    self._set_model(arg)
                    msgs.mount(Static(
                        f"  [dim]Model → {rich_escape(self.model)}[/]", classes="log"
                    ))
                except ValueError as exc:
                    msgs.mount(Static(
                        f"  [bold {ERROR}]Model not changed:[/] {rich_escape(str(exc))}",
                        classes="log",
                    ))
            else:
                self._open_provider_slots()

        elif cmd == "/plan":
            self._perm_mode = "plan"
            await self._process_turn(user_input)

        elif cmd == "/default":
            self._perm_mode = "default"
            self._notify_mode()

        elif cmd == "/accept-edits":
            self._perm_mode = "accept_edits"
            self._notify_mode()

        elif cmd == "/resume":
            self._pk_enter("resume")

        elif cmd == "/delete":
            self._pk_enter("delete")

        # ── BG commands ──
        elif cmd == "/bg":
            await self._handle_bg(cmd, arg)
            if not self._bg_poll_timer and self._bg_view is not None:
                self._start_bg_poll()

        else:
            await self._process_turn(user_input)

    # ── Provider/model picker ───────────────────────────────────────

    async def _handle_models_command(self, arg: str, msgs) -> None:
        """Dispatch the additive, non-secret ``/models`` grammar."""
        from bglab.tui.provider_slots_screen import ProviderSlotEdit

        parts = arg.split()
        if not parts:
            self._open_provider_slots()
            return

        if parts[0].lower() == "test":
            if len(parts) != 2 or parts[1].lower() not in {"primary", "standby"}:
                msgs.mount(Static("  [bold]Usage:[/] /models test <primary|standby>", classes="log"))
                return
            slot_id = parts[1].lower()
            slot = self.provider_slots.primary if slot_id == "primary" else self.provider_slots.standby
            if slot is None or not slot.enabled:
                msgs.mount(Static("  [dim]Standby is disabled.[/]", classes="log"))
            else:
                from bglab.llm.provider_probe import probe_provider_slot

                connected = await probe_provider_slot(slot)
                status = "available" if connected else "unavailable"
                msgs.mount(Static(
                    f"  [dim]{slot_id.capitalize()} Provider is {status}.[/]",
                    classes="log",
                ))
            return

        if parts[0].lower() == "disable":
            if len(parts) != 2 or parts[1].lower() != "standby":
                msgs.mount(Static("  [bold]Usage:[/] /models disable standby", classes="log"))
                return
            current = self.provider_slots.standby
            if current is not None:
                self._apply_provider_slot_edit(
                    self._slot_edit_from_slot(current, enabled=False),
                )
            msgs.mount(Static("  [dim]Standby disabled.[/]", classes="log"))
            return

        slot_id = parts[0].lower()
        if slot_id not in {"primary", "standby"}:
            msgs.mount(Static("  [bold]Usage:[/] /models [primary|standby] ...", classes="log"))
            return
        if len(parts) == 1:
            self._open_provider_slots(slot_id=slot_id)  # type: ignore[arg-type]
            return

        subcommand = parts[1].lower()
        if subcommand == "key":
            if len(parts) != 2:
                # Never echo the trailing value: inline keys are not accepted.
                msgs.mount(Static("  [bold]Inline API keys are not accepted.[/] Use `/models <slot> key`.", classes="log"))
                return
            self._open_provider_slots(slot_id=slot_id, focus_key=True)  # type: ignore[arg-type]
            return
        if len(parts) != 3:
            msgs.mount(Static("  [bold]Inline API keys are not accepted.[/] Use `/models <slot> key`.", classes="log"))
            return

        current = self.provider_slots.primary if slot_id == "primary" else self.provider_slots.standby
        if current is None:
            current = ProviderSlot(
                id="standby",
                provider=self.provider_slots.primary.provider,
                model=self.provider_slots.primary.model,
                base_url=self.provider_slots.primary.base_url,
                credential_env=f"{self.provider_slots.primary.credential_env}_STANDBY",
                enabled=False,
            )
        value = parts[2]
        try:
            if subcommand == "provider":
                provider = next(item for item in PROVIDERS if item.id == value)
                base_url = provider.openai_base_url if current.provider != value else current.base_url
                edit = ProviderSlotEdit(
                    slot_id=slot_id, provider=value, model=current.model,
                    base_url=base_url, enabled=True,
                )
            elif subcommand == "url":
                if not value.startswith(("http://", "https://")):
                    raise ValueError("URL must start with http:// or https://")
                edit = ProviderSlotEdit(
                    slot_id=slot_id, provider=current.provider, model=current.model,
                    base_url=value, enabled=True,
                )
            elif subcommand == "model":
                if not value or "/" in value:
                    raise ValueError("model ID must be non-empty and must not contain '/'")
                edit = ProviderSlotEdit(
                    slot_id=slot_id, provider=current.provider, model=value,
                    base_url=current.base_url, enabled=True,
                )
            else:
                raise ValueError("unknown sub-command")
        except ValueError:
            msgs.mount(Static("  [bold]Invalid /models argument.[/]", classes="log"))
            return
        self._apply_provider_slot_edit(edit)
        msgs.mount(Static(f"  [dim]Updated {slot_id} {subcommand}.[/]", classes="log"))

    def _open_provider_slots(self, *, slot_id: str = "primary", focus_key: bool = False) -> None:
        from bglab.tui.provider_slots_screen import ProviderSlotsScreen
        from bglab.llm.provider_probe import probe_provider_slot

        screen = ProviderSlotsScreen(
            self.provider_slots,
            cwd=self.cwd,
            initial_slot=slot_id,  # type: ignore[arg-type]
            focus_key=focus_key,
            probe=probe_provider_slot,
        )
        self.push_screen(screen, self._provider_slots_closed)

    def _provider_slots_closed(self, edit) -> None:
        if edit is not None:
            self._apply_provider_slot_edit(edit)
            self.query_one("#user-input", Input).placeholder = "输入 /bg start 选择桌游，或直接提问"
            self.query_one(MessagesArea).mount(Static(
                f"[bold {SUCCESS}]配置已保存[/] · {rich_escape(edit.provider)}/{rich_escape(edit.model)}\n"
                "下一步：输入 /bg start 选择桌游。",
                classes="log",
            ))
        self._refresh_sidebar()
        self.query_one("#user-input", Input).focus()
        self.query_one(MessagesArea).scroll_end(animate=False)

    def _slot_edit_from_slot(self, slot: ProviderSlot, *, enabled: bool | None = None):
        from bglab.tui.provider_slots_screen import ProviderSlotEdit

        return ProviderSlotEdit(
            slot_id=slot.id,
            provider=slot.provider,
            model=slot.model,
            base_url=slot.base_url,
            enabled=slot.enabled if enabled is None else enabled,
            chat_stream=slot.chat_stream,
        )

    def _apply_provider_slot_edit(self, edit) -> None:
        from bglab.session.settings import save_setting
        from bglab.llm.provider_slots import load_provider_slot_policy

        provider = next(item for item in PROVIDERS if item.id == edit.provider)
        slot = ProviderSlot(
            id=edit.slot_id,
            provider=edit.provider,
            model=edit.model,
            base_url=edit.base_url,
            credential_env=provider.api_key_env,
            enabled=edit.enabled,
            chat_stream=edit.chat_stream,
        )
        if edit.slot_id == "primary":
            provisional = ProviderSlotPolicy(
                primary=slot,
                standby=self.provider_slots.standby,
                max_attempts=self.provider_slots.max_attempts,
            )
        else:
            provisional = ProviderSlotPolicy(
                primary=self.provider_slots.primary,
                standby=slot,
                max_attempts=self.provider_slots.max_attempts,
            )
        normalized = load_provider_slot_policy({
            "model": provisional.primary.reference,
            "provider_slots": {
                "primary": provisional.primary.to_settings(),
                "standby": provisional.standby.to_settings() if provisional.standby else None,
                "maxAttempts": provisional.max_attempts,
            },
        })
        self.provider_slots = normalized
        if edit.slot_id == "primary":
            self.model = normalized.primary.reference
            if hasattr(self, "_query_engine"):
                self._query_engine.set_model(self.model)
            from bglab.session.state import session_state
            session_state.model = self.model
            save_setting("model", self.model)
            if self._status_line:
                self._status_line.model = self.model
                self._status_line._refresh()
        save_setting("provider_slots", self.provider_slots.to_settings())
        self._refresh_sidebar()

    def _set_model(self, reference: str) -> None:
        from bglab.llm.providers import canonical_model_reference
        from bglab.session.settings import save_setting
        from bglab.session.state import session_state

        selected = canonical_model_reference(reference)
        self.model = selected
        if hasattr(self, "_query_engine"):
            self._query_engine.set_model(selected)
        session_state.model = selected
        save_setting("model", selected)
        provider_id, model_id = selected.split("/", 1)
        provider = next(item for item in PROVIDERS if item.id == provider_id)
        current = self.provider_slots.primary
        base_url = current.base_url if current.provider == provider_id else provider.openai_base_url
        from bglab.tui.provider_slots_screen import ProviderSlotEdit
        self._apply_provider_slot_edit(ProviderSlotEdit(
            slot_id="primary",
            provider=provider_id,
            model=model_id,
            base_url=base_url,
            enabled=True,
        ))
        if self._status_line:
            self._status_line.model = selected
            self._status_line._refresh()
        self._refresh_sidebar()

    # ── BG Game Commands ───────────────────────────────────────────

    async def _handle_bg(self, cmd: str, arg: str) -> None:
        if cmd == "/bg":
            cmd = "/bg"
        msgs = self.query_one(MessagesArea)
        parts = arg.split(maxsplit=1)
        subcommand = parts[0].lower() if parts else ""

        if cmd == "/bg" and arg.strip() == "start":
            from bglab.games.registry import discover_games

            lines = [f"[bold {TEXT}]选择一款桌游[/]", ""]
            for game in discover_games().values():
                count = str(game.min_players) if game.min_players == game.max_players else f"{game.min_players}–{game.max_players}"
                lines.extend([
                    f"[bold]{rich_escape(game.title)}[/]  [dim]{count} 人[/]",
                    f"  /bg start {game.id} human {game.min_players}",
                    "",
                ])
            lines.append("输入上方命令开始游戏。省略 human 可观看 AI 对战。")
            msgs.mount(Static("\n".join(lines), classes="log"))
            msgs.scroll_end(animate=False)
            return

        if cmd == "/bg" and subcommand == "start":
            _parts = arg.split()
            from bglab.slash_commands.bg import (
                GameStartArgumentError,
                parse_bg_start_args,
            )
            from bglab.games.registry import get_game
            try:
                _start_args = parse_bg_start_args(_parts[1:])
            except GameStartArgumentError as exc:
                msgs.mount(Static(
                    f"  [bold {C_RED}]ERROR: {rich_escape(str(exc))}[/]",
                    classes="log",
                ))
                return
            _definition = get_game(_start_args["engine"])
            from bglab.tools.bg_play import _bg_tool_call
            # Expose the game surface before startup work begins so /bg stop
            # remains available while teams, adapters, and ports are starting.
            self._bg_starting = True
            self._enter_game_blocked(
                title=_definition.title,
                url=None,
            )
            try:
                result = await asyncio.to_thread(
                    _bg_tool_call,
                    _start_args,
                )
            finally:
                self._bg_starting = False
            if result.startswith("ERROR:") or result.startswith("Game start cancelled"):
                msgs.mount(Static(
                    f"  [{C_RED}]启动未完成：{rich_escape(result.removeprefix('ERROR:').strip())}[/]",
                    classes="log",
                ))
                if self._game_blocked:
                    self._leave_game_blocked()
            elif self._game_blocked:
                await self._activate_metadata_from_result(result)

        elif cmd == "/bg" and arg and arg.split(maxsplit=1)[0].lower() == "replay":
            from bglab.tools.bg_play import start_replay
            _replay_parts = arg.split(maxsplit=1)
            _game_id = _replay_parts[1].strip() if len(_replay_parts) > 1 else None
            result = await asyncio.to_thread(start_replay, _game_id)
            msgs.mount(Static(
                f"  [bold {WARNING}]{rich_escape(result)}[/]",
                classes="log",
            ))
            if not result.startswith("ERROR:"):
                _replay_url = "http://localhost:8080/replay"
                for _line in result.splitlines():
                    if _line.startswith("回放页面:"):
                        _replay_url = _line.split(":", 1)[1].strip()
                        break
                self._enter_game_blocked(title="历史回放", url=_replay_url)

        elif cmd == "/bg" and subcommand == "stop":
            from bglab.tools.bg_play import stop_game
            result = await asyncio.to_thread(stop_game)
            await self._complete_game_shutdown(result)

        elif cmd == "/bg" and subcommand == "resume":
            from bglab.tools.bg_play import resume_game
            parts = arg.split(maxsplit=1)
            game_id = parts[1].strip() if len(parts) > 1 else None
            if game_id is None:
                from bglab.games.persistence.store import GameStore
                from bglab.games.registry import get_game, GameRegistryError
                from bglab.tui.game_resume_screen import GameResumeScreen
                stores = await asyncio.to_thread(GameStore.unfinished)
                if len(stores) > 1:
                    games = []
                    for store in stores:
                        try:
                            manifest = store.read_manifest()
                        except (OSError, ValueError):
                            continue
                        if not isinstance(manifest, dict):
                            continue
                        engine = manifest.get("engine", "")
                        try:
                            title = get_game(engine).title
                        except GameRegistryError:
                            title = engine or "桌游"
                        games.append({**manifest, "game_id": store.game_id, "title": title})
                    if not games:
                        self._show_game_command_error("暂时没有可读取的对局存档。")
                        return
                    self.push_screen(GameResumeScreen(games), self._resume_selected_game)
                    return
            result = await asyncio.to_thread(resume_game, game_id)
            if result.startswith("ERROR:"):
                self._show_game_command_error(result)
            else:
                self._enter_game_blocked()
                await self._activate_metadata_from_result(result)

        elif cmd == "/bg" and subcommand == "retry":
            from bglab.tools.bg_play import retry_game
            parts = arg.split(maxsplit=1)
            game_id = parts[1].strip() if len(parts) > 1 else None
            result = await asyncio.to_thread(retry_game, game_id)
            if result.startswith("ERROR:"):
                self._show_game_command_error(result)
            else:
                self._enter_game_blocked()
                await self._activate_metadata_from_result(result)

        elif cmd == "/bg" and not arg:
            # /bg alone — show help
            msgs.mount(Static(
                f"  [bold {ACCENT}]桌游命令:[/]\n" + rich_escape(
                "  /bg start [游戏名] [human] [人数] — 开启网页对局\n"
                "  /bg stop                  — 关闭并返回 Code Agent\n"
                "  /bg retry [game_id]       — 重试同一已暂停局面\n"
                "  /bg resume [game_id]      — 选择存档对局，或按 ID 恢复\n"
                "  /bg replay [game_id]      — 打开历史只读回放"),
                classes="log"))
        else:
            self._show_game_command_error("只支持 /bg start、/bg stop、/bg retry、/bg resume、/bg replay")

    def _show_game_command_error(self, result: str) -> None:
        if self._game_blocked:
            self.query_one(GameHero).set_notice(result)
        else:
            self.query_one(MessagesArea).mount(Static(
                f"  [{C_RED}]{rich_escape(result)}[/]", classes="log",
            ))

    def _resume_selected_game(self, game_id: str | None) -> None:
        if game_id is None:
            return
        command = f"/bg resume {game_id}"
        current = getattr(self, "_game_session", None)
        if self._game_blocked and (current is None or current.game_id != game_id):
            self._queue_game_exit_confirmation(command)
            return
        previous = None if self._game_blocked else self._active_query_worker
        worker = self.run_worker(
            self._dispatch_submitted_input(command, previous_worker=previous),
            group="game-control" if self._game_blocked else "leader-query",
            exit_on_error=False,
        )
        if not self._game_blocked:
            self._active_query_worker = worker

    async def _activate_metadata_from_result(self, result: str) -> None:
        """Bind the exact adapter metadata when a start/resume result has an id."""
        match = re.search(
            r"(?:game_id\s*=\s*|Game\s+)([A-Za-z0-9_-]{1,64})",
            result or "",
        )
        if match is None:
            return
        try:
            from bglab.tools.bg_play import get_presentation_metadata

            metadata = await asyncio.to_thread(
                get_presentation_metadata, match.group(1),
            )
            await self._activate_game_session(metadata)
        except (OSError, ValueError, TypeError) as exc:
            # Startup can legitimately return before the manifest is visible;
            # keep the preparing Hero and let the lifecycle owner retry.
            self._record_game_diagnostic("game metadata", exc)

    def _enter_game_blocked(
        self, *, title: str = "桌游", url: str | None = None,
    ) -> None:
        self._game_enter_epoch = getattr(self, "_game_enter_epoch", 0) + 1
        self._cancel_game_metadata_watcher()
        self._game_blocked = True
        self._pending_game_exit_input = None
        self._last_game_exit_title = title or "桌游"
        self._last_game_report_count = 0
        inp = self.query_one("#user-input")
        inp.disabled = False
        inp.placeholder = "输入命令…"
        inp.focus()
        msgs = self.query_one(MessagesArea)
        surface = self.query_one(GameSurface)
        surface.prepare(title=title, url=url)
        self._bg_view = surface
        msgs.display = False
        surface.display = True
        self.query_one("#welcome").display = False
        self.query_one("#app-header").display = False
        self.query_one(GameHero).set_notice(None)
        self._game_ui_metadata = None
        sidebar_query = self.query(BrandSidebar)
        if sidebar_query.nodes:
            sidebar_query.nodes[0].set_mode("bg", surface)
        self._refresh_composer_hints()

    def action_open_game(self) -> None:
        if not self._game_blocked or len(self.screen_stack) > 1:
            return
        url = self.query_one(GameHero)._url
        if url:
            self.open_url(url)

    def _leave_game_blocked(self, *, report_count: int | None = None) -> None:
        if not getattr(self, "_game_blocked", False):
            return
        session_epoch = getattr(self, "_game_enter_epoch", 0)
        title = getattr(self, "_last_game_exit_title", "桌游") or "桌游"
        if report_count is None:
            report_count = getattr(self, "_last_game_report_count", 0)
        self._last_game_report_count = max(0, int(report_count))
        self._game_blocked = False
        self._cancel_game_metadata_watcher()
        self._pending_game_exit_input = None
        inp = self.query_one("#user-input")
        inp.disabled = False
        inp.placeholder = "输入 /bg start 开始游戏，或 /help 查看命令"
        inp.focus()
        msgs = self.query_one(MessagesArea)
        append_exit_row(
            msgs,
            title,
            self._last_game_report_count,
            session_token=session_epoch,
        )
        msgs.display = True
        self.query_one("#app-header").display = True
        self.query_one(GameSurface).display = False
        sidebar_query = self.query(BrandSidebar)
        if sidebar_query.nodes:
            sidebar_query.nodes[0].set_mode("code")
        self._refresh_composer_hints()
        if self._bg_poll_timer:
            self._bg_poll_timer.cancel()
            self._bg_poll_timer = None
        self._game_ui_metadata = None
        self._bg_view = None

    def _start_bg_poll(self) -> None:
        """Compatibility no-op; durable polling belongs to the session owner."""
        async def _poll():
            while self._bg_view is not None:
                await asyncio.sleep(1.5)
        self._bg_poll_timer = asyncio.create_task(_poll())

    # ── Slash command popup ──────────────────────────────────────────

    def _slash_show(self, text: str) -> None:
        prefix = text.lstrip("/").lower()
        matched = [
            command
            for command in _live_slash_commands()
            if not prefix
            or prefix in command.name.lstrip("/").lower()
            or any(prefix in alias.lstrip("/").lower() for alias in command.aliases)
        ]
        if not matched:
            return
        groups: dict[str, list] = {}
        for command in matched:
            group = command.category or "other"
            aliases = f" ({' '.join(command.aliases)})" if command.aliases else ""
            label = f"  {command.name}{aliases}"
            groups.setdefault(group, []).append(
                f"{label.ljust(26)} [dim]{command.description}[/]"
            )
        lines = [f"[bold {C_ORANGE}]slash palette · ↑↓ navigate · Enter pick · Esc dismiss[/]"]
        for g, entries in groups.items():
            lines.append(f"\n[bold]{g}[/]")
            lines.extend(entries)
        self._slash_dismiss()
        w = Static("\n".join(lines))
        self._slash_widgets.append(w)
        input_bar = self.query_one("#input-bar")
        input_bar.styles.height = "auto"
        input_bar.styles.max_height = max(8, self.size.height - 4)
        input_bar.styles.overflow_y = "auto"
        input_bar.mount(w, after=0)

    def _slash_dismiss(self) -> None:
        for w in self._slash_widgets:
            try: w.remove()
            except Exception: pass
        self._slash_widgets = []
        if self.is_mounted:
            try:
                input_bar = self.query_one("#input-bar")
            except Exception:
                return
            input_bar.styles.height = 4
            input_bar.styles.max_height = None
            input_bar.styles.overflow_y = "hidden"

    # ── Session picker ───────────────────────────────────────────────

    def _pk_enter(self, mode: str) -> None:
        msgs = self.query_one(MessagesArea)
        try:
            from bglab.persistence import get_session_previews
            sessions = get_session_previews(self.cwd, limit=50, include_game=False)
            if not sessions:
                msgs.mount(Static("  [dim]没有已保存的对话。游戏存档请用 /bg resume。[/]", classes="log"))
                return
            self._pk_sessions = sessions
            self._pk_mode = mode
            self._pk_idx = 0
            self._pk_selected = set()
            self._pk_widgets = []
            self._pk_render()
        except Exception as e:
            msgs.mount(Static(f"  [dim]Failed to list sessions: {e}[/]", classes="log"))

    def _pk_render(self) -> None:
        msgs = self.query_one(MessagesArea)
        for w in self._pk_widgets:
            try: w.remove()
            except Exception: pass
        self._pk_widgets = []

        sessions = self._pk_sessions
        if not sessions:
            return
        idx = self._pk_idx
        is_delete = self._pk_mode == "delete"
        action = "恢复对话" if self._pk_mode == "resume" else "删除对话"
        color = C_RED if is_delete else C_ORANGE

        hint = "↑↓ 选择 · Enter 确定 · Esc 返回"
        if is_delete:
            sel_count = len(self._pk_selected)
            hint += f" · space toggle · {sel_count} selected"
        head = f" {action} ({idx + 1}/{len(sessions)}) · {hint} "
        sep = Divider(f"[{color}]{head}[/]", color=color)
        msgs.mount(sep); self._pk_widgets.append(sep)

        for i, s in enumerate(sessions):
            sid = s.get("session_id", "")[:8]
            name = rich_escape(s.get("title", "") or sid)
            is_current = i == idx

            if is_delete:
                checked = "[✓]" if i in self._pk_selected else "[ ]"
                mark = f"❯ {checked}" if is_current else f"  {checked}"
                color_tag = color if i in self._pk_selected or is_current else C_DIM
                bold_open = "[bold]" if is_current else ""
                bold_close = "[/]" if is_current else ""
            else:
                mark = "❯" if is_current else " "
                color_tag = color if is_current else C_DIM
                bold_open = "[bold]" if is_current else ""
                bold_close = "[/]" if is_current else ""

            model = rich_escape(s.get("model", ""))
            msgs_count = s.get("message_count", 0)
            sz = fmt_size(s.get("size", 0))
            mod = relative_time(s.get("modified", ""))
            detail = f"  [{color_tag}]{bold_open}{mark} {name}{bold_close}[/]"
            detail += f"\n    [dim]{model + ' · ' if model else ''}{msgs_count} msgs · {mod} · {sz}[/]"
            w = Static(detail, classes="log")
            msgs.mount(w); self._pk_widgets.append(w)

        bottom = Divider()
        msgs.mount(bottom); self._pk_widgets.append(bottom)

        # Auto-scroll to selected session
        selected_widget_idx = idx + 1  # _pk_widgets: [sep, item0, item1, ..., bottom]
        if selected_widget_idx < len(self._pk_widgets):
            msgs.scroll_to_widget(self._pk_widgets[selected_widget_idx], animate=False)

    def _pk_dismiss(self) -> None:
        for w in self._pk_widgets:
            try: w.remove()
            except Exception: pass
        self._pk_widgets = []
        self._pk_sessions = None

    async def _pk_confirm(self) -> None:
        sessions = self._pk_sessions
        if not sessions:
            self._pk_dismiss()
            return
        mode = self._pk_mode
        msgs = self.query_one(MessagesArea)

        if mode == "delete":
            # Multi-delete: all selected items, or fallback to current
            selected = self._pk_selected if self._pk_selected else {min(self._pk_idx, len(sessions) - 1)}
            deleted = 0
            for idx in sorted(selected, reverse=True):
                s = sessions[idx]
                try:
                    os.remove(s["path"])
                    deleted += 1
                except Exception as e:
                    msgs.mount(Static(
                        f"  [dim]Failed to delete {s.get('session_id', '')[:8]}: {e}[/]",
                        classes="log"))
            self._pk_dismiss()
            msgs.mount(Static(
                f"  [dim]Deleted {deleted} session{'s' if deleted != 1 else ''}[/]", classes="log"))
            msgs.mount(Divider())
            return
        else:
            selected = sessions[min(self._pk_idx, len(sessions) - 1)]
            path = selected["path"]
            self._pk_dismiss()
            try:
                from bglab.persistence import load_messages_from_boundary, load_transcript
                history_messages = load_transcript(path)
                msgs_loaded, _ = load_messages_from_boundary(path)
                if not msgs_loaded:
                    raise ValueError("Selected transcript has no recoverable messages")
                self._messages = msgs_loaded
                self._transcript_session_id = selected["session_id"]
                self._deps = self._make_deps()
                self._rebuild_query_engine(msgs_loaded)
                await msgs.remove_children()
                self.query_one("#welcome").display = False
                msgs.display = True
                self._history = []
                self._perm_session_allow.clear()
                self._modified_files = []
                self._active_reducer = None
                self._active_turn_widgets = []
                sid = selected.get("session_id", "")[:8]
                msgs.mount(Static(f"  [dim]已恢复对话 {sid}[/]",
                                  classes="log"))

                # Display stored history independently of the model's compact context.
                for msg in history_messages:
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    msg_type = msg.get("type", "")
                    if msg.get("_is_meta") or msg.get("_compact_boundary"):
                        continue
                    if msg_type == "tool_result":
                        tr = ToolResultLine()
                        tr.show(str(content)[:500], is_error=bool(msg.get("is_error", False)))
                        msgs.mount(tr)
                        continue
                    if role == "user":
                        text = _extract_text(content)
                        if text:
                            msgs.mount(Static(rich_escape(text), classes="user-msg"))
                            self._history.append(text)
                    elif role == "assistant":
                        if isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict):
                                    if block.get("type") == "text":
                                        t = block.get("text", "")
                                        if t.strip():
                                            msgs.mount(AssistantText(t.strip(), classes="assistant-text"))
                                    elif block.get("type") == "tool_use":
                                        self._record_modified_file(block.get("name", ""), block.get("input", {}))
                                        tl = ToolUseLine()
                                        tl.show(block.get("name", "?"), block.get("input", {}))
                                        msgs.mount(tl)
                        elif isinstance(content, str) and content.strip():
                            msgs.mount(AssistantText(content.strip(), classes="assistant-text"))
                self._status_line = StatusLine(model=self.model)
                await msgs.mount(self._status_line, Divider())
                self._history_idx = len(self._history)
                self.query_one("#user-input", Input).focus()
                self.call_after_refresh(msgs.scroll_end, animate=False, immediate=True)
                self._total_turns = 0
                self._refresh_sidebar()
            except Exception:
                logger.exception("Session resume failed")
                msgs.mount(Static(
                    f"  [dim]Failed to resume: {RUNTIME_FAILURE}[/]",
                    classes="log",
                ))

    # ── Helpers ──────────────────────────────────────────────────────

    def _notify_mode(self) -> None:
        self._query_engine.set_permission_mode(self._perm_mode)
        msgs = self.query_one(MessagesArea)
        label = MODE_LABEL.get(self._perm_mode, self._perm_mode)
        color = MODE_BORDER.get(self._perm_mode, C_ORANGE)
        msgs.mount(Static(f"  [dim]mode → [{color}]{label}[/][/]", classes="log"))
        self.show_toast(f"mode → {label}", color)

    async def _submit_one_shot(self) -> None:
        if not self._one_shot_prompt:
            return
        await self._process_turn(self._one_shot_prompt)
        self._one_shot_prompt = None
        self.set_timer(0.5, self.action_quit)


# ══════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════

def main():
    load_env()

    import io as _io
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = _io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = _io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    import argparse
    parser = argparse.ArgumentParser(prog="bglab-textual")
    parser.add_argument("-m", "--model", default="deepseek-chat")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("-p", "--prompt", default=None)
    args = parser.parse_args()

    app = BglabREPL(model=args.model, cwd=args.cwd, one_shot_prompt=args.prompt)
    app.run()


if __name__ == "__main__":
    main()
