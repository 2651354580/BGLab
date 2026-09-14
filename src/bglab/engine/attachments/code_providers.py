"""Code Agent attachment providers behind the fixed query-loop seam."""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from bglab.engine.attachments.types import AttachmentValue, ProviderContext


def _reminder(kind: str, text: str, *, attachment_id: str | None = None,
              metadata: dict[str, Any] | None = None) -> AttachmentValue:
    return AttachmentValue(
        kind=kind,
        text="<system-reminder>\n" + text + "\n</system-reminder>",
        attachment_id=attachment_id,
        metadata=metadata or {},
    )


@dataclass
class CodeAttachmentState:
    """Query-lifetime state used by Code providers, not provider wire data."""

    sent_skill_names: set[str] = field(default_factory=set)


def _state(context: ProviderContext) -> CodeAttachmentState:
    state = getattr(context.deps, "_code_attachment_state", None)
    if not isinstance(state, CodeAttachmentState):
        state = CodeAttachmentState()
        context.deps._code_attachment_state = state
    return state


def _pending_todos(query_state: Any) -> list[dict[str, str]]:
    pending: list[dict[str, str]] = []
    seen: set[str] = set()
    for message in reversed(getattr(query_state, "messages", []) or []):
        content = message.get("content", []) if isinstance(message, dict) else []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            text = str(block.get("text", ""))
            name = block.get("name", "")
            if not (
                name in {"TaskCreate", "TaskUpdate"}
                or "<todo>" in text.lower()
                or "- [pending]" in text.lower()
                or "- [in_progress]" in text.lower()
            ):
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line.startswith("- [") or "] " not in line:
                    continue
                bracket_end = line.index("] ")
                status = line[3:bracket_end]
                if status in {"completed", "done", "deleted"}:
                    continue
                subject = line[bracket_end + 2:][:80].strip()
                if subject and subject not in seen:
                    seen.add(subject)
                    pending.append({"status": status, "subject": subject})
    return pending[:5]


def build_changed_files(context: ProviderContext) -> tuple[AttachmentValue, ...]:
    write_tools = {"Write", "Edit", "Bash", "Cmd", "PowerShell"}
    modified = sorted({
        str(args.get("file_path", ""))
        for _, name, args in context.tool_uses
        if name in write_tools and args.get("file_path")
    })
    if not modified:
        return ()
    return (_reminder(
        "changed_files",
        "Files modified this turn:\n" + "\n".join(f"  - {path}" for path in modified),
    ),)


def build_date_change(context: ProviderContext) -> tuple[AttachmentValue, ...]:
    today = datetime.now().strftime("%Y-%m-%d")
    previous = getattr(context.deps, "_last_date", "")
    changed = bool(previous and previous != today)
    context.deps._last_date = today
    if not changed:
        return ()
    return (_reminder(
        "date_change",
        f"The date has changed. Today's date is now {today}. "
        "Do not mention this to the user explicitly because they are already aware.",
    ),)


def build_plan_mode(context: ProviderContext) -> tuple[AttachmentValue, ...]:
    if context.permission_mode == "plan":
        try:
            from bglab.prompt.plan_mode_full import get_plan_mode_attachment
            last = getattr(context.deps, "_plan_mode_last_attachment_turn", None)
            text = get_plan_mode_attachment(getattr(context.state, "turn_count", 1), last)
            if text is not None:
                context.deps._plan_mode_last_attachment_turn = getattr(
                    context.state, "turn_count", 1,
                )
                return (_reminder("plan_mode", text),)
        except (ImportError, AttributeError):
            pass
    return ()


def build_plan_mode_exit(context: ProviderContext) -> tuple[AttachmentValue, ...]:
    previous = getattr(context.deps, "_prev_permission_mode", None)
    context.deps._prev_permission_mode = context.permission_mode
    if previous == "plan" and context.permission_mode != "plan":
        return (_reminder(
            "plan_mode_exit",
            "You have exited plan mode. Write operations are now allowed. "
            "Resume normal coding workflow.",
        ),)
    return ()


