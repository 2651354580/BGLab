"""Prompt viewer — captures and displays the complete assembled prompt for /show command.

Three parts of every LLM call:
  1. System Prompt (15+ sections from system_prompt.py)
  2. User Context (CLAUDE.md + date + envInfo — injected as <system-reminder> in message[0])
  3. System Context (git status — appended to system prompt)

Plus the full message list including all _is_meta injections (user context, deferred tools,
system attachments, delta attachments, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from io import StringIO

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

C_TITLE = "#FF8800"
C_LABEL = "#00D7D7"
C_META = "#888888"
C_SECTION = "#FFFFFF"
C_DIM = "#666666"
C_USER = "#5FAFD7"
C_ASSISTANT = "#C4A000"


@dataclass
class AssemblySnapshot:
    """Complete assembled prompt sent to the LLM in a single turn."""
    # Part 1: System prompt
    system_prompt: str = ""
    system_prompt_sections: list[str] = field(default_factory=list)

    # Part 2: User context (injected as <system-reminder> in message[0])
    user_context: dict[str, str] = field(default_factory=dict)

    # Part 3: System context (appended to system prompt)
    system_context: dict[str, str] = field(default_factory=dict)

    # Full message list with all _is_meta injections
    messages: list[dict[str, Any]] = field(default_factory=list)

    model: str = ""
    turn_count: int = 0
    tools_count: int = 0
    tool_names: list[str] = field(default_factory=list)


_last: AssemblySnapshot | None = None


def capture(
    *,
    system_prompt: str = "",
    system_prompt_sections: list[str] | None = None,
    user_context: dict[str, str] | None = None,
    system_context: dict[str, str] | None = None,
    messages: list[dict[str, Any]] | None = None,
    tools: list[Any] | None = None,
    model: str = "",
    turn_count: int = 0,
) -> AssemblySnapshot:
    """Capture the complete assembled prompt snapshot.

    Called from query_loop after all assembly is done but before the LLM call.
    """
    global _last
    snap = AssemblySnapshot(
        system_prompt=system_prompt,
        system_prompt_sections=list(system_prompt_sections) if system_prompt_sections else [],
        user_context=dict(user_context) if user_context else {},
        system_context=dict(system_context) if system_context else {},
        messages=list(messages) if messages else [],
        model=model,
        turn_count=turn_count,
        tools_count=len(tools) if tools else 0,
        tool_names=[t.name if hasattr(t, 'name') else str(t) for t in (tools or [])[:20]],
    )
    _last = snap
    return snap


def get_last() -> AssemblySnapshot | None:
    return _last


# ═══════════════════════════════════════════════════════════════
# Display helpers
# ═══════════════════════════════════════════════════════════════

def _line_count(text: str) -> int:
    return (text or "").count("\n") + 1 if text else 0


def _first_line(text: str, width: int = 120) -> str:
    """First non-empty line, truncated."""
    for line in (text or "").split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped[:width]
    return "(empty)"


def _detect_meta_kind(msg: dict) -> str:
    """Detect the type of a _is_meta message."""
    content = msg.get("content", "")
    if isinstance(content, list):
        text = " ".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    else:
        text = str(content)

    if "<available-deferred-tools>" in text:
        return "deferred_tools"
    if "deferred_tools_delta" in text:
        return "deferred_tools_delta"
    if "agent_listing_delta" in text:
        return "agent_listing_delta"
    if "mcp_instructions_delta" in text:
        return "mcp_instructions_delta"
    if "<system-reminder>" in text:
        if "claudeMd" in text or "currentDate" in text or "IMPORTANT: this context" in text:
            return "user_context"
        if "Files modified this turn" in text:
            return "changed_files"
        if "plan mode" in text.lower():
            return "plan_mode"
        if "date has changed" in text.lower():
            return "date_change"
        if "pending tasks" in text.lower():
            return "todo"
        if "just compacted" in text.lower():
            return "compaction"
        if "memory files" in text.lower():
            return "auto_memory"
        if "additional tools" in text or "ToolSearch" in text:
            return "deferred_list"
        return "system_reminder"
    if "<task_notification>" in text:
        return "bg_agent"
    return "meta"


def _get_display_role(msg: dict) -> str:
    """Get display role, accounting for tool_use/tool_result content blocks."""
    # Top-level type: tool_result
    if msg.get("type") == "tool_result":
        return "tool_result"

    role = msg.get("role", "?")
    content = msg.get("content", "")

    if isinstance(content, list):
        block_types: list[str] = []
        for b in content:
            if isinstance(b, dict):
                bt = b.get("type", "")
                if bt in ("tool_use", "tool_result") and bt not in block_types:
                    block_types.append(bt)
        if block_types:
            return f"{role} [{', '.join(block_types)}]"

    return role


def _extract_content_text(msg: dict, *, preview: bool = False) -> str:
    """Extract readable text from message content, including tool blocks.

    Args:
        msg: The message dict.
        preview: If True, return a short single-line preview.
    """
    content = msg.get("content", "")

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if not isinstance(b, dict):
                parts.append(str(b))
                continue
            bt = b.get("type", "text")
            if bt == "text":
                parts.append(b.get("text", ""))
            elif bt == "tool_use":
                name = b.get("name", "?")
                inp = b.get("input", {})
                parts.append(f"[{name}({_fmt_input(inp, preview)})]")
            elif bt == "tool_result":
                c = str(b.get("content", ""))
                if preview and len(c) > 80:
                    c = c[:80] + "..."
                parts.append(c)
        return "\n".join(parts)

    return str(content)


def _fmt_input(inp: dict, preview: bool) -> str:
    """Format tool input for display."""
    # Show first key=value pair, truncate long values
    if not inp:
        return ""
    items = list(inp.items())
    if not items:
        return ""
    k, v = items[0]
    s = f"{k}={v!r}"
    if preview and len(s) > 60:
        s = s[:60] + "..."
    return s


# ═══════════════════════════════════════════════════════════════
# Overview display
# ═══════════════════════════════════════════════════════════════

def overview(snap: AssemblySnapshot | None) -> Panel | Text:
    """Build a Rich Panel showing the complete prompt overview.

    Returns a Panel for inline display, or a Text message if no data.
    """
    if snap is None:
        return Text(
            "\nNo prompt data captured yet.\n\n"
            "Send a message and then use /show.\n",
            style=C_DIM,
        )

    parts: list[Text] = []

    # ── Header bar ──
    parts.append(Text(
        f"Turn #{snap.turn_count}    Model: {snap.model}    "
        f"Tools: {snap.tools_count}    Messages: {len(snap.messages)}",
        style=f"bold {C_DIM}",
    ))
    parts.append(Text(""))

    # ── Part 1: System Prompt ──
    parts.append(Text("── Part 1: System Prompt ", style=f"bold {C_LABEL}"))

    if snap.system_prompt_sections:
        parts.append(Text(
            f"   {len(snap.system_prompt_sections)} sections, {len(snap.system_prompt):,} chars\n",
            style=C_DIM,
        ))
        for i, section in enumerate(snap.system_prompt_sections):
            fl = _first_line(section, 100)
            lc = _line_count(section)
            parts.append(Text(f"  [{i:2d}] ", style=C_LABEL))
            if fl:
                parts.append(Text(f"{fl}\n", style=C_SECTION))
            parts.append(Text(
                f"       {lc} lines — /show prompt {i}\n",
                style=C_DIM,
            ))
        parts.append(Text("  /show prompt all  — full system prompt in pager\n", style=C_DIM))
    else:
        parts.append(Text(
            "   (not yet captured — send a message first)\n",
            style=C_DIM,
        ))
    parts.append(Text(""))

    # ── Part 2: User Context ──
    parts.append(Text("── Part 2: User Context ", style=f"bold {C_LABEL}"))
    parts.append(Text("(injected as <system-reminder> in message[0])\n", style=C_DIM))

    if snap.user_context:
        for key, value in snap.user_context.items():
            lc = _line_count(value)
            fl = _first_line(value, 100)
            parts.append(Text(f"  {key}  ({lc} lines)\n", style=f"bold {C_SECTION}"))
            if fl:
                parts.append(Text(f"    {fl}\n", style=C_META))
            parts.append(Text(f"    /show context {key}\n", style=C_DIM))
    else:
        parts.append(Text("  (empty)\n", style=C_DIM))

    parts.append(Text(""))

    # ── Part 3: System Context ──
    parts.append(Text("── Part 3: System Context ", style=f"bold {C_LABEL}"))
    parts.append(Text("(appended to system prompt)\n", style=C_DIM))

    if snap.system_context:
        for key, value in snap.system_context.items():
            lc = _line_count(value)
            fl = _first_line(value, 100)
            parts.append(Text(f"  {key}  ({lc} lines)\n", style=f"bold {C_SECTION}"))
            if fl:
                parts.append(Text(f"    {fl}\n", style=C_META))
            parts.append(Text(f"    /show context {key}\n", style=C_DIM))
    else:
        parts.append(Text("  (empty — not a git repository)\n", style=C_DIM))

    parts.append(Text(""))

    # ── Messages ──
    meta_count = sum(1 for m in snap.messages if m.get("_is_meta"))
    parts.append(Text(
        f"── Messages ({len(snap.messages)} total, {meta_count} [META]) ",
        style=f"bold {C_LABEL}",
    ))
    parts.append(Text("\n", style=C_DIM))

    for i, msg in enumerate(snap.messages):
        is_meta = msg.get("_is_meta", False)
        display_role = _get_display_role(msg)

        text = _extract_content_text(msg, preview=True)
        fl = _first_line(text, 120)
        meta_tag = " [META]" if is_meta else ""
        kind = f" [{_detect_meta_kind(msg)}]" if is_meta else ""

        if is_meta:
            role_color = C_META
        elif "tool_result" in display_role:
            role_color = "#FFCC00"
        elif "tool_use" in display_role:
            role_color = "#FF8800"
        elif display_role == "user":
            role_color = C_USER
        else:
            role_color = C_ASSISTANT

        parts.append(Text(f"  [{i:3d}] ", style=C_DIM))
        parts.append(Text(f"{display_role}{meta_tag}{kind}", style=role_color))
        if fl:
            parts.append(Text(f"\n        {fl}", style=C_DIM))
        parts.append(Text("\n", style=C_DIM))

    parts.append(Text("  /show messages      — all messages in pager\n", style=C_DIM))
    parts.append(Text("  /show messages <N>  — expand single message\n", style=C_DIM))

    return Panel(
        Text.assemble(*parts),
        title="PROMPT SNAPSHOT",
        border_style=C_TITLE,
        padding=(0, 1),
    )


# ═══════════════════════════════════════════════════════════════
# Detail views (for pager)
# ═══════════════════════════════════════════════════════════════

def prompt_detail(snap: AssemblySnapshot | None, index: int | None) -> str | None:
    """Return full text of a specific section, or the entire system prompt.

    Args:
        snap: The snapshot.
        index: Section index, or None for all sections.

    Returns:
        Formatted text, or None if no data.
    """
    if snap is None or not snap.system_prompt:
        return None

    if index is not None:
        if 0 <= index < len(snap.system_prompt_sections):
            section = snap.system_prompt_sections[index]
            header = f"Section [{index}] ({_line_count(section)} lines, {len(section):,} chars)"
            return f"{'='*60}\n{header}\n{'='*60}\n\n{section}"
        return f"Section {index} not found. Valid range: 0–{len(snap.system_prompt_sections)-1}"

    # All sections
    parts = []
    parts.append(f"{'='*60}")
    parts.append(f"SYSTEM PROMPT — {len(snap.system_prompt_sections)} sections, "
                 f"{len(snap.system_prompt):,} chars")
    parts.append(f"{'='*60}")
    parts.append("")

    for i, section in enumerate(snap.system_prompt_sections):
        parts.append(f"{'─'*40}")
        parts.append(f"[{i}] {_first_line(section, 100)} ({_line_count(section)} lines)")
        parts.append(f"{'─'*40}")
        parts.append(section)
        parts.append("")

    return "\n".join(parts)


def context_detail(snap: AssemblySnapshot | None, key: str | None) -> str | None:
    """Return full text of a specific context key, or all context.

    Args:
        snap: The snapshot.
        key: Context key name (e.g. "claudeMd", "gitStatus"), or None for all.

    Returns:
        Formatted text, or None if no data.
    """
    if snap is None:
        return None

    if key:
        if key in snap.user_context:
            val = snap.user_context[key]
            return f"{'='*60}\nUser Context — {key}  ({_line_count(val)} lines, {len(val):,} chars)\n{'='*60}\n\n{val}"
        if key in snap.system_context:
            val = snap.system_context[key]
            return f"{'='*60}\nSystem Context — {key}  ({_line_count(val)} lines, {len(val):,} chars)\n{'='*60}\n\n{val}"
        all_keys = list(snap.user_context.keys()) + list(snap.system_context.keys())
        return f"Context key '{key}' not found. Available: {all_keys}"

    # All context
    parts = []
    parts.append(f"{'='*60}")
    parts.append("USER CONTEXT (injected as <system-reminder> in message[0])")
    parts.append(f"{'='*60}")
    for k, v in snap.user_context.items():
        parts.append(f"\n── {k} ({_line_count(v)} lines, {len(v):,} chars) ──\n")
        parts.append(v)

    parts.append(f"\n{'='*60}")
    parts.append("SYSTEM CONTEXT (appended to system prompt)")
    parts.append(f"{'='*60}")
    for k, v in snap.system_context.items():
        parts.append(f"\n── {k} ({_line_count(v)} lines, {len(v):,} chars) ──\n")
        parts.append(v)

    return "\n".join(parts)


def messages_detail(snap: AssemblySnapshot | None, index: int | None) -> str | None:
    """Return full text of a specific message, or all messages.

    Args:
        snap: The snapshot.
        index: Message index, or None for all.

    Returns:
        Formatted text, or None if no data.
    """
    if snap is None:
        return None

    if index is not None:
        if 0 <= index < len(snap.messages):
            msg = snap.messages[index]
            return _format_single_message(index, msg)
        return f"Message {index} not found. Valid range: 0–{len(snap.messages)-1}"

    # All messages
    parts = []
    meta_count = sum(1 for m in snap.messages if m.get("_is_meta"))
    parts.append(f"{'='*60}")
    parts.append(f"MESSAGES — {len(snap.messages)} total, {meta_count} _is_meta")
    parts.append(f"{'='*60}\n")

    for i, msg in enumerate(snap.messages):
        parts.append(_format_single_message(i, msg))
        parts.append("")

    return "\n".join(parts)


def _format_single_message(index: int, msg: dict) -> str:
    """Format a single message for display."""
    is_meta = msg.get("_is_meta", False)
    kind = _detect_meta_kind(msg)
    display_role = _get_display_role(msg)

    text = _extract_content_text(msg, preview=False)

    meta_info = f" [META: {kind}]" if is_meta else ""
    return (
        f"{'─'*60}\n"
        f"[{index}] {display_role}{meta_info}  ({len(text):,} chars, {_line_count(text)} lines)\n"
        f"{'─'*60}\n"
        f"{text}"
    )


# ═══════════════════════════════════════════════════════════════
# Pager helpers
# ═══════════════════════════════════════════════════════════════

def pager(text: str) -> None:
    """Display text in Rich pager (like less)."""
    Console().pager(text)


def render_str(renderable) -> str:
    """Convert a Rich renderable to an ANSI string for NDJSON/CLI transport."""
    buf = StringIO()
    console = Console(file=buf, force_terminal=True, color_system="standard", width=120)
    console.print(renderable)
    return buf.getvalue()
