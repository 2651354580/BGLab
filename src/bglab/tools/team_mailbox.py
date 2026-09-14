""

from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from bglab.owned_path import (
    normalize_owned_component,
    open_owned_regular,
    owned_child,
)

TEAM_LEAD_NAME = "leader"
TEAMS_DIR = Path.home() / ".bglab" / "teams"


_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


def _process_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(os.path.abspath(str(path)))
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _lock_file(fd: int):
    try:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
    except ImportError:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock_file(fd: int):
    try:
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except ImportError:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


def sanitize_component(name: str) -> str:
    return normalize_owned_component(name)


def get_inbox_path(agent_name: str, team_name: str) -> str:
    team_dir = owned_child(TEAMS_DIR, team_name)
    inbox_dir = owned_child(team_dir, "inboxes")
    inbox_dir.mkdir(parents=True, exist_ok=True)
    return str(owned_child(inbox_dir, f"{sanitize_component(agent_name)}.json"))


def _lock_path(inbox_path: Path) -> Path:
    return owned_child(
        inbox_path.parent,
        f".{inbox_path.name}.lock",
        max_length=255,
    )


@contextmanager
def _locked_inbox(agent_name: str, team_name: str):
    inbox_path = Path(get_inbox_path(agent_name, team_name))
    lock_path = _lock_path(inbox_path)
    process_lock = _process_lock(lock_path)
    with process_lock:
        lock_fd = open_owned_regular(lock_path, create=True)
        try:
            if os.fstat(lock_fd).st_size < 1:
                os.lseek(lock_fd, 0, os.SEEK_SET)
                os.write(lock_fd, b"\0")
                os.fsync(lock_fd)
            _lock_file(lock_fd)
            yield inbox_path
        finally:
            try:
                _unlock_file(lock_fd)
            finally:
                os.close(lock_fd)


def _load_messages(path: Path) -> list[dict]:
    fd = open_owned_regular(path, read_only=True)
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            loaded = json.load(handle)
    finally:
        if fd >= 0:
            os.close(fd)
    return loaded if isinstance(loaded, list) else []


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_messages(path: Path, messages: list[dict]) -> None:
    temporary = owned_child(
        path.parent,
        f".{path.name}.{uuid.uuid4().hex}.tmp",
        max_length=255,
    )
    fd = open_owned_regular(temporary, create=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = -1
            json.dump(messages, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_parent(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def read_mailbox(agent_name: str, team_name: str) -> list[dict]:
    with _locked_inbox(agent_name, team_name) as path:
        try:
            return _load_messages(path)
        except (FileNotFoundError, json.JSONDecodeError, IOError, PermissionError):
            return []


def read_unread(agent_name: str, team_name: str) -> list[dict]:
    return [m for m in read_mailbox(agent_name, team_name) if not m.get("read")]


def read_first_unread(agent_name: str, team_name: str) -> tuple[dict | None, int]:
    """Read FIRST unread message with its index. Returns (msg, index) or (None, -1)."""
    msgs = read_mailbox(agent_name, team_name)
    for i, m in enumerate(msgs):
        if not m.get("read"):
            return m, i
    return None, -1


def write_message(
    recipient: str, message: dict, team_name: str,
    *, sender: str = "", summary: str = "",
) -> None:
    text_content = json.dumps(message, ensure_ascii=False)
    new_msg = {
        "from": sender,
        "text": text_content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "read": False,
    }
    if summary:
        new_msg["summary"] = summary

    with _locked_inbox(recipient, team_name) as path:
        try:
            messages = _load_messages(path)
        except FileNotFoundError:
            messages = []
        except json.JSONDecodeError:
            messages = []
        messages.append(new_msg)
        _atomic_write_messages(path, messages)


def mark_read(agent_name: str, team_name: str) -> None:
    with _locked_inbox(agent_name, team_name) as path:
        try:
            messages = _load_messages(path)
        except (FileNotFoundError, json.JSONDecodeError, IOError, PermissionError):
            return
        for message in messages:
            message["read"] = True
        _atomic_write_messages(path, messages)


def mark_one_read(agent_name: str, team_name: str, index: int) -> None:
    """Mark a single message (by index) as read."""
    with _locked_inbox(agent_name, team_name) as path:
        try:
            messages = _load_messages(path)
        except (FileNotFoundError, json.JSONDecodeError, IOError, PermissionError):
            return
        if 0 <= index < len(messages):
            messages[index]["read"] = True
        _atomic_write_messages(path, messages)


def is_shutdown(msg: dict) -> bool:
    try:
        return json.loads(msg.get("text", "{}")).get("type") == "shutdown_request"
    except Exception:
        return False