def build_todo_reminders(context: ProviderContext) -> tuple[AttachmentValue, ...]:
    pending = _pending_todos(context.state)
    if not pending:
        return ()
    lines = "\n".join(f"  - [{row['status']}] {row['subject']}" for row in pending)
    return (_reminder(
        "todo_reminder",
        "You have pending tasks:\n"
        + lines
        + "\nContinue working on these tasks unless the user says otherwise.",
    ),)


def build_compaction_reminder(context: ProviderContext) -> tuple[AttachmentValue, ...]:
    if not getattr(context.deps, "compact_relink", None):
        return ()
    return (_reminder(
        "compaction_reminder",
        "Conversation was just compacted. The summary above captures what happened "
        "before compaction. Pick up where you left off.",
    ),)


def build_deferred_tool_listing(context: ProviderContext) -> tuple[AttachmentValue, ...]:
    deps = context.deps
    assembly = getattr(deps, "_last_assembly", None)
    if assembly is None or not assembly.activated or assembly.deferred_count <= 0:
        return ()
    current = getattr(context.state, "turn_count", 1)
    if getattr(deps, "_deferred_list_injected", False) and (
        current - getattr(deps, "_deferred_list_last_turn", 0)
    ) < 5:
        return ()
    catalog = getattr(getattr(deps, "deferred_registry", None), "_catalog", [])
    if not catalog:
        return ()
    deps._deferred_list_injected = True
    deps._deferred_list_last_turn = current
    lines = [
        f"{index + 1}. `{entry['name']}` — {entry['description'][:120]}"
        for index, entry in enumerate(catalog)
    ]
    return (_reminder(
        "deferred_tool_listing",
        f"{len(catalog)} additional tools are available on demand "
        "(via ToolSearch → ToolDescribe → ToolCall):\n\n"
        + "\n".join(lines)
        + "\n\nUse ToolSearch for full-text search across these tools, "
        "ToolDescribe to get a tool's parameter schema, and ToolCall to invoke one.",
    ),)


def build_background_notifications(context: ProviderContext) -> tuple[AttachmentValue, ...]:
    if context.phase != "post_tool":
        return ()
    try:
        from bglab.tools.bg_agent import drain_notifications
        notifications = drain_notifications()
    except Exception:
        return ()
    values: list[AttachmentValue] = []
    for notification in notifications:
        lines = [
            "<task_notification>",
            f"  <task_name>{html.escape(str(notification.task_name))}</task_name>",
            f"  <status>{html.escape(str(notification.status))}</status>",
            f"  <summary>{html.escape(str(notification.summary))}</summary>",
        ]
        if notification.output_file:
            path = html.escape(str(notification.output_file))
            lines.extend((
                f"  <output_file>{path}</output_file>",
                f"  <hint>Use the Read tool to view the full agent output at: {path}</hint>",
            ))
        if notification.error:
            lines.append(f"  <error>{html.escape(str(notification.error))}</error>")
        lines.append("</task_notification>")
        values.append(AttachmentValue(
            kind="queued_command",
            text="\n".join(lines),
        ))
    return tuple(values)


