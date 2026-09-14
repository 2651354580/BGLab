"""Plan Mode 工具 — 对齐 EnterPlanModeTool + ExitPlanModeTool。

EnterPlanMode: 进入规划模式，只读探索代码库
ExitPlanMode:  退出规划模式，请求用户批准方案
"""

from __future__ import annotations

from bglab.tools.base import Tool
from bglab.session.state import session_state


def enter_plan_mode(args: dict) -> str:
    """进入 plan mode — writes to shared session state."""
    if session_state.permission_mode == "plan":
        return "Already in plan mode. Use ExitPlanMode when ready to leave."

    session_state.pre_plan_mode = session_state.permission_mode
    session_state.permission_mode = "plan"
    return (
        "Entered plan mode. Write operations (Write/Edit/Bash/Cmd/PowerShell) "
        "are now DISABLED. Read-only tools only.\n"
        "Explore the codebase, design an approach, then use ExitPlanMode "
        "to present your plan for approval."
    )


def exit_plan_mode(args: dict) -> str:
    """退出 plan mode — restores previous mode."""
    if session_state.permission_mode != "plan":
        return "Not in plan mode."

    restored = session_state.pre_plan_mode
    session_state.permission_mode = restored
    session_state.pre_plan_mode = "default"

    allowed_prompts = args.get("allowedPrompts", [])
    if allowed_prompts:
        lines = ["Exiting plan mode. Required permissions for implementation:"]
        for p in allowed_prompts:
            tool = p.get("tool", "")
            prompt_desc = p.get("prompt", "")
            lines.append(f"  - {tool}: {prompt_desc}")
        lines.append("\nPlan is ready for user review and approval.")
        return "\n".join(lines)
    return f"Exited plan mode. Mode restored to: {restored}"


EnterPlanModeTool = Tool(
    name="EnterPlanMode",
    searchHint="enter plan-only read-only mode",
    description="Enter plan mode — explore the codebase and design an approach before writing code.",
    prompt="""Use this tool proactively when you're about to start a non-trivial implementation task. Getting user sign-off on your approach before writing code prevents wasted effort and ensures alignment. This tool transitions you into plan mode where you can explore the codebase and design an implementation approach for user approval.

## When to Use This Tool

Use EnterPlanMode for implementation tasks unless they're simple. Use it when ANY of these conditions apply:

1. **New Feature Implementation**: Adding meaningful new functionality
2. **Multiple Valid Approaches**: The task can be solved in several different ways
3. **Code Modifications**: Changes that affect existing behavior or structure
4. **Architectural Decisions**: The task requires choosing between patterns or technologies
5. **Multi-File Changes**: The task will likely touch more than 2-3 files
6. **Unclear Requirements**: You need to explore before understanding the full scope
7. **User Preferences Matter**: The implementation could reasonably go multiple ways

## When NOT to Use This Tool

Skip for simple tasks: single-line fixes, adding obvious functions, tasks with very specific instructions, or pure research.

## Important Notes

- This tool REQUIRES user approval - they must consent to entering plan mode
- If unsure whether to use it, err on the side of planning""",
    parameters={"type": "object", "properties": {}},
    call=enter_plan_mode,
    is_read_only=True,
    auto_allow=True,
    plan_allowed=True,
)

ExitPlanModeTool = Tool(
    name="ExitPlanMode",
    searchHint="exit plan mode resume coding",
    description="Exit plan mode when your implementation plan is ready for user approval.",
    prompt="""Use this tool when you are in plan mode and have finished writing your plan and are ready for user approval.

## How This Tool Works
- You should have already written your plan to the plan file
- This tool does NOT take the plan content as a parameter
- This tool simply signals that you're done planning and ready for the user to review
- The user will see your plan when they review it

## Before Using This Tool
Ensure your plan is complete and unambiguous:
- If you have unresolved questions about requirements or approach, use AskUserQuestion first
- Once your plan is finalized, use THIS tool to request approval

**Important:** Do NOT use AskUserQuestion to ask "Is my plan okay?" — that's exactly what THIS tool does.

## Examples

1. "Search for and understand vim mode in the codebase" — Do NOT use ExitPlanMode (research task, not planning implementation)
2. "Help me implement yank mode for vim" — Use ExitPlanMode after finishing the implementation plan
3. "Add auth to the app" — If unsure about method (OAuth/JWT), use AskUserQuestion first, then ExitPlanMode""",
    parameters={
        "type": "object",
        "properties": {
            "allowedPrompts": {
                "type": "array",
                "description": "Prompt-based permissions needed to implement the plan",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "enum": ["Bash"]},
                        "prompt": {"type": "string", "description": "Semantic description of the action"},
                    },
                },
            },
        },
    },
    call=exit_plan_mode,
    is_read_only=True,
    auto_allow=True,
    plan_allowed=True,
)
