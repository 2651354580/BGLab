"""持久化 — JSONL transcript 存储 + Resume。

对齐 sessionStorage.ts 的 JSONL 格式:
  - 每行一个 JSON object
  - 文件存储在 ~/.bglab/transcripts/<sanitized_cwd>/<session_id>.jsonl
  - --resume 恢复最近 session
"""

from bglab.persistence.transcript import (
    TranscriptWriter,
    TranscriptReader,
    save_transcript,
    load_transcript,
    list_sessions,
    get_session_previews,
    find_last_compact_boundary,
    get_relink_metadata,
    load_messages_from_boundary,
)

__all__ = [
    "TranscriptWriter",
    "TranscriptReader",
    "save_transcript",
    "load_transcript",
    "list_sessions",
    "get_session_previews",
    "find_last_compact_boundary",
    "get_relink_metadata",
    "load_messages_from_boundary",
]