async def build_skill_listing(context: ProviderContext) -> tuple[AttachmentValue, ...]:
    try:
        from bglab.engine.stubs import load_skills
        skills = await load_skills(context.cwd)
    except Exception:
        return ()
    state = _state(context)
    from bglab.skills.base import _FILE_SKILLS_CACHE

    discovered: dict[str, Any] = {}
    for skill in skills or []:
        name = skill.get("name", "") if isinstance(skill, dict) else getattr(skill, "name", "")
        if name:
            discovered[name] = skill
    _FILE_SKILLS_CACHE.clear()
    _FILE_SKILLS_CACHE.update(discovered)
    pending_skills = []
    for skill in sorted(
        skills or [],
        key=lambda item: (
            item.get("name", "")
            if isinstance(item, dict)
            else getattr(item, "name", "")
        ),
    ):
        if isinstance(skill, dict) and skill.get("disable_model_invocation") is True:
            continue
        name = skill.get("name", "") if isinstance(skill, dict) else getattr(skill, "name", "")
        if not name or name in state.sent_skill_names:
            continue
        pending_skills.append(skill)
    if not pending_skills:
        return ()

    lines: list[str] = []
    emitted_names: list[str] = []
    byte_budget = 12_000
    for skill in pending_skills:
        name = skill.get("name", "") if isinstance(skill, dict) else getattr(skill, "name", "")
        description = skill.get("description", "") if isinstance(skill, dict) else getattr(skill, "description", "")
        normalized_description = " ".join(str(description).split())[:240]
        loaded = (
            skill.get("loaded") is True
            if isinstance(skill, dict)
            else getattr(skill, "loaded", False) is True
        )
        marker = " [loaded]" if loaded else ""
        line = f"- {name}{marker}: {normalized_description}"
        projected = len(("\n".join([*lines, line])).encode("utf-8"))
        if len(lines) >= 40 or projected > byte_budget:
            break
        lines.append(line)
        emitted_names.append(name)
    if not emitted_names:
        return ()
    state.sent_skill_names.update(emitted_names)
    remaining = len(pending_skills) - len(emitted_names)
    suffix = (
        f"\n\n{remaining} additional skills remain and will be listed in a later update."
        if remaining
        else ""
    )
    return (_reminder(
        "skill_listing",
        "The following skills are available for use with the Skill tool:\n\n"
        + "\n".join(lines)
        + suffix,
        metadata={
            "skillCount": len(emitted_names),
            "remainingCount": remaining,
            "isInitial": len(state.sent_skill_names) == len(emitted_names),
        },
    ),)


async def prefetch_relevant_memory(context: ProviderContext) -> tuple[AttachmentValue, ...]:
    flags = getattr(context.deps, "feature_flags", None)
    if flags is not None and not bool(
        getattr(flags, "memory_section_enabled", True),
    ):
        return ()
    user_text = str(context.local.get("turn_input") or "").strip()
    if not user_text:
        user_text = _last_real_user_text(context.messages)
    if not user_text or " " not in user_text.strip():
        return ()
    surfaced, surfaced_bytes = _surfaced_memories(context.messages)
    if surfaced_bytes >= 60 * 1024:
        return ()
    try:
        from bglab.memory import get_relevant_memory_attachments
        results = await get_relevant_memory_attachments(
            user_text, context.cwd, surfaced, model=context.model,
        )
    except Exception:
        return ()
    values: list[AttachmentValue] = []
    for result in results or []:
        for memory in result.get("memories", []):
            if not isinstance(memory, dict):
                continue
            text = "\n".join((
                memory.get("header", f"Memory: {memory.get('path', '')}:"),
                memory.get("content", ""),
            ))
            values.append(_reminder(
                "relevant_memories",
                "The following memory file was found to be relevant to your current task. "
                "Use it to inform your work.\n\n" + text,
                metadata={"memories": [memory]},
            ))
    return tuple(values)


async def prefetch_skill_discovery(
    _context: ProviderContext,
) -> tuple[AttachmentValue, ...]:
    ""
    return ()


def _last_real_user_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "user" or message.get("_is_meta"):
            continue
        if message.get("type") == "tool_result":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return str(block.get("text", ""))
    return ""


def _surfaced_memories(messages: list[dict[str, Any]]) -> tuple[set[str], int]:
    paths: set[str] = set()
    total = 0
    for message in messages:
        if message.get("attachment_type") != "relevant_memories":
            continue
        for memory in message.get("_memories", []):
            path = memory.get("path", "")
            if path:
                paths.add(path)
            total += len(memory.get("content", "").encode("utf-8"))
    return paths, total
