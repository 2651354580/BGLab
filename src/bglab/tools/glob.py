""

from __future__ import annotations

import os
import time
from pathlib import Path

from bglab.tools.base import Tool

# — 对齐 GlobTool: 结果上限 100 —
MAX_FILES = 100


def glob_files(args: dict) -> str:
    """Glob 工具的 handler。对齐 GlobTool.call()。

    参数(对齐源码 inputSchema):
      pattern: str       — glob 模式 ("**/*.py")
      path: str (可选)    — 搜索目录, 默认 cwd

    返回: 按 mtime 降序排列的文件路径列表。
    """
    pattern = args.get("pattern", "*")
    base_path = args.get("path", os.getcwd())

    if not os.path.isabs(base_path):
        base_path = os.path.abspath(base_path)

    if not os.path.isdir(base_path):
        return f"Directory not found: {base_path}"

    start = time.time()

    # 收集匹配文件
    files = []
    try:
        for p in Path(base_path).rglob(pattern):
            if p.is_file():
                files.append(p)
    except Exception as e:
        return f"Error during glob: {e}"

    # 按 mtime 降序 — 对齐 GlobTool 输出
    try:
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        pass  # 某些文件无权限，跳过排序

    duration_ms = int((time.time() - start) * 1000)
    filenames = [str(f) for f in files[:MAX_FILES]]
    truncated = len(files) > MAX_FILES

    if not filenames:
        return f"No files matching '{pattern}' in {base_path}"

    output_lines = [f"{len(filenames)} file(s) found in {duration_ms}ms:"]
    for fn in filenames:
        output_lines.append(fn)

    if truncated:
        output_lines.append(f"\n... [truncated: showing {MAX_FILES} of {len(files)} results]")

    return '\n'.join(output_lines)


GlobTool = Tool(
    name="Glob",
    searchHint="find files by name pattern",
    description="Find files matching a glob pattern.",
    prompt="""- Fast file pattern matching tool that works with any codebase size
- Supports glob patterns like "**/*.js" or "src/**/*.ts"
- Returns matching file paths sorted by modification time
- Use this tool when you need to find files by name patterns""",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "The glob pattern to match files against"
            },
            "path": {
                "type": "string",
                "description": "The directory to search in. Defaults to current working directory."
            },
        },
        "required": ["pattern"],
    },
    call=glob_files,
    is_read_only=True,
    auto_allow=True,
    plan_allowed=True,
    check_permission=None,
)
