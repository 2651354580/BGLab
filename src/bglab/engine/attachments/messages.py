"""Transcript rendering for typed attachment values."""

from __future__ import annotations

from typing import Any

from bglab.engine.attachments.types import AttachmentValue


def attachment_to_message(value: AttachmentValue) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "user",
        "type": "attachment",
        "attachment_type": value.kind,
        "content": [{"type": "text", "text": value.text}],
        "_is_meta": True,
    }
    if value.attachment_id:
        message["_attachment_id"] = value.attachment_id
    if value.metadata:
        message["_attachment_metadata"] = dict(value.metadata)
        memories = value.metadata.get("memories")
        if isinstance(memories, list):
            message["_memories"] = memories
    return message


def attachment_to_head_message(value: AttachmentValue) -> dict[str, Any]:
    ""
    return {
        "role": "user",
        "content": [{"type": "text", "text": value.text}],
        "_is_meta": True,
        "_attachment_kind": value.kind,
    }
