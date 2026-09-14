"""TaskCreate / TaskUpdate 工具 — 对齐 TaskCreateTool.ts + TaskUpdateTool.ts。

模型可创建和更新任务列表，在 /stats 中可查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from bglab.tools.base import Tool


@dataclass
class TaskStore:
    """内存中的任务列表 — 跨 turn 保持。"""
    tasks: list[dict] = field(default_factory=list)
    _next_id: int = 1

    def create(self, subject: str, description: str) -> dict:
        task = {
            "id": str(self._next_id),
            "subject": subject,
            "description": description,
            "status": "pending",
        }
        self._next_id += 1
        self.tasks.append(task)
        return task

    def update(self, task_id: str, status: str | None = None) -> dict | None:
        for t in self.tasks:
            if t["id"] == task_id:
                if status:
                    t["status"] = status
                return t
        return None

    def list(self) -> list[dict]:
        return self.tasks


# 模块级共享 store（单 session 内）
_store = TaskStore()


def _get_store() -> TaskStore:
    return _store


def task_create(args: dict) -> str:
    """创建任务。"""
    subject = args.get("subject", "")
    description = args.get("description", "")
    if not subject:
        return "Error: subject is required"

    task = _get_store().create(subject, description)
    return f"Task #{task['id']} created: {task['subject']} (status: {task['status']})"


def task_update(args: dict) -> str:
    """更新任务状态。"""
    task_id = args.get("taskId", "")
    status = args.get("status", "")
    if not task_id:
        return "Error: taskId is required"

    task = _get_store().update(task_id, status or None)
    if task is None:
        return f"Task #{task_id} not found. Use TaskList to see all tasks."

    msg = f"Task #{task_id} updated"
    if status:
        msg += f": status={status}"
    return msg


def task_list(args: dict) -> str:
    """列出所有任务。"""
    tasks = _get_store().list()
    if not tasks:
        return "No tasks. Use TaskCreate to create one."
    lines = []
    for t in tasks:
        status_icon = {"pending": "○", "in_progress": "◔", "completed": "●", "deleted": "✕"}.get(t["status"], "?")
        lines.append(f"{status_icon} [{t['id']}] {t['subject']} — {t['status']}")
    return "\n".join(lines)


def _reset_store():
    """测试用：重置 task store。"""
    global _store
    _store = TaskStore()


TaskCreateTool = Tool(
    name="TaskCreate",
    searchHint="create task todo list item",
    description="Create a structured task list for tracking work progress across the session.",
    prompt="""Use this tool to create a structured task list for your current coding session. This helps track progress, organize complex tasks, and demonstrate thoroughness to the user.
It also helps the user understand the progress of the task and overall progress of their requests.

## When to Use This Tool
Use this tool proactively in these scenarios:

1. Complex multi-step tasks - When a task requires 3 or more distinct steps or actions
2. Non-trivial and complex tasks - Tasks that require careful planning or multiple operations
3. User explicitly requests todo list - When the user directly asks you to use the todo list
4. User provides multiple tasks - When users provide a list of things to be done (numbered or comma-separated)
5. After receiving new instructions - Immediately capture user requirements as tasks
6. When you start working on a task - Mark it as in_progress BEFORE beginning work
7. After completing a task - Mark it as completed and add any new follow-up tasks discovered during implementation

## When NOT to Use This Tool
Skip using this tool when:
1. There is only a single, straightforward task
2. The task is trivial and tracking it provides no organizational benefit
3. The task can be completed in less than 3 trivial steps
4. The task is purely conversational or informational

NOTE that you should not use this tool if there is only one trivial task to do. In this case you are better off just doing the task directly.

## Task Fields
- subject: A brief, actionable title in imperative form (e.g., "Fix authentication bug in login flow")
- description: What needs to be done
- activeForm (optional): Present continuous form shown in the spinner when the task is in_progress

## Tips
- Create tasks with clear, specific subjects that describe the outcome
- After creating tasks, use TaskUpdate to set up dependencies (blocks/blockedBy) if needed
- Check TaskList first to avoid creating duplicate tasks""",
    parameters={
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "A brief title for the task"},
            "description": {"type": "string", "description": "What needs to be done"},
            "activeForm": {"type": "string", "description": "Present continuous form shown in spinner when in_progress (e.g., 'Running tests')"},
        },
        "required": ["subject", "description"],
    },
    call=task_create,
    is_read_only=False,
    auto_allow=True,
    plan_allowed=False,
    always_load=True,
)

TaskUpdateTool = Tool(
    name="TaskUpdate",
    searchHint="update task status progress",
    description="Update a task's status.",
    prompt="""Use this tool to update a task in the task list.

Available statuses: pending → in_progress → completed (or deleted)""",
    parameters={
        "type": "object",
        "properties": {
            "taskId": {"type": "string", "description": "The ID of the task to update"},
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "completed", "deleted"],
                "description": "New status for the task",
            },
        },
        "required": ["taskId"],
    },
    call=task_update,
    is_read_only=False,
    auto_allow=True,
    plan_allowed=False,
)

TaskListTool = Tool(
    name="TaskList",
    searchHint="list all pending tasks",
    description="List all tasks.",
    prompt="""Use this tool to list all tasks in the task list.

Use when:
- You need to see what tasks are available to work on
- After completing a task, to find the next one""",
    parameters={
        "type": "object",
        "properties": {},
    },
    call=task_list,
    is_read_only=True,
    auto_allow=True,
    plan_allowed=True,
)
