"""Project-boundary checks for file tools."""

from __future__ import annotations

import os
from pathlib import Path


PATH_SCOPED_TOOLS = frozenset({"Read", "Write", "Edit"})


def is_within_project(
    tool_name: str,
    tool_input: dict,
    project_cwd: str | None,
) -> bool | None:
    """Return whether a file-tool target resolves below ``project_cwd``.

    ``None`` means the tool or input has no path boundary to check; callers
    retain their existing validation and permission behavior in that case.
    Existing symlinks and ``..`` segments are resolved before containment is
    tested, so a lexical prefix cannot grant project scope.
    """
    if tool_name not in PATH_SCOPED_TOOLS:
        return None
    raw_path = tool_input.get("file_path")
    if not isinstance(raw_path, str) or not raw_path:
        return None

    try:
        root = Path(project_cwd or Path.cwd()).expanduser().resolve(strict=False)
        target = Path(os.path.expanduser(raw_path))
        if not target.is_absolute():
            target = Path.cwd() / target
        target = target.resolve(strict=False)
        target.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True
