""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from bglab.tools.base import Tool
from bglab.tools.base import tool_error

# — 对齐 GrepTool prompt.ts:4 —
GREP_TOOL_NAME = "Grep"

# 结果截断 — 对齐 GrepTool 的 head_limit 默认 50
MAX_RESULTS = 50


def _rg_available() -> bool:
    """检查 ripgrep 是否可用。"""
    try:
        subprocess.run(['rg', '--version'], capture_output=True, timeout=2)
        return True
    except Exception:
        return False


def _grep_with_rg(
    pattern: str,
    path: str,
    glob_filter: str | None,
    output_mode: str,
    head_limit: int,
    case_insensitive: bool,
    context_lines: int | None,
) -> str:
    """使用 ripgrep 搜索。对齐 GrepTool 的 ripGrep() 调用。"""
    cmd = ['rg', '--no-heading', '--color', 'never']

    if case_insensitive:
        cmd.append('-i')

    if output_mode == 'content':
        cmd.append('-n')  # 行号
        if context_lines:
            cmd.extend(['-C', str(context_lines)])
    elif output_mode == 'files_with_matches':
        cmd.append('-l')
    elif output_mode == 'count':
        cmd.append('-c')

    if glob_filter:
        cmd.extend(['-g', glob_filter])

    cmd.append(pattern)
    cmd.append(path)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode not in {0, 1}:
            detail = (result.stderr or "ripgrep failed").strip()
            return tool_error(f"Grep error: {detail}")
        lines = result.stdout.strip().split('\n') if result.stdout.strip() else []
    except subprocess.TimeoutExpired:
        return tool_error("Grep timed out.")
    except FileNotFoundError:
        return tool_error("ripgrep (rg) not found.")

    if not lines or (len(lines) == 1 and lines[0] == ''):
        return f"No matches found for pattern: {pattern}"

    if len(lines) > head_limit:
        truncated = lines[:head_limit]
        output = '\n'.join(truncated)
        output += f"\n... [truncated: showing {head_limit} of {len(lines)} results]"
    else:
        output = '\n'.join(lines)

    return output


def _grep_with_python(
    pattern: str,
    path: str,
    glob_filter: str | None,
    output_mode: str,
    head_limit: int,
    case_insensitive: bool,
) -> str:
    """Python 内置 re 兜底搜索。"""
    try:
        flags = re.IGNORECASE if case_insensitive else 0
        regex = re.compile(pattern, flags)
    except re.error as e:
        return tool_error(f"Invalid regex pattern: {e}")

    search_path = Path(path) if os.path.isabs(path) else Path(os.getcwd()) / path
    if not search_path.exists():
        return tool_error(f"Path not found: {path}")

    # glob 过滤
    def _matches_glob(filepath: str) -> bool:
        if not glob_filter:
            return True
        return Path(filepath).match(glob_filter)

    results: list[str] = []

    if search_path.is_file():
        files = [search_path]
    else:
        files = [p for p in search_path.rglob('*') if p.is_file()]

    for filepath in files:
        if len(results) >= head_limit:
            break
        fp = str(filepath)
        if not _matches_glob(fp):
            continue

        # 跳过二进制
        try:
            with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                for i, line in enumerate(f, 1):
                    if len(results) >= head_limit:
                        break
                    if regex.search(line):
                        if output_mode == 'files_with_matches':
                            results.append(fp)
                            break
                        elif output_mode == 'count':
                            pass  # handled separately
                        else:  # content
                            results.append(f"{fp}:{i}:{line.rstrip()}")
        except Exception:
            continue

    if not results:
        return f"No matches found for pattern: {pattern}"

    return '\n'.join(results)


def grep(args: dict) -> str:
    """Grep 工具的 handler。对齐 GrepTool.call()。

    参数(对齐源码 inputSchema):
      pattern: str           — 正则表达式
      path: str (可选)        — 文件/目录路径, 默认 cwd
      glob: str (可选)        — 文件过滤 glob ("*.py")
      output_mode: str (可选)  — "content" | "files_with_matches" | "count"
      -i: bool (可选)         — 大小写不敏感
      -C: int (可选)          — 上下文行数
      head_limit: int (可选)  — 结果上限, 默认 50

    返回: grep 格式输出 (file:line:content)。
    """
    pattern = args.get("pattern", "")
    path = args.get("path", os.getcwd())
    glob_filter = args.get("glob")
    output_mode = args.get("output_mode", "files_with_matches")
    case_insensitive = args.get("-i", False)
    context = args.get("-C") or args.get("context")
    head_limit = min(args.get("head_limit", MAX_RESULTS), MAX_RESULTS)

    if not pattern:
        return tool_error("Error: pattern is required.")

    # 对齐源码: 优先 ripgrep，回退 Python
    if _rg_available():
        return _grep_with_rg(
            pattern, path, glob_filter, output_mode,
            head_limit, case_insensitive,
            context_lines=context,
        )
    else:
        return _grep_with_python(
            pattern, path, glob_filter, output_mode,
            head_limit, case_insensitive,
        )


GrepTool = Tool(
    name="Grep",
    searchHint="search file contents with regex",
    description="Search file contents with regex. Uses ripgrep if available.",
    prompt="""A powerful search tool built on ripgrep.

Usage:
- ALWAYS use Grep for search tasks. NEVER invoke grep or rg as a Bash command. The Grep tool has been optimized for correct permissions and access.
- Supports full regex syntax (e.g., "log.*Error", "function\\s+\\w+")
- Filter files with glob parameter (e.g., "*.js", "**/*.tsx") or type parameter
- Output modes: "content" shows matching lines, "files_with_matches" shows only file paths (default), "count" shows match counts
- Pattern syntax: Uses ripgrep (not grep) - literal braces need escaping""",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "The regular expression pattern to search for in file contents"
            },
            "path": {
                "type": "string",
                "description": "File or directory to search in. Defaults to current working directory."
            },
            "glob": {
                "type": "string",
                "description": 'Glob pattern to filter files (e.g. "*.js", "*.{ts,tsx}")'
            },
            "output_mode": {
                "type": "string",
                "enum": ["content", "files_with_matches", "count"],
                "description": 'Output mode. Defaults to "files_with_matches".'
            },
            "-i": {
                "type": "boolean",
                "description": "Case insensitive search"
            },
            "-C": {
                "type": "integer",
                "description": "Number of lines to show before and after each match"
            },
            "head_limit": {
                "type": "integer",
                "description": "Maximum number of results to return. Default 50."
            },
        },
        "required": ["pattern"],
    },
    call=grep,
    is_read_only=True,
    auto_allow=True,
    plan_allowed=True,
    check_permission=None,
)
