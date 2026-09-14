""

from __future__ import annotations
from bglab.tools.base import Tool, ToolRegistry


# ── TaskStop ──

TASK_STOP_PROMPT = """Stop a running background task by ID.

- Takes a task_id parameter identifying the task to stop
- Returns a success or failure status
- Use this tool when you need to terminate a long-running task"""


def _task_stop_call(args: dict) -> str:
    task_id = args.get("task_id") or args.get("shell_id", "")
    if not task_id:
        return "Error: Missing required parameter: task_id"

    
    # This stub stops a task by ID from the global task store
    from bglab.tools.task import _get_store
    store = _get_store()
    task = None
    for t in store.tasks:
        if t["id"] == task_id:
            task = t
            break

    if task is None:
        return f"Error: No task found with ID: {task_id}"

    if task.get("status") not in ("running", "in_progress"):
        return f"Error: Task {task_id} is not running (status: {task.get('status')})"

    task["status"] = "stopped"
    desc = task.get("description", "") or task.get("subject", "")
    return f"Successfully stopped task: {task_id} ({desc})"


# ── TaskOutput ──

TASK_OUTPUT_PROMPT = """Retrieves output from a running or completed task (background shell, agent, or remote session).

- Takes a task_id parameter identifying the task
- Returns the task output along with status information
- Use block=true (default) to wait for task completion
- Use block=false for non-blocking check of current status
- Task IDs can be found using the /tasks command
- Works with all task types: background shells, async agents, and remote sessions"""


def _task_output_call(args: dict) -> str:
    task_id = args.get("task_id", "")
    if not task_id:
        return "Error: Task ID is required"

    from bglab.tools.task import _get_store
    store = _get_store()
    task = None
    for t in store.tasks:
        if t["id"] == task_id:
            task = t
            break

    if task is None:
        return f"Error: No task found with ID: {task_id}"

    status = task.get("status", "unknown")
    output = task.get("output", task.get("result", ""))
    subject = task.get("subject", "")

    parts = [
        f"<retrieval_status>{'success' if status in ('completed', 'stopped', 'error') else 'not_ready'}</retrieval_status>",
        f"<task_id>{task_id}</task_id>",
        "<task_type>background</task_type>",
        f"<status>{status}</status>",
    ]
    if subject:
        parts.append(f"<description>{subject}</description>")
    if output:
        parts.append(f"<output>\n{output}\n</output>")

    return "\n".join(parts)


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="TaskStop",
        searchHint="stop running background task",
        description="Stop a running background task by ID",
        prompt=TASK_STOP_PROMPT,
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The ID of the background task to stop",
                },
                "shell_id": {
                    "type": "string",
                    "description": "Deprecated: use task_id instead",
                },
            },
            "required": [],
        },
        call=_task_stop_call,
        is_read_only=False,
        auto_allow=True,
        plan_allowed=False,
    ))

    registry.register(Tool(
        name="TaskOutput",
        searchHint="get background task output result",
        description="Retrieve output from a background task",
        prompt=TASK_OUTPUT_PROMPT,
        parameters={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "The task ID to get output from",
                },
                "block": {
                    "type": "boolean",
                    "description": "Whether to wait for completion (default true)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Max wait time in ms (30000 default, 600000 max)",
                },
            },
            "required": ["task_id"],
        },
        call=_task_output_call,
        is_read_only=True,
        auto_allow=True,
        plan_allowed=True,
    ))
