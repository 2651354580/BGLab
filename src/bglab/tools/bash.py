"""Bash 工具 — 对齐 BashTool.tsx 的命令执行和安全检查。

源码核心逻辑:
  1. 检查危险命令 (rm -rf /, sudo, etc.)
  2. timeout 控制 (默认 120s, 最大 600s)
  3. 子进程执行，捕获 stdout + stderr
  4. 返回 {stdout, stderr, exit_code}

简化: 去掉了沙箱、git 指令、后台任务、LSP 集成。
"""

from __future__ import annotations

import subprocess
import os
import re
import shutil

from bglab.tools.base import Tool

# — 对齐 BashTool: 默认超时 120s, 最大 600s —
DEFAULT_TIMEOUT_MS = 120_000
MAX_TIMEOUT_MS = 600_000

# — 危险命令模式 —
_DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    (r'\brm\s+-rf\s+/', 'rm -rf / is destructive'),
    (r'\brm\s+-rf\s+~', 'rm -rf ~ is destructive'),
    (r'\brm\s+-rf\s+\*', 'rm -rf * may be destructive'),
    (r'\bsudo\b', 'sudo requires user confirmation'),
    (r'>\s*/dev/sda', 'writing to raw block devices is destructive'),
    (r'\bdd\s+if=', 'dd can overwrite disks'),
    (r'\bmkfs\.', 'formatting filesystems is destructive'),
    (r'\bchmod\s+777\s+/', 'chmod 777 on system dirs is dangerous'),
    (r'>\s*/etc/', 'writing to /etc requires confirmation'),
]


def _check_dangerous(command: str) -> str | None:
    """检查危险命令。返回错误消息或 None。

    对齐 BashTool/bashSecurity.ts 的检查逻辑。
    """
    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, command):
            return f"Dangerous command blocked: {reason}\nCommand: {command[:200]}"
    return None


def _sanitize_cwd(cwd: str | None) -> str | None:
    """如果 cwd 是相对路径，转为绝对路径。"""
    if cwd is None:
        return None
    if not os.path.isabs(cwd):
        return os.path.abspath(cwd)
    return cwd if os.path.isdir(cwd) else None


def run_bash(args: dict) -> str:
    ""
    command = args.get("command", "")
    timeout_ms = min(args.get("timeout", DEFAULT_TIMEOUT_MS), MAX_TIMEOUT_MS)

    if not command.strip():
        return "Error: empty command"

    # 安全检查 — 对齐 BashTool/bashSecurity.ts
    danger = _check_dangerous(command)
    if danger:
        return danger

    timeout_sec = timeout_ms / 1000.0

    try:
        # ``shell=True`` expects cmd.exe semantics on Windows.  Passing a
        # Git/MSYS bash shim as ``executable`` is unreliable (and can return
        # 127 before the command is evaluated), so only select bash on POSIX.
        shell_executable = shutil.which("bash") if os.name != "nt" else None
        result = subprocess.run(
            command,
            shell=True,
            executable=shell_executable,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_sec,
            cwd=os.getcwd(),
            env={**os.environ},
        )

        output_parts: list[str] = []
        if result.stdout:
            output_parts.append(result.stdout.rstrip())
        if result.stderr:
            output_parts.append(f"[stderr]\n{result.stderr.rstrip()}")

        output = '\n'.join(output_parts) if output_parts else "(no output)"

        # 超长截断 — 对齐工具结果截断
        max_chars = 50_000
        if len(output) > max_chars:
            output = output[:max_chars] + f"\n... [truncated {len(output) - max_chars} chars]"

        if result.returncode != 0:
            output += f"\n\nExit code: {result.returncode}"

        return output

    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout_ms}ms: {command[:200]}"
    except FileNotFoundError:
        return "Command not found. The shell or command may not be available."


def _bash_check_permission(tool_input: dict):
    """Bash 工具自身的权限检查 — 拦截危险命令模式。"""
    from bglab.permissions.types import PermissionBehavior, PermissionDecision
    command = tool_input.get("command", "").strip()

    for pattern, reason in _DANGEROUS_PATTERNS:
        if re.search(pattern, command):
            return PermissionDecision(
                behavior=PermissionBehavior.DENY,
                reason=f"dangerous command blocked: {reason}",
            )
    return PermissionDecision(
        behavior=PermissionBehavior.ALLOW,
        reason="passthrough",
    )


BashTool = Tool(
    name="Bash",
    aliases=["Shell"],
    searchHint="execute shell commands in bash",
    description="Execute a bash command.",
    prompt="""Executes a given bash command and returns its output.

The working directory persists between commands, but shell state does not. The shell environment is initialized from the user's profile.

IMPORTANT: Avoid using this tool to run cat, head, tail, sed, awk, or echo commands, unless explicitly instructed. Instead, use the appropriate dedicated tool:
- Read files: Use Read (NOT cat/head/tail)
- Edit files: Use Edit (NOT sed/awk)
- Write files: Use Write (NOT echo >/cat <<EOF)
- Communication: Output text directly (NOT echo/printf)

# Instructions
- If your command will create new directories or files, first use this tool to run ls to verify the parent directory exists.
- Always quote file paths that contain spaces with double quotes.
- Try to maintain your current working directory by using absolute paths.
- You may specify an optional timeout in milliseconds (up to 600000ms / 10 minutes). Default timeout is 120000ms.
- When issuing multiple independent commands, make multiple Bash tool calls in a single message.
- When commands depend on each other, use '&&' to chain them together.
- DO NOT use newlines to separate commands (newlines are ok in quoted strings).
- For git: Never skip hooks, never force push to main, prefer new commits over amend.""",
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to execute"
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in milliseconds (max 600000). Default 120000."
            },
            "description": {
                "type": "string",
                "description": "Human-readable description of what this command does. Used for permission prompts.",
            },
        },
        "required": ["command"],
    },
    call=run_bash,
    is_read_only=False,
    check_permission=_bash_check_permission,
)
