"""Background-agent notification queue used by query-loop attachments."""

from __future__ import annotations

import os
import tempfile
import threading
from dataclasses import dataclass


@dataclass
class AgentNotification:
    task_name: str
    status: str
    summary: str
    output_file: str = ""
    error: str = ""


_notifications: list[AgentNotification] = []
_lock = threading.Lock()


def enqueue_notification(
    name: str,
    status: str,
    result: str,
    error: str = "",
) -> None:
    output_file = ""
    if result:
        try:
            fd, output_file = tempfile.mkstemp(
                prefix=f"bglab_agent_{name}_",
                suffix=".txt",
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(result)
        except Exception:
            output_file = ""

    summary = result[:500].strip() if result else "(no output)"
    if len(result) > 500:
        summary += "..."

    with _lock:
        _notifications.append(AgentNotification(
            task_name=name,
            status=status,
            summary=summary,
            output_file=output_file,
            error=error,
        ))


def drain_notifications() -> list[AgentNotification]:
    with _lock:
        notifications = list(_notifications)
        _notifications.clear()
        return notifications


def has_pending() -> bool:
    with _lock:
        return bool(_notifications)
