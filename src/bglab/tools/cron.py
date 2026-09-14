""

from __future__ import annotations
import json
import os
import time
import uuid
from bglab.tools.base import Tool, ToolRegistry


# ── Shared storage ──

_SESSION_JOBS: dict[str, dict] = {}
_CRON_FILE = ".claude/scheduled_tasks.json"


def _load_durable_jobs(cwd: str) -> dict[str, dict]:
    path = os.path.join(cwd, _CRON_FILE)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_durable_jobs(cwd: str, jobs: dict[str, dict]) -> None:
    path = os.path.join(cwd, _CRON_FILE)
    os.makedirs(os.path.dirname(path) or os.getcwd(cwd), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)


# ── CronCreate ──

CRON_CREATE_PROMPT = """Schedule a prompt to be enqueued at a future time. Use for both recurring schedules and one-shot reminders.

Uses standard 5-field cron in the user's local timezone: minute hour day-of-month month day-of-week. "0 9 * * *" means 9am local — no timezone conversion needed.

## One-shot tasks (recurring: false)

For "remind me at X" or "at <time>, do Y" requests — fire once then auto-delete.
Pin minute/hour/day-of-month/month to specific values:
  "remind me at 2:30pm today to check the deploy" → cron: "30 14 <today_dom> <today_month> *", recurring: false
  "tomorrow morning, run the smoke test" → cron: "57 8 <tomorrow_dom> <tomorrow_month> *", recurring: false

## Recurring jobs (recurring: true, the default)

For "every N minutes" / "every hour" / "weekdays at 9am" requests:
  "*/5 * * * *" (every 5 min), "0 * * * *" (hourly), "0 9 * * 1-5" (weekdays at 9am local)

## Avoid the :00 and :30 minute marks when the task allows it

Every user who asks for "9am" gets `0 9`, and every user who asks for "hourly" gets `0 *` — which means requests from across the planet land on the API at the same instant. When the user's request is approximate, pick a minute that is NOT 0 or 30:
  "every morning around 9" → "57 8 * * *" or "3 9 * * *" (not "0 9 * * *")
  "hourly" → "7 * * * *" (not "0 * * * *")

Only use minute 0 or 30 when the user names that exact time and clearly means it ("at 9:00 sharp", "at half past", coordinating with a meeting).

## Durability

By default (durable: false) the job lives only in this Claude session — nothing is written to disk, and the job is gone when Claude exits. Pass durable: true to write to .claude/scheduled_tasks.json so the job survives restarts. Only use durable: true when the user explicitly asks for the task to persist.

## Runtime behavior

Jobs only fire while the REPL is idle (not mid-query). Durable jobs persist to .claude/scheduled_tasks.json and survive session restarts.

Recurring tasks auto-expire after 7 days — they fire one final time, then are deleted. This bounds session lifetime. Tell the user about the 7-day limit when scheduling recurring jobs.

Returns a job ID you can pass to CronDelete."""


ALIASES = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


def _validate_cron(expr: str) -> str | None:
    """Validate a 5-field cron expression. Returns error or None."""
    stripped = expr.strip()

    # Aliases first — they aren't 5-field
    if stripped in ALIASES:
        return None

    parts = stripped.split()
    if len(parts) != 5:
        return f"Invalid cron expression '{expr}'. Expected 5 fields: M H DoM Mon DoW."

    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]

    for i, (part, (lo, hi)) in enumerate(zip(parts, ranges)):
        if part == "*":
            continue
        for chunk in part.split(","):
            step = None
            if "/" in chunk:
                chunk, step = chunk.split("/", 1)
            if "-" in chunk:
                a, b = chunk.split("-", 1)
                if not a.isdigit() or not b.isdigit():
                    return f"Invalid range '{chunk}' in {names[i]} field"
                if int(a) < lo or int(b) > hi:
                    return f"Value out of range [{lo},{hi}] in {names[i]} field: {chunk}"
            elif chunk.isdigit():
                if int(chunk) < lo or int(chunk) > hi:
                    return f"Value out of range [{lo},{hi}] in {names[i]} field: {chunk}"
            elif chunk == "*":
                pass
            else:
                return f"Invalid {names[i]} field: '{part}'"
    return None


def _human_schedule(cron: str) -> str:
    """Convert cron expression to human-readable."""
    aliases = {
        "@yearly": "yearly", "@annually": "yearly", "@monthly": "monthly",
        "@weekly": "weekly", "@daily": "daily", "@midnight": "daily at midnight",
        "@hourly": "hourly",
    }
    if cron.strip() in aliases:
        return aliases[cron.strip()]
    parts = cron.strip().split()
    m, h, dom, mon, dow = parts
    if dow != "*":
        days = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
        dows = [days[int(d)] if d.isdigit() else d for d in dow.split(",")]
        day_str = ",".join(dows)
    else:
        day_str = ""

    if m.startswith("*/"):
        every = m[2:]
        return f"every {every} min" + (f" on {day_str}" if day_str else "")
    if h.startswith("*/"):
        every = h[2:]
        return f"every {every}h" + (f" on {day_str}" if day_str else "")
    if m == "0" and h == "0":
        return "midnight" + (f" on {day_str}" if day_str else "")

    return f"{cron}" + (f" ({day_str})" if day_str else "")


