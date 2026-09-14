""

from __future__ import annotations
import os
import random
import string
import subprocess
from bglab.tools.base import Tool, ToolRegistry


_WORKTREE_SESSION: dict | None = None  # Track current worktree session


ENTER_WORKTREE_PROMPT = """Creates an isolated git worktree and switches the session's working directory into it. Used when the user explicitly asks to work in a worktree.

This tool creates a new git worktree on a new branch inside .claude/worktrees/.
- In a git repository: creates a new git worktree
- The session's working directory switches to the new worktree
- Use ExitWorktree to leave the worktree

Parameters:
- name: Optional name for the worktree. Each "/"-separated segment may contain only letters, digits, dots, underscores, and dashes; max 64 chars total. A random name is generated if not provided.
- path: Optional path to an existing worktree to enter instead of creating one."""


EXIT_WORKTREE_PROMPT = """Exit a worktree session and return the session to the original working directory.

Parameters:
- action: "keep" (leave worktree on disk) or "remove" (delete worktree and branch)
- discard_changes: Required true when action is "remove" and the worktree has uncommitted changes. The tool will refuse otherwise."""


def _validate_slug(name: str) -> str | None:
    """Validate worktree slug segments."""
    if len(name) > 64:
        return f"Name too long ({len(name)} > 64)"
    for seg in name.split("/"):
        if not seg:
            return "Empty segment in name"
        for ch in seg:
            if not (ch.isalnum() or ch in "._-"):
                return f"Invalid character '{ch}' in name segment '{seg}'"
    return None


def _random_name() -> str:
    """Generate a random worktree name."""
    adj = ["bright", "calm", "cool", "dark", "deep", "fast", "good", "keen",
           "kind", "loud", "mild", "neat", "pure", "quick", "safe", "sharp",
           "smart", "soft", "warm", "wild"]
    noun = ["bird", "crane", "dock", "fish", "gate", "hill", "lake", "leaf",
            "mist", "moon", "path", "pine", "pond", "reef", "rock", "ship",
            "star", "swan", "tree", "wind"]
    return f"{random.choice(adj)}_{random.choice(noun)}_{''.join(random.choices(string.ascii_lowercase + string.digits, k=4))}"


