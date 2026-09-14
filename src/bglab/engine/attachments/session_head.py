""

from __future__ import annotations

import html

from bglab.engine.attachments.types import AttachmentValue, ProviderContext
from bglab.prompt.context import build_user_context_head_values


def build_code_session_head(
    context: ProviderContext,
) -> tuple[AttachmentValue, ...]:
    values = build_user_context_head_values(
        context.local.get("user_context") or {},
        context.local.get("deferred_tool_names") or [],
    )
    return tuple(
        AttachmentValue(kind=kind, text=text)
        for kind, text in values
    )


def build_code_post_compact(
    context: ProviderContext,
) -> tuple[AttachmentValue, ...]:
    """Restore non-head Code lifecycle facts after compaction.

    CLAUDE.md/user context is intentionally *not* copied here; it is supplied
    by the dynamic session head on the next request.  Only compact-safe,
    typed lifecycle facts are transcript-backed.
    """
    relink = context.local.get("compact_relink")
    if not isinstance(relink, dict):
        relink = getattr(context.deps, "compact_relink", None)
    if not isinstance(relink, dict):
        return ()
    compact_token = str(relink.get("_compact_id", "latest"))
    values: list[AttachmentValue] = []
    skills = relink.get("skill_names", [])
    skill_bodies = relink.get("skill_bodies", [])
    restored_skill_names: set[str] = set()
    if isinstance(skill_bodies, list):
        for index, skill in enumerate(skill_bodies[:8]):
            if not isinstance(skill, dict):
                continue
            name = str(skill.get("name", "")).strip()
            body = str(skill.get("body", "")).strip()
            if not name or not body:
                continue
            restored_skill_names.add(name)
            path = str(skill.get("path", "")).strip()
            escaped_name = html.escape(name, quote=True)
            escaped_path = html.escape(path, quote=True)
            values.append(AttachmentValue(
                kind="invoked_skill",
                text=(
                    f"<skill name=\"{escaped_name}\""
                    + (f" path=\"{escaped_path}\"" if path else "")
                    + f">\n{body}\n</skill>"
                ),
                attachment_id=(
                    f"post_compact:code:{compact_token}:invoked_skill:{index}"
                ),
            ))
    missing_skill_bodies = [
        str(name)
        for name in skills
        if str(name).strip() and str(name) not in restored_skill_names
    ] if isinstance(skills, (list, tuple)) else []
    if missing_skill_bodies:
        values.append(
            AttachmentValue(
                kind="invoked_skills",
                text="Invoked skills after compaction:\n" + "\n".join(
                    f"- {name}" for name in missing_skill_bodies
                ),
                attachment_id=f"post_compact:code:{compact_token}:invoked_skills",
            )
        )
    permission_mode = str(relink.get("permission_mode", "")).strip()
    if permission_mode:
        values.append(
            AttachmentValue(
                kind="permission_mode",
                text=f"Permission mode after compaction: {permission_mode}",
                attachment_id=f"post_compact:code:{compact_token}:permission_mode",
            )
        )
    agents = relink.get("agent_calls", [])
    if isinstance(agents, list) and agents:
        values.append(
            AttachmentValue(
                kind="agent_history",
                text="Recent Agent calls before compaction:\n" + "\n".join(
                    "- "
                    + str(agent.get("description", ""))
                    + f" (type={agent.get('type', 'fork')}, "
                    + f"background={bool(agent.get('background', False))})"
                    for agent in agents[:8]
                    if isinstance(agent, dict)
                ),
                attachment_id=f"post_compact:code:{compact_token}:agent_history",
            )
        )
    task_state = str(relink.get("task_state", "")).strip()
    if task_state:
        values.append(AttachmentValue(
            kind="task_state",
            text="Task state after compaction:\n" + task_state,
            attachment_id=f"post_compact:code:{compact_token}:task_state",
        ))
    deferred_tools = relink.get("deferred_tools", [])
    if isinstance(deferred_tools, list) and deferred_tools:
        values.append(AttachmentValue(
            kind="deferred_tools",
            text="Deferred tools still available:\n" + "\n".join(
                f"- {name}" for name in deferred_tools if str(name).strip()
            ),
            attachment_id=f"post_compact:code:{compact_token}:deferred_tools",
        ))
    # Keep a small opaque session marker when no richer lifecycle fact exists;
    # this preserves the typed seam without replaying the entire head.
    if not values and relink:
        values.append(
            AttachmentValue(
                kind="session_relink",
                text="Code session lifecycle restored after compaction.",
                attachment_id=f"post_compact:code:{compact_token}:session_relink",
                metadata={"keys": sorted(str(key) for key in relink)},
            )
        )
    return tuple(values)
