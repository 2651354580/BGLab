"""PowerShell 工具 — Windows PowerShell 命令执行。

和 Bash/Cmd 隔离：Bash 走 /bin/bash（Unix），Cmd 走 cmd.exe（Windows），
PowerShell 走 powershell.exe（Windows）。LLM 根据平台信息自选。
"""

from __future__ import annotations

import subprocess
import os
import re

from bglab.tools.base import Tool

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000

_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r'Remove-Item\s+-Recurse\s+-Force\s+\w:\\', 'recursive force delete from system root'),
    (r'Format-Volume\b', 'formatting volumes is destructive'),
    (r'Clear-Disk\b', 'clearing disks is destructive'),
    (r'>\s*\w:\\Windows\\', 'writing to Windows directory'),
    (r'Set-ExecutionPolicy\b', 'changing execution policy is dangerous'),
]


def _check_dangerous(command: str) -> str | None:
    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return f"Dangerous command blocked: {reason}\nCommand: {command[:200]}"
    return None


def run_powershell(args: dict) -> str:
    """PowerShell 工具的 handler。"""

    command = args.get("command", "")
    timeout_ms = min(args.get("timeout", DEFAULT_TIMEOUT_MS), MAX_TIMEOUT_MS)

    if not command.strip():
        return "Error: empty command"

    danger = _check_dangerous(command)
    if danger:
        return danger

    timeout_sec = timeout_ms / 1000.0

    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout_sec,
            cwd=os.getcwd(), env={**os.environ},
        )

        output_parts: list[str] = []
        if result.stdout:
            output_parts.append(result.stdout.rstrip())
        if result.stderr:
            output_parts.append(f"[stderr]\n{result.stderr.rstrip()}")

        output = '\n'.join(output_parts) if output_parts else "(no output)"

        max_chars = 50_000
        if len(output) > max_chars:
            output = output[:max_chars] + f"\n... [truncated {len(output) - max_chars} chars]"

        if result.returncode != 0:
            output += f"\n\nExit code: {result.returncode}"

        return output

    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout_ms}ms: {command[:200]}"
    except FileNotFoundError:
        return "PowerShell.exe not found. This tool requires Windows."


def _powershell_check_permission(tool_input: dict):
    from bglab.permissions.types import PermissionBehavior, PermissionDecision
    command = tool_input.get("command", "").strip()
    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                reason=f"dangerous command blocked: {reason}",
            )
    return PermissionDecision(behavior=PermissionBehavior.ALLOW, reason="passthrough")


PowerShellTool = Tool(
    name="PowerShell",
    searchHint="execute powershell command",
    description="Execute a PowerShell command on Windows.",
    prompt="""Executes a given PowerShell command on Windows and returns its output.

Uses powershell.exe with -NoProfile -NonInteractive for clean, predictable output.
For basic cmd.exe commands, use the Cmd tool. For Unix bash commands, use the Bash tool.

Working directory persists between commands, but shell state does not.

# Instructions
- Use PowerShell syntax: Get-ChildItem, Get-Content, Select-String, Set-Content, etc.
- Many Unix commands work as aliases in PowerShell (ls, cat, rm, curl, etc.).
- To list files: Get-ChildItem (or ls / dir).
- To read a file: Get-Content (or cat / type).
- To search: Select-String (or sls).
- Always quote file paths that contain spaces.
- You may specify an optional timeout in milliseconds (up to 600000ms / 10 minutes). Default is 120000ms.
- Chain commands with ; (semicolon).""",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The PowerShell command to execute."
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in milliseconds (max 600000). Default 120000."
            },
            "description": {
                "type": "string",
                "description": "Human-readable description of what this command does."
            },
        },
        "required": ["command"],
    },
    call=run_powershell,
    is_read_only=False,
    check_permission=_powershell_check_permission,
)