def _get_git_root(cwd: str) -> str | None:
    """Find git root directory."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, cwd=cwd, timeout=10,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _set_session_cwd(path: str) -> None:
    from bglab.session.state import session_state

    session_state.cwd = os.path.abspath(path)


def _enter_worktree_call(args: dict) -> str:
    global _WORKTREE_SESSION

    if _WORKTREE_SESSION is not None:
        return f"Already in a worktree session: {_WORKTREE_SESSION['worktreePath']}"

    cwd = args.get("_cwd", os.getcwd())

    # Check if entering an existing worktree path
    existing_path = args.get("path")
    if existing_path:
        existing_path = str(existing_path)
        if not os.path.isdir(existing_path):
            return f"Error: path does not exist: {existing_path}"
        try:
            result = subprocess.run(
                ["git", "worktree", "list"],
                capture_output=True, text=True, cwd=cwd, timeout=10,
            )
            for line in result.stdout.splitlines():
                if existing_path in line:
                    _WORKTREE_SESSION = {
                        "originalCwd": cwd,
                        "worktreePath": existing_path,
                        "worktreeBranch": None,
                    }
                    os.chdir(existing_path)
                    _set_session_cwd(existing_path)
                    return f"Entered existing worktree at {existing_path}"
        except Exception:
            pass
        return f"Error: {existing_path} is not a registered git worktree"

    # Create a new worktree
    git_root = _get_git_root(cwd)
    if git_root is None:
        return "Error: not in a git repository"

    name = args.get("name")
    if name:
        name = str(name)
        err = _validate_slug(name)
        if err:
            return f"Error: invalid name: {err}"
    else:
        name = _random_name()

    worktree_dir = os.path.join(git_root, ".claude", "worktrees", name)
    branch_name = f"claude/{name}"

    try:
        result = subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, worktree_dir],
            capture_output=True, text=True, cwd=git_root, timeout=30,
        )
        if result.returncode != 0:
            # Try without -b (branch might already exist)
            result2 = subprocess.run(
                ["git", "worktree", "add", "--detach", worktree_dir],
                capture_output=True, text=True, cwd=git_root, timeout=30,
            )
            if result2.returncode != 0:
                return f"Error: git worktree add failed: {result2.stderr.strip()}"
    except FileNotFoundError:
        return "Error: git not found"

    os.chdir(worktree_dir)
    _set_session_cwd(worktree_dir)
    _WORKTREE_SESSION = {
        "originalCwd": cwd,
        "worktreePath": worktree_dir,
        "worktreeBranch": branch_name,
    }

    return (
        f"Created worktree at {worktree_dir} on branch {branch_name}.\n"
        f"Working directory is now {worktree_dir}.\n"
        "Use ExitWorktree to leave this worktree."
    )


def _exit_worktree_call(args: dict) -> str:
    global _WORKTREE_SESSION

    if _WORKTREE_SESSION is None:
        return "Not in a worktree session."

    action = str(args.get("action", "keep"))
    if action not in ("keep", "remove"):
        return "Error: action must be 'keep' or 'remove'"

    discard_changes = args.get("discard_changes", False)
    if isinstance(discard_changes, str):
        discard_changes = discard_changes.lower() in ("true", "1", "yes")

    worktree_path = _WORKTREE_SESSION["worktreePath"]
    original_cwd = _WORKTREE_SESSION["originalCwd"]
    branch = _WORKTREE_SESSION.get("worktreeBranch")

    if action == "remove":
        if not discard_changes:
            # Check for uncommitted changes
            try:
                result = subprocess.run(
                    ["git", "status", "--porcelain"],
                    capture_output=True, text=True, cwd=worktree_path, timeout=10,
                )
                if result.stdout.strip():
                    return (
                        "Error: worktree has uncommitted changes. Pass discard_changes=true to force remove.\n"
                        f"Changes:\n{result.stdout[:500]}"
                    )
            except Exception:
                pass

        # Remove worktree
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", worktree_path],
                capture_output=True, text=True, cwd=original_cwd, timeout=30,
                check=True,
            )
            if branch:
                subprocess.run(
                    ["git", "branch", "-D", branch],
                    capture_output=True, text=True, cwd=original_cwd, timeout=10,
                )
        except subprocess.CalledProcessError:
            pass  # Best effort cleanup
        except FileNotFoundError:
            pass

    os.chdir(original_cwd)
    _set_session_cwd(original_cwd)
    _WORKTREE_SESSION = None

    if action == "remove":
        return f"Removed worktree at {worktree_path}. Working directory restored to {original_cwd}."
    else:
        return f"Left worktree session. Working directory restored to {original_cwd}. Worktree at {worktree_path} preserved."


def register(registry: ToolRegistry) -> None:
    registry.register(Tool(
        name="EnterWorktree",
        searchHint="create isolated git worktree branch",
        description="Create an isolated git worktree and switch into it",
        prompt=ENTER_WORKTREE_PROMPT,
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Optional name for the worktree. Random if not provided.",
                },
                "path": {
                    "type": "string",
                    "description": "Path to an existing worktree to enter instead of creating one.",
                },
            },
            "required": [],
        },
        call=_enter_worktree_call,
        is_read_only=False,
    ))

    registry.register(Tool(
        name="ExitWorktree",
        searchHint="leave exit worktree session",
        description="Exit a worktree session and return to the original directory",
        prompt=EXIT_WORKTREE_PROMPT,
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["keep", "remove"],
                    "description": "\"keep\" leaves the worktree on disk; \"remove\" deletes both worktree and branch.",
                },
                "discard_changes": {
                    "type": "boolean",
                    "description": "Set to true to force removal when the worktree has uncommitted changes.",
                },
            },
            "required": ["action"],
        },
        call=_exit_worktree_call,
        is_read_only=False,
    ))
