from bglab.tools.base import (
    Tool,
    ToolRegistry,
    build_tool,
    tool_matches_name,
    find_tool_by_name,
    assemble_tool_pool,
    filter_tools_by_mode,
    filter_tools_by_deny_rules,
)
from bglab.tools.read import ReadTool
from bglab.tools.write import WriteTool
from bglab.tools.edit import EditTool
from bglab.tools.bash import BashTool
from bglab.tools.cmd import CmdTool
from bglab.tools.powershell import PowerShellTool
from bglab.tools.grep import GrepTool
from bglab.tools.glob import GlobTool
from bglab.tools.web_search import WebSearchTool
from bglab.tools.web_fetch import WebFetchTool
from bglab.tools.task import TaskCreateTool, TaskUpdateTool, TaskListTool
from bglab.tools.ask_user import AskUserQuestionTool
from bglab.tools.agent import AgentTool
from bglab.tools.tool_search import ToolSearchTool
from bglab.tools.plan_mode import EnterPlanModeTool, ExitPlanModeTool
from bglab.tools.sleep import register as register_sleep
from bglab.tools.cron import register as register_cron
from bglab.tools.bg_play import BG_PLAY_TOOL, build_bg_play_tool
from bglab.tools.player_nickname import SetPlayerNicknameTool
from bglab.tools.team_create import TeamCreateTool
from bglab.tools.team_delete import TeamDeleteTool
from bglab.tools.send_message import SendMessageTool
from bglab.tools.notebook import register as register_notebook
from bglab.tools.task_stop_output import register as register_task_stop_output
from bglab.tools.worktree import register as register_worktree


def build_registry(extra_tools: list[Tool] | None = None) -> ToolRegistry:
    ""
    # Skill loading is an optional Layer 2 capability.  Keep the public
    # registry API intact, but do not import its implementation merely because
    # a Layer 1 caller imports ``bglab.tools.base``.
    from bglab.tools.skill import SkillTool

    registry = ToolRegistry()
    registry.register(ReadTool)
    registry.register(WriteTool)
    registry.register(EditTool)
    registry.register(BashTool)
    registry.register(CmdTool)
    registry.register(PowerShellTool)
    registry.register(GrepTool)
    registry.register(GlobTool)
    registry.register(WebSearchTool)
    registry.register(WebFetchTool)
    registry.register(TaskCreateTool)
    registry.register(TaskUpdateTool)
    registry.register(TaskListTool)
    registry.register(AskUserQuestionTool)
    registry.register(SkillTool)
    registry.register(AgentTool)
    registry.register(EnterPlanModeTool)
    registry.register(ExitPlanModeTool)

    register_sleep(registry)
    register_cron(registry)
    register_notebook(registry)
    register_task_stop_output(registry)
    register_worktree(registry)
    registry.register(build_bg_play_tool())
    registry.register(SetPlayerNicknameTool)
    registry.register(TeamCreateTool)
    registry.register(TeamDeleteTool)
    registry.register(SendMessageTool)

    if extra_tools:
        for t in extra_tools:
            registry.register(t)

    return registry


def __getattr__(name: str):
    """Preserve ``bglab.tools.SkillTool`` without eagerly loading Layer 2."""
    if name == "SkillTool":
        from bglab.tools.skill import SkillTool

        return SkillTool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Tool", "ToolRegistry",
    "build_tool",
    "tool_matches_name", "find_tool_by_name",
    "assemble_tool_pool", "filter_tools_by_mode", "filter_tools_by_deny_rules",
    "ReadTool", "WriteTool", "EditTool",
    "BashTool", "CmdTool", "PowerShellTool",
    "GrepTool", "GlobTool",
    "WebSearchTool", "WebFetchTool",
    "TaskCreateTool", "TaskUpdateTool", "TaskListTool",
    "AskUserQuestionTool",
    "SkillTool", "AgentTool",
    "ToolSearchTool", "EnterPlanModeTool", "ExitPlanModeTool",
    "TeamCreateTool", "TeamDeleteTool", "SendMessageTool",
    "BG_PLAY_TOOL",
    "build_registry",
]
