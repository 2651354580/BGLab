"""Write 工具 — 对齐 FileWriteTool.ts:94-434。

源码核心逻辑:
  1. expandPath(file_path) → 绝对路径
  2. 确保父目录存在 (mkdir)
  3. 检查 readFileState: 必须读过，且未被修改 (timestamp check)
  4. 读原文件内容 (用于构造 diff)
  5. 写新内容到磁盘
  6. 更新 readFileState: {content, timestamp, offset: undefined}
  7. 返回 "created" 或 "updated"

简化: 去掉了 LSP/VSCode 通知、git diff、skill discovery、fileHistory。
"""

from __future__ import annotations

import os
from pathlib import Path

from bglab.tools.base import Tool, tool_error

# — 对齐 FileWriteTool.ts:5 —
_FILE_WRITE_TOOL_NAME = 'Write'

# — 对齐 FileEditTool.ts:84 FILE_UNEXPECTEDLY_MODIFIED_ERROR —
_FILE_UNEXPECTEDLY_MODIFIED_ERROR = (
    "File has been modified since read, either by the user or by a linter. "
    "Read it again before attempting to write it."
)

# — 复用 read tool 的 readFileState —
from bglab.tools.read import _read_state, _get_read_state


def write_file(args: dict) -> str:
    """Write 工具的 handler。对齐 FileWriteTool.call()。

    参数(对齐源码 inputSchema):
      file_path: str    — 绝对路径
      content: str      — 要写入的内容

    返回: "created" 或 "updated" 消息。
    """
    file_path = args.get("file_path", "")
    content = args.get("content", "")

    # 对齐 expandPath
    abs_path = str(Path(os.path.expanduser(file_path)).resolve())

    # 确保父目录存在 — FileWriteTool.ts:254
    parent = os.path.dirname(abs_path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)

    file_exists = os.path.exists(abs_path)

    if file_exists:
        # 检查 readFileState: 必须读过 — FileWriteTool.ts:198-206
        last_read = _get_read_state(abs_path)
        if not last_read:
            return tool_error(
                "File has not been read yet. Read it first before writing to it."
            )

        # 检查文件未被外部修改 — FileWriteTool.ts:279-294
        try:
            last_write_time = int(os.path.getmtime(abs_path))
        except OSError:
            last_write_time = 0

        if last_read["timestamp"] and last_write_time > last_read["timestamp"]:
            # 比较内容确认 — 防 Windows 时间戳误报
            if last_read.get("offset") is None and last_read.get("limit") is None:
                # 是全量读取，比较内容
                try:
                    with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
                        current = f.read()
                    if current == last_read["content"]:
                        pass  # 内容没变，安全
                    else:
                        return tool_error(_FILE_UNEXPECTEDLY_MODIFIED_ERROR)
                except Exception:
                    return tool_error(_FILE_UNEXPECTEDLY_MODIFIED_ERROR)
            else:
                return tool_error(_FILE_UNEXPECTEDLY_MODIFIED_ERROR)

    # 写文件 — FileWriteTool.ts:305
    try:
        with open(abs_path, 'w', encoding='utf-8', newline='') as f:
            f.write(content)
    except PermissionError:
        return tool_error("FILE_PERMISSION_DENIED")

    # 更新 readFileState — FileWriteTool.ts:332-337
    try:
        mtime = int(os.path.getmtime(abs_path))
    except OSError:
        mtime = 0
    _read_state[abs_path] = {
        "content": content,
        "timestamp": mtime,
        "offset": None,  # None 表示这不是通过 Read 写入的
        "limit": None,
    }

    # 对齐 FileWriteTool.ts:419-432 mapToolResultToToolResultBlockParam
    if file_exists:
        return f"The file {file_path} has been updated successfully."
    else:
        return f"File created successfully at: {file_path}"


WriteTool = Tool(
    name="Write",
    searchHint="create or overwrite a file",
    description="Write a file to the local filesystem.",
    prompt="""Writes a file to the local filesystem.

Usage:
- This tool will overwrite the existing file if there is one at the provided path.
- If this is an existing file, you MUST use the Read tool first to read the file's contents. This tool will fail if you did not read the file first.
- Prefer the Edit tool for modifying existing files — it only sends the diff. Only use this tool to create new files or for complete rewrites.
- NEVER create documentation files (*.md) or README files unless explicitly requested by the User.""",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "The absolute path to the file to write"
            },
            "content": {
                "type": "string",
                "description": "The content to write to the file"
            },
        },
        "required": ["file_path", "content"],
    },
    call=write_file,
    is_read_only=False,
    check_permission=None,
)
