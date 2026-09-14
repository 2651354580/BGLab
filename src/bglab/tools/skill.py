"""Skill 工具 — 对齐 SkillTool/prompt.ts。

模型调用 skill 时触发，查找并加载 skill 指令。
查找顺序: bundled registry → file/game skill cache → filesystem scan。
"""

from __future__ import annotations

from bglab.tools.base import Tool


def invoke_skill(args: dict) -> str:
    """调用 skill。Bundled first, then runtime cache, fall back to filesystem scan."""
    skill_name = args.get("skill", "")
    skill_args = args.get("args", "")

    if not skill_name:
        return "Error: skill name is required"

    from bglab.skills.loader import substitute_skill_vars

    # 1. Bundled skills (registered at import time).
    try:
        from bglab.skills.base import get_skill
        bundled = get_skill(skill_name)
        if bundled is not None:
            raw_prompt = bundled.get_prompt(skill_args or "")
            prompt = substitute_skill_vars(raw_prompt, skill_args or "")
            if skill_args:
                return f"Skill '{skill_name}' loaded with args: {skill_args}\n\n{prompt}"
            return f"Skill '{skill_name}' loaded.\n\n{prompt}"
    except Exception:
        pass

    # 2. File/game skill cache (populated by _collect_skill_prefetch or game registration).
    try:
        from bglab.skills.base import get_file_skill
        cached = get_file_skill(skill_name)
        if cached is not None:
            if cached.get("disable_model_invocation") is True:
                return f"Error: Skill '{skill_name}' does not allow model invocation."
            body = cached.get("body", "")
            skill_dir = cached.get("_source_dir", "")
            prompt = substitute_skill_vars(body, skill_args or "", skill_dir)
            if skill_args:
                return f"Skill '{skill_name}' loaded with args: {skill_args}\n\n{prompt}"
            return f"Skill '{skill_name}' loaded.\n\n{prompt}"
    except Exception:
        pass

    # 3. Filesystem scan fallback.
    from bglab.skills.loader import load_skills
    import asyncio

    cwd = args.get("_cwd", "")

    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            from bglab.skills.base import get_all_registered_skills
            available = [
                item.get("name")
                for item in get_all_registered_skills()
                if item.get("name")
            ]
            return (
                f"Skill '{skill_name}' not found in the registered skill set. "
                f"Use one exact available name: {available}"
            )
        skills = asyncio.run(load_skills(cwd))
        for s in skills:
            if s.get("name") == skill_name:
                if s.get("disable_model_invocation") is True:
                    return f"Error: Skill '{skill_name}' does not allow model invocation."
                raw_body = s.get("body", "")
                skill_dir = s.get("_source_dir")
                prompt = substitute_skill_vars(raw_body, skill_args or "", skill_dir)
                if skill_args:
                    return f"Skill '{skill_name}' loaded with args: {skill_args}\n\n{prompt}"
                return f"Skill '{skill_name}' loaded.\n\n{prompt}"
        available = [s.get("name") for s in skills]
        return f"Skill '{skill_name}' not found. Available skills: {available}"
    except Exception as e:
        return f"Error loading skill '{skill_name}': {e}"


SkillTool = Tool(
    name="Skill",
    searchHint="invoke a skill by name",
    description="Load a skill's instructions on demand",
    prompt="""Read a skill's instructions into this conversation using its exact available name.
The catalog describes when each skill helps; it does not contain the full instructions.
Load a relevant skill when its method is needed for the current task, then apply the
instructions within the user's scope and existing tool permissions. Loading does not
execute the task or start another agent. Reuse instructions already loaded and still
applicable. Do not guess names or treat built-in CLI commands as skills.
The optional args field supplies task details to the loaded instructions.""",
    parameters={
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "The name of a skill from the available-skills list. Do not guess names.",
            },
            "args": {
                "type": "string",
                "description": "Optional arguments for the skill",
            },
        },
        "required": ["skill"],
    },
    call=invoke_skill,
    is_read_only=True,
    auto_allow=True,
    plan_allowed=True,
)
