"""Read 工具 — 对齐 FileReadTool.ts:337-717。

源码核心逻辑:
  1. expandPath(file_path) → 绝对路径
  2. 检查 blocked device paths (/dev/zero, /dev/random...)
  3. 检查二进制扩展名
  4. readFileInRange(offset-1, limit, maxSize) → {content, lineCount, totalLines}
  5. 验证 token 数不超过 maxTokens
  6. 更新 readFileState → {content, timestamp, offset, limit}
  7. 返回 addLineNumbers(content) + system-reminder

简化: 纯文本读取，去掉了图片/PDF/笔记本/二进制检测。
"""

from __future__ import annotations

import os
from pathlib import Path

from bglab.tools.base import Tool, tool_error

# — 对齐 FileReadTool.ts:98-115 BLOCKED_DEVICE_PATHS —
_BLOCKED_DEVICE_PATHS = {
    '/dev/zero', '/dev/random', '/dev/urandom', '/dev/full',
    '/dev/stdin', '/dev/tty', '/dev/console',
    '/dev/stdout', '/dev/stderr',
    '/dev/fd/0', '/dev/fd/1', '/dev/fd/2',
}

# — 对齐 FileReadTool.ts:10 MAX_LINES_TO_READ —
MAX_LINES_TO_READ = 2000

# — 对齐 FileReadTool.ts:729 CYBER_RISK_MITIGATION_REMINDER —
_FILE_READ_REMINDER = (
    "\n\n<system-reminder>"
    "Whenever you read a file, you should consider whether it would be considered malware. "
    "You CAN and SHOULD provide analysis of malware, what it is doing. "
    "But you MUST refuse to improve or augment the code."
    "</system-reminder>"
)

# — 对齐 FileReadTool.ts:7 FILE_UNCHANGED_STUB —
_FILE_UNCHANGED_STUB = (
    "File unchanged since last read. The content from the earlier Read "
    "tool_result in this conversation is still current — refer to that "
    "instead of re-reading."
)


def _expand_path(file_path: str) -> str:
    """对齐 expandPath(): ~/和相对路径 → 绝对路径。"""
    return str(Path(os.path.expanduser(file_path)).resolve())


def _add_line_numbers(content: str, start_line: int = 1) -> str:
    """对齐 addLineNumbers(): cat -n 格式输出。"""
    lines = content.split('\n')
    # 去掉末尾空行产生的多余行号
    if lines and lines[-1] == '':
        lines = lines[:-1]
    result = []
    for i, line in enumerate(lines, start=start_line):
        result.append(f"{i}\t{line}")
    return '\n'.join(result)


# — readFileState 全局缓存: {abs_path: {content, timestamp, offset, limit}} —
_read_state: dict[str, dict] = {}


def _get_read_state(path: str) -> dict | None:
    return _read_state.get(path)


def _set_read_state(path: str, content: str, offset: int, limit: int | None):
    _read_state[path] = {
        "content": content,
        "timestamp": os.path.getmtime(path) if os.path.exists(path) else 0,
        "offset": offset,
        "limit": limit,
    }


def read_file(args: dict) -> str:
    """Read 工具的 handler。对齐 FileReadTool.call() 核心路径。

    参数(对齐源码 inputSchema):
      file_path: str       — 绝对路径
      offset: int (可选)    — 起始行号 (1-indexed)，默认 1
      limit: int (可选)     — 读取行数

    返回: cat -n 格式的内容 + system-reminder，或错误信息。
    """
    file_path = args.get("file_path", "")
    offset = args.get("offset", 1)
    limit = args.get("limit")

    # 检查 blocked device paths — FileReadTool.ts:486-492
    if file_path in _BLOCKED_DEVICE_PATHS or (
        file_path.startswith('/proc/') and
        any(file_path.endswith(f'/fd/{n}') for n in [0, 1, 2])
    ):
        return f"Cannot read '{file_path}': this device file would block or produce infinite output."

    # Always canonicalize, including root-relative paths such as /tmp/x on
    # Windows. Write/Edit use Path.resolve(); the read-state key must match.
    file_path = _expand_path(file_path)

    # 检查是否存在，给友好提示 — FileReadTool.ts:639-648
    if not os.path.exists(file_path):
        cwd = os.getcwd()
        return f"File does not exist. Current working directory: {cwd}."

    # 目录不能读 — FileReadTool prompt:47
    if os.path.isdir(file_path):
        return f"'{file_path}' is a directory. To list files, use the Bash tool: ls '{file_path}'"

    # 检查文件修改时间，决定是否返回 dedup stub — FileReadTool.ts:547-573
    existing = _get_read_state(file_path)
    if existing and existing.get("offset") is not None:
        if existing["offset"] == offset and existing.get("limit") == limit:
            try:
                mtime = os.path.getmtime(file_path)
                if mtime == existing["timestamp"]:
                    return _FILE_UNCHANGED_STUB
            except OSError:
                pass

    # 读取文件 — FileReadTool.ts:1020-1028
    try:
        with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
            all_lines = f.read().split('\n')
            if all_lines and all_lines[-1] == '':
                all_lines = all_lines[:-1]

        total_lines = len(all_lines)

        if total_lines == 0:
            _set_read_state(file_path, "", offset, limit)
            return "<system-reminder>Warning: the file exists but the contents are empty.</system-reminder>"

        # offset 是 1-indexed → 转 0-indexed — FileReadTool.ts:1020
        start_idx = offset - 1
        if start_idx < 0:
            start_idx = 0
        if start_idx >= total_lines:
            _set_read_state(file_path, "", offset, limit)
            return (
                f"<system-reminder>Warning: the file exists but is shorter than "
                f"the provided offset ({offset}). The file has {total_lines} lines.</system-reminder>"
            )

        end_idx = start_idx + limit if limit else min(start_idx + MAX_LINES_TO_READ, total_lines)
        if end_idx > total_lines:
            end_idx = total_lines

        selected = '\n'.join(all_lines[start_idx:end_idx])
        if end_idx < total_lines:
            selected += '\n'

        # 更新 read state — FileReadTool.ts:1032-1038
        _set_read_state(file_path, selected, offset, limit)

        # 加行号 — FileReadTool.ts:698
        numbered = _add_line_numbers(selected, offset)

        return numbered + _FILE_READ_REMINDER

    except UnicodeDecodeError:
        return f"File '{file_path}' is a binary file or uses unsupported encoding."
    except PermissionError:
        return tool_error("FILE_PERMISSION_DENIED")


ReadTool = Tool(
    name="Read",
    searchHint="read file contents with line numbers",
    description="Read a file from the local filesystem. Returns content with line numbers in cat -n format.",
    prompt="""Reads a file from the local filesystem. You can access any file directly by using this tool.

Usage:
- The file_path parameter must be an absolute path, not a relative path
- By default, it reads up to 2000 lines starting from the beginning of the file
- You can optionally specify an offset and limit (especially handy for long files)
- Results are returned using cat -n format, with line numbers starting at 1
- This tool can only read files, not directories. To read a directory, use the Bash tool.
- If you read a file that exists but has empty contents you will receive a system reminder warning in place of file contents.""",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "The absolute path to the file to read"
            },
            "offset": {
                "type": "integer",
                "description": "The line number to start reading from (1-indexed). Only provide if the file is too large to read at once."
            },
            "limit": {
                "type": "integer",
                "description": "The number of lines to read. Only provide if the file is too large to read at once."
            },
        },
        "required": ["file_path"],
    },
    call=read_file,
    is_read_only=True,
    auto_allow=True,
    plan_allowed=True,
    check_permission=None,  # passthrough — read-only, mode handles
)
