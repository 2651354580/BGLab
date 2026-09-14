"""Transcript 持久化 — JSONL 格式，对齐 sessionStorage.ts。

格式: 每行一个 JSON object，包含 message + metadata。
路径: ~/.bglab/transcripts/<sanitized_cwd>/<session_id>.jsonl
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from datetime import datetime

def _persist_dir(cwd: str | None = None) -> Path:
    """获取 transcript 存储目录。"""
    home = Path.home() / ".bglab" / "transcripts"
    if cwd:
        sanitized = _sanitize_path(cwd)
        home = home / sanitized
    home.mkdir(parents=True, exist_ok=True)
    return home


def _sanitize_path(path: str) -> str:
    """清理路径为合法目录名。对齐 sessionStoragePortable.ts sanitizePath()。"""
    import re
    import hashlib
    cleaned = re.sub(r'[^a-zA-Z0-9_\-]', '-', path)
    cleaned = re.sub(r'-{2,}', '-', cleaned)
    cleaned = cleaned.strip('-')
    if len(cleaned) > 100:
        h = hashlib.sha256(path.encode()).hexdigest()[:8]
        cleaned = cleaned[:91] + "-" + h
    return cleaned or "root"


class TranscriptWriter:
    """写入 transcript JSONL 文件。"""

    def __init__(self, cwd: str | None = None, session_id: str | None = None):
        self._cwd = cwd
        self.session_id = session_id or str(uuid.uuid4())
        self._dir = _persist_dir(cwd)
        self._path = self._dir / f"{self.session_id}.jsonl"
        self._written_count = 0
        self._truncate = True  # first write overwrites, subsequent appends

    @property
    def path(self) -> str:
        return str(self._path)

    def _write_line(self, entry: dict) -> None:
        """Write a single JSON line to the transcript file."""
        mode = "w" if self._truncate else "a"
        self._truncate = False
        with open(self._path, mode, encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._written_count += 1

    def write_message(self, msg: dict) -> None:
        """写入一条 message 到 JSONL。保存完整消息结构。"""
        role = msg.get("role", "")
        if not role:
            # Infer role from message type
            if msg.get("type") == "tool_result":
                role = "user"
            else:
                role = "unknown"
        entry = {
            "session_id": self.session_id,
            "timestamp": (
                datetime.fromtimestamp(float(msg["_timestamp"])).isoformat()
                if isinstance(msg.get("_timestamp"), (int, float))
                else datetime.now().isoformat()
            ),
            "role": role,
            "content": msg.get("content", ""),
            "_is_meta": msg.get("_is_meta", False),
        }
        # 保留 compact boundary 等元数据
        if msg.get("_compact_boundary"):
            entry["_compact_boundary"] = msg["_compact_boundary"]
        if msg.get("_authoritative_decision_frame") is True:
            entry["_authoritative_decision_frame"] = True
        if "_turn_input" in msg:
            entry["_turn_input"] = msg["_turn_input"]
        # 保留 tool_result 专用字段
        if msg.get("type") == "tool_result":
            entry["type"] = "tool_result"
            entry["tool_use_id"] = msg.get("tool_use_id", "")
            entry["is_error"] = bool(msg.get("is_error", False))
        # 记录工具使用详情
        if role == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                tool_uses = [b.get("name") for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
                if tool_uses:
                    entry["tool_uses"] = tool_uses
                    entry["text"] = " ".join(
                        b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
                    )

        self._write_line(entry)

    def write_messages(self, messages: list[dict]) -> None:
        """写入多条 messages。"""
        for msg in messages:
            self.write_message(msg)

    def close(self, *, raise_errors: bool = False) -> None:
        """写入 session metadata 并关闭。"""
        meta = {
            "session_id": self.session_id,
            "type": "session_end",
            "message_count": self._written_count,
            "timestamp": datetime.now().isoformat(),
        }
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        except Exception:
            if raise_errors:
                raise


class TranscriptReader:
    """读取 transcript JSONL 文件。"""

    def __init__(self, path: str):
        self._path = Path(path)
        if not self._path.exists():
            raise FileNotFoundError(f"Transcript not found: {path}")
        self._messages: list[dict] = []
        self._metadata: dict = {}

    def read(self) -> list[dict]:
        """读取所有 messages, 过滤 metadata entries。"""
        messages = []
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "session_end":
                    self._metadata = entry
                    continue
                if entry.get("type") == "session_meta":
                    self._session_meta = entry
                    continue
                messages.append(_entry_to_message(entry))
        self._messages = messages
        return messages

    @property
    def message_count(self) -> int:
        return len(self._messages)

    @property
    def metadata(self) -> dict:
        return self._metadata


def _entry_to_message(entry: dict) -> dict:
    """从 JSONL entry 重建 message dict。完整回建所有持久化字段。"""
    content = entry.get("content", "")
    is_tool_result = entry.get("type") == "tool_result"
    if not is_tool_result and not isinstance(content, list):
        content = [{"type": "text", "text": str(content)}]

    msg: dict = {
        "role": entry.get("role", "user"),
        "content": content,
    }
    timestamp = entry.get("timestamp")
    if isinstance(timestamp, str):
        try:
            msg["_timestamp"] = datetime.fromisoformat(timestamp).timestamp()
        except ValueError:
            pass
    if entry.get("_is_meta"):
        msg["_is_meta"] = True
    if entry.get("_compact_boundary"):
        msg["_compact_boundary"] = entry["_compact_boundary"]
    if entry.get("_authoritative_decision_frame") is True:
        msg["_authoritative_decision_frame"] = True
    if "_turn_input" in entry:
        msg["_turn_input"] = entry["_turn_input"]
    if entry.get("tool_uses"):
        msg["_tool_uses"] = entry["tool_uses"]
    if entry.get("text"):
        msg["_text"] = entry["text"]
    # Reconstruct tool_result messages
    if is_tool_result:
        msg["type"] = "tool_result"
        msg["tool_use_id"] = entry.get("tool_use_id", "")
        msg["is_error"] = bool(entry.get("is_error", False))
    return msg


# ── 便捷函数 ──

def save_transcript(messages: list[dict], cwd: str | None = None, session_id: str | None = None,
                    session_meta: dict | None = None) -> str:
    """保存 messages 到 transcript 并返回 session_id。

    session_meta: {model, tools[], permission_mode, cwd, ...} 存为首行元数据。
    """
    writer = TranscriptWriter(cwd=cwd, session_id=session_id)
    destination = Path(writer.path)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent),
    )
    os.close(fd)
    writer._path = Path(temporary)
    try:
        # Keep the existing JSONL encoder, metadata and append API. Only the
        # completed full-history file may replace the last confirmed save.
        if session_meta:
            meta_entry = {
                "type": "session_meta",
                "model": session_meta.get("model", ""),
                "tools": session_meta.get("tools", []),
                "permission_mode": session_meta.get("permission_mode", "default"),
                "cwd": session_meta.get("cwd", ""),
                "session_id": writer.session_id,
                "timestamp": datetime.now().isoformat(),
            }
            for key in ("mode", "game_id", "pid"):
                if key in session_meta:
                    meta_entry[key] = session_meta[key]
            writer._write_line(meta_entry)
        writer.write_messages(messages)
        writer.close(raise_errors=True)
        # One durability flush for the completed file, never one per message.
        with open(temporary, "a", encoding="utf-8") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return writer.session_id


def load_transcript(path: str) -> list[dict]:
    """从 JSONL 文件加载 messages。"""
    reader = TranscriptReader(path)
    return reader.read()


def find_last_compact_boundary(messages: list[dict], *, with_preserved_segment: bool = False) -> int:
    """Return index of last compact boundary marker, or -1 if none.
    If with_preserved_segment=True, only match boundaries that carry
    a preserved_segment (skip stale manual/reactive compacts without one).
    Aligns with findLastCompactBoundaryIndex() + segIsLive gate."""
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if isinstance(msg, dict):
            cb = msg.get("_compact_boundary")
            if isinstance(cb, dict):
                if with_preserved_segment and not cb.get("preserved_segment"):
                    continue
                return i
    return -1


def get_relink_metadata(messages: list[dict]) -> dict | None:
    """Extract relink metadata from the last compact boundary with preserved_segment.
    Returns dict with 5 relink items or None if no live boundary."""
    idx = find_last_compact_boundary(messages, with_preserved_segment=True)
    if idx < 0:
        return None
    boundary = messages[idx].get("_compact_boundary", {})
    return boundary.get("relink", None)


def load_messages_from_boundary(path: str) -> tuple[list[dict], dict | None]:
    """Load transcript and slice from last compact boundary with preserved_segment.
    Returns (messages_from_boundary, relink_metadata).
    Aligns with getMessagesAfterCompactBoundary() used on resume."""
    messages = load_transcript(path)
    idx = find_last_compact_boundary(messages, with_preserved_segment=True)
    if idx >= 0:
        boundary = messages[idx].get("_compact_boundary", {})
        result = messages[idx:]
        try:
            from bglab.engine.trace import transcript_load as _tl
            _tl(path, len(result), has_boundary=True, has_relink=bool(boundary.get("relink")))
        except Exception:
            pass
        return result, boundary.get("relink", None)
    try:
        from bglab.engine.trace import transcript_load as _tl
        _tl(path, len(messages), has_boundary=False)
    except Exception:
        pass
    return messages, None


def list_sessions(cwd: str | None = None) -> list[dict]:
    """列出所有 session。

    Returns:
        [{session_id: str, path: str, message_count: int, timestamp: str}, ...]
    """
    d = _persist_dir(cwd)
    sessions = []
    if not d.exists():
        return sessions
    for f in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            stat = f.stat()
            sessions.append({
                "session_id": f.stem,
                "path": str(f),
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        except Exception:
            continue
    return sessions


def get_session_previews(cwd: str | None = None, limit: int = 100, *, include_game: bool = True) -> list[dict]:
    """列出 session 并读取首条用户消息作为标题，供 UI 选择器使用。

    Returns:
        [{session_id, path, size, modified, title, message_count, model, tools}, ...]
        title 取自首条 role=user 的消息内容（截断至 80 字符）
    """
    sessions = list_sessions(cwd)
    previews: list[dict] = []
    for s in sessions:
        if len(previews) >= limit:
            break
        # Legacy player transcripts predate persisted mode metadata.
        is_game = s["session_id"].startswith("game-") and "-p" in s["session_id"]
        title = ""
        msg_count = 0
        model = ""
        tools: list[str] = []
        try:
            with open(s["path"], encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") == "session_meta":
                        model = entry.get("model", "")
                        tools = entry.get("tools", [])
                        is_game = is_game or entry.get("mode") == "game" or bool(entry.get("game_id"))
                        continue
                    if entry.get("type") == "session_end":
                        msg_count = entry.get("message_count", 0)
                        continue
                    msg_count += 1
                    if not title and entry.get("role") == "user" and not entry.get("_is_meta"):
                        content = entry.get("content", "")
                        if isinstance(content, list):
                            text_parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
                            title = " ".join(text_parts)[:80]
                        elif isinstance(content, str):
                            title = content[:80]
        except Exception:
            pass
        if is_game and not include_game:
            continue
        previews.append({
            "session_id": s["session_id"],
            "path": s["path"],
            "size": s["size"],
            "modified": s["modified"],
            "title": title or s["session_id"][:8],
            "message_count": msg_count,
            "model": model,
            "tools": tools,
        })
    return previews