def _cron_create_call(args: dict) -> str:
    cron = str(args.get("cron", "")).strip()
    prompt = str(args.get("prompt", ""))
    recurring = args.get("recurring", True)
    if isinstance(recurring, str):
        recurring = recurring.lower() in ("true", "1", "yes")
    durable = args.get("durable", False)
    if isinstance(durable, str):
        durable = durable.lower() in ("true", "1", "yes")

    if not cron:
        return "Error: cron expression is required"
    err = _validate_cron(cron)
    if err:
        return f"Error: {err}"
    if not prompt:
        return "Error: prompt is required"

    job_id = str(uuid.uuid4())[:8]
    job = {
        "id": job_id,
        "cron": cron,
        "prompt": prompt,
        "recurring": recurring,
        "durable": durable,
        "created_at": time.time(),
    }
    _SESSION_JOBS[job_id] = job

    cwd = os.getcwd()
    if durable:
        jobs = _load_durable_jobs(cwd)
        jobs[job_id] = job
        _save_durable_jobs(cwd, jobs)

    human = _human_schedule(cron)
    where = "Persisted to .claude/scheduled_tasks.json" if durable else "Session-only (not written to disk, dies when Claude exits)"

    if recurring:
        return f"Scheduled recurring job {job_id} ({human}). {where}. Auto-expires after 7 days. Use CronDelete to cancel sooner."
    else:
        return f"Scheduled one-shot task {job_id} ({human}). {where}. It will fire once then auto-delete."


# ── CronDelete ──

CRON_DELETE_PROMPT = """Cancel a cron job previously scheduled with CronCreate. Removes it from .claude/scheduled_tasks.json (durable jobs) or the in-memory session store (session-only jobs)."""


def _cron_delete_call(args: dict) -> str:
    job_id = str(args.get("id", "")).strip()
    if not job_id:
        return "Error: id is required"

    found = None
    if job_id in _SESSION_JOBS:
        found = _SESSION_JOBS.pop(job_id)

    cwd = os.getcwd()
    durable_jobs = _load_durable_jobs(cwd)
    if job_id in durable_jobs:
        found = durable_jobs.pop(job_id, None)
        _save_durable_jobs(cwd, durable_jobs)

    if found is None:
        return f"Error: No scheduled job with id '{job_id}'"

    return f"Cancelled job {job_id}."


# ── CronList ──

CRON_LIST_PROMPT = "List all cron jobs scheduled via CronCreate, both durable (.claude/scheduled_tasks.json) and session-only."


def _cron_list_call(args: dict) -> str:
    cwd = os.getcwd()
    durable_jobs = _load_durable_jobs(cwd)
    all_jobs = {**_SESSION_JOBS, **durable_jobs}

    if not all_jobs:
        return "No scheduled jobs."

    lines = []
    for job_id, j in sorted(all_jobs.items(), key=lambda x: x[1].get("created_at", 0)):
        human = _human_schedule(j["cron"])
        rtype = "recurring" if j.get("recurring", True) else "one-shot"
        dur = " [session-only]" if not j.get("durable") else ""
        prompt_preview = j["prompt"][:80] + ("..." if len(j["prompt"]) > 80 else "")
        lines.append(f"{job_id} — {human} ({rtype}){dur}: {prompt_preview}")

    return "\n".join(lines)


# ── Registration ──

def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="CronCreate",
        searchHint="schedule recurring task at future time",
        description="Schedule a prompt to run at a future time",
        prompt=CRON_CREATE_PROMPT,
        parameters={
            "type": "object",
            "properties": {
                "cron": {
                    "type": "string",
                    "description": 'Standard 5-field cron expression in local time: "M H DoM Mon DoW"',
                },
                "prompt": {
                    "type": "string",
                    "description": "The prompt to enqueue at each fire time.",
                },
                "recurring": {
                    "type": "boolean",
                    "description": "true = fire on every cron match until deleted. false = fire once then auto-delete.",
                },
                "durable": {
                    "type": "boolean",
                    "description": "true = persist to .claude/scheduled_tasks.json. false (default) = in-memory only.",
                },
            },
            "required": ["cron", "prompt"],
        },
        call=_cron_create_call,
        is_read_only=False,
    ))

    registry.register(Tool(
        name="CronDelete",
        searchHint="cancel remove scheduled cron job",
        description="Cancel a scheduled cron job by ID",
        prompt=CRON_DELETE_PROMPT,
        parameters={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Job ID returned by CronCreate.",
                },
            },
            "required": ["id"],
        },
        call=_cron_delete_call,
        is_read_only=False,
    ))

    registry.register(Tool(
        name="CronList",
        searchHint="list show all scheduled cron jobs",
        description="List all scheduled cron jobs",
        prompt=CRON_LIST_PROMPT,
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        call=_cron_list_call,
        is_read_only=True,
        auto_allow=True,
        plan_allowed=True,
    ))
