"""Cmd 工具 — Windows cmd.exe 命令执行。

和 Bash 隔离：Bash 走 /bin/bash（Unix），Cmd 走 %COMSPEC%（Windows）。
两个工具都注册，LLM 根据环境信息选。
"""

from __future__ import annotations

import subprocess
import os
import re

from bglab.tools.base import Tool

DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000

_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r'\brmdir\s+/s\s+\w:\\', 'recursive delete of system drive'),
    (r'\bdel\s+/f\s+/s\s+\w:\\', 'force delete from system root'),
    (r'\bformat\b', 'formatting disks is destructive'),
    (r'\bdiskpart\b', 'diskpart can wipe drives'),
    (r'>\s*\w:\\Windows\\', 'writing to Windows directory'),
]


def _check_dangerous(command: str) -> str | None:
    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return f"Dangerous command blocked: {reason}\nCommand: {command[:200]}"
    return None


def run_cmd(args: dict) -> str:
    """Cmd 工具的 handler。"""

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
            command, shell=True, capture_output=True, text=True,
            timeout=timeout_sec, cwd=os.getcwd(), env={**os.environ},
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
        return "Cmd.exe not found. This tool requires Windows."


def _cmd_check_permission(tool_input: dict):
    from bglab.permissions.types import PermissionBehavior, PermissionDecision
    command = tool_input.get("command", "").strip()
    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                reason=f"dangerous command blocked: {reason}",
            )
    return PermissionDecision(behavior=PermissionBehavior.ALLOW, reason="passthrough")


CmdTool = Tool(
    name="Cmd",
    searchHint="execute windows cmd command",
    description="Execute a Windows cmd.exe command.",
    prompt="""Executes a given cmd command on Windows and returns its output.

Uses cmd.exe (Windows Command Prompt) syntax. For PowerShell commands, use the PowerShell tool.

Working directory persists between commands, but shell state does not.

# Instructions
- Use Cmd syntax: dir (not ls), type (not cat), echo, del, mkdir, rmdir, etc.
- If your command creates new directories or files, verify the parent directory exists first.
- Always quote file paths that contain spaces.
- Use absolute paths when possible.
- You may specify an optional timeout in milliseconds (up to 600000ms / 10 minutes). Default is 120000ms.
- Chain dependent commands with &&.""",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The cmd command to execute (Windows Command Prompt syntax)."
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
    call=run_cmd,
    is_read_only=False,
    check_permission=_cmd_check_permission,
)
