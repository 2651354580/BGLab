""

from __future__ import annotations
import time
from bglab.tools.base import Tool, ToolRegistry


SLEEP_PROMPT = """Wait for a specified duration. The user can interrupt the sleep at any time.

Use this when the user tells you to sleep or rest, when you have nothing to do, or when you're waiting for something.

Prefer this over `Bash(sleep ...)` — it doesn't hold a shell process.

Each wake-up costs an API call, but the prompt cache expires after 5 minutes of inactivity — balance accordingly."""


def _sleep_call(args: dict) -> str:
    duration = max(0.1, min(float(args.get("duration", 1.0)), 600.0))
    time.sleep(duration)
    return f"Slept for {duration:.1f}s"


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="Sleep",
        searchHint="wait pause delay seconds",
        description="Wait for a specified duration",
        prompt=SLEEP_PROMPT,
        parameters={
            "type": "object",
            "properties": {
                "duration": {
                    "type": "number",
                    "description": "Seconds to sleep (0.1–600, default 1.0)",
                },
            },
            "required": [],
        },
        call=_sleep_call,
        is_read_only=True,
        auto_allow=True,
        plan_allowed=True,
    ))
