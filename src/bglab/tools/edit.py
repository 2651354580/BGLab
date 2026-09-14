"""Edit 工具 — 对齐 FileEditTool.ts:86-595。

源码核心逻辑:
  1. expandPath(file_path) → 绝对路径
  2. 检查 readFileState: 必须读过，且未被修改
  3. 读文件内容
  4. findActualString: 在文件中找 old_string（处理引号差异）
  5. 唯一性检查: 如果 replace_all=false 且匹配 > 1 次 → 报错
  6. 替换: replace(old, new) 或 replace_all
  7. 写回磁盘 (writeTextContent)
  8. 更新 readFileState
  9. 返回更新确认

简化: 去掉了 LSP/VSCode、git diff、skill discovery。
"""

from __future__ import annotations

import os
from pathlib import Path

from bglab.tools.base import Tool, tool_error

# — 复用 read state —
from bglab.tools.read import _read_state, _get_read_state


def edit_file(args: dict) -> str:
    """Edit 工具的 handler。对齐 FileEditTool.call()。

    参数(对齐源码 inputSchema):
      file_path: str        — 绝对路径
      old_string: str       — 要替换的文本
      new_string: str       — 替换后的文本
      replace_all: bool     — 是否替换所有出现 (默认 false)

    返回: 成功提示或错误信息。
    """
    file_path = args.get("file_path", "")
    old_string = args.get("old_string", "")
    new_string = args.get("new_string", "")
    replace_all = args.get("replace_all", False)

    # 对齐 FileEditTool.ts:148-155: old == new?
    if old_string == new_string:
        return tool_error("No changes to make: old_string and new_string are exactly the same.")

    abs_path = str(Path(os.path.expanduser(file_path)).resolve())

    # 文件不存在 — FileEditTool.ts:224-246
    if not os.path.exists(abs_path):
        if old_string == '':
            # 空 old_string + 文件不存在 = 创建新文件
            parent = os.path.dirname(abs_path)
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
            try:
                with open(abs_path, 'w', encoding='utf-8', newline='') as f:
                    f.write(new_string)
                _read_state[abs_path] = {
                    "content": new_string,
                    "timestamp": int(os.path.getmtime(abs_path)),
                    "offset": None,
                    "limit": None,
                }
                return f"File created successfully at: {file_path}"
            except PermissionError:
                return tool_error("FILE_PERMISSION_DENIED")

        cwd = os.getcwd()
        return tool_error(f"File does not exist. Current working directory: {cwd}.")

    # 空 old_string + 文件存在但有内容 — FileEditTool.ts:249-257
    if old_string == '':
        try:
            with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
                existing = f.read()
            if existing.strip() != '':
                return tool_error("Cannot create new file - file already exists.")
        except PermissionError:
            return tool_error("FILE_PERMISSION_DENIED")

    # 检查 readFileState — FileEditTool.ts:275-287
    last_read = _get_read_state(abs_path)
    if not last_read:
        return tool_error("File has not been read yet. Read it first before editing it.")

    # 检查文件未被修改 — FileEditTool.ts:290-311
    try:
        last_write_time = int(os.path.getmtime(abs_path))
    except OSError:
        last_write_time = 0
    if last_read["timestamp"] and last_write_time > last_read["timestamp"]:
        return tool_error("File has been modified since read, either by the user or by a linter. Read it again before attempting to edit it.")

    # 读文件 — FileEditTool.ts:444-446
    try:
        with open(abs_path, 'r', encoding='utf-8', errors='replace') as f:
            file_content = f.read()
    except PermissionError:
        return tool_error("FILE_PERMISSION_DENIED")

    # 在文件中找 old_string — FileEditTool.ts:316-327
    if old_string not in file_content:
        return tool_error(
            f"String to replace not found in file.\n"
            f"String: {old_string[:200]}"
            + ("..." if len(old_string) > 200 else "")
        )

    # 唯一性检查 — FileEditTool.ts:329-343
    count = file_content.count(old_string)
    if count > 1 and not replace_all:
        return tool_error(
            f"Found {count} matches of the string to replace, but replace_all is "
            f"false. To replace all occurrences, set replace_all to true. To replace "
            f"only one occurrence, please provide more context to uniquely identify "
            f"the instance."
        )

    # 替换 — FileEditTool.ts:482-488 getPatchForEdit
    if replace_all:
        new_content = file_content.replace(old_string, new_string)
    else:
        new_content = file_content.replace(old_string, new_string, 1)

    # 写回 — FileEditTool.ts:491
    try:
        with open(abs_path, 'w', encoding='utf-8', newline='') as f:
            f.write(new_content)
    except PermissionError:
        return tool_error("FILE_PERMISSION_DENIED")

    # 更新 readFileState — FileEditTool.ts:520-525
    try:
        mtime = int(os.path.getmtime(abs_path))
    except OSError:
        mtime = 0
    _read_state[abs_path] = {
        "content": new_content,
        "timestamp": mtime,
        "offset": None,
        "limit": None,
    }

    # 对齐 FileEditTool.ts:575-594 mapToolResultToToolResultBlockParam
    if replace_all:
        return f"The file {file_path} has been updated. All occurrences were successfully replaced."
    return f"The file {file_path} has been updated successfully."


EditTool = Tool(
    name="Edit",
    searchHint="find-and-replace text in files",
    description="Edit a file by performing exact string replacements.",
    prompt="""Performs exact string replacements in files.

Usage:
- You must use your Read tool at least once in the conversation before editing. This tool will error if you attempt an edit without reading the file.
- When editing text from Read tool output, ensure you preserve the exact indentation (tabs/spaces) as it appears AFTER the line number prefix. The line number prefix format is: line number + tab. Everything after that is the actual file content to match. Never include any part of the line number prefix in the old_string or new_string.
- ALWAYS prefer editing existing files in the codebase. NEVER write new files unless explicitly required.
- The edit will FAIL if old_string is not unique in the file. Either provide a larger string with more surrounding context to make it unique or use replace_all to change every instance of old_string.
- Use replace_all for replacing and renaming strings across the file.""",
    parameters={
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "The absolute path to the file to modify"
            },
            "old_string": {
                "type": "string",
                "description": "The text to replace"
            },
            "new_string": {
                "type": "string",
                "description": "The text to replace it with (must be different from old_string)"
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences of old_string (default false)",
                "default": False,
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    },
    call=edit_file,
    is_read_only=False,
    check_permission=None,
)
