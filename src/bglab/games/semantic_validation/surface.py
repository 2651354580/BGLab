from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping


def _canonical_bytes(value: Any) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class SemanticSurfaceIdentity:
    input_version: str
    prompt_hash: str
    frame_hash: str
    tool_description_hash: str
    schema_hash: str
    request_context_hash: str
    composite_hash: str

    def to_dict(self) -> dict[str, str]:
        return {
            "inputVersion": self.input_version,
            "promptHash": self.prompt_hash,
            "frameHash": self.frame_hash,
            "toolDescriptionHash": self.tool_description_hash,
            "schemaHash": self.schema_hash,
            "requestContextHash": self.request_context_hash,
            "compositeHash": self.composite_hash,
        }


def freeze_semantic_surface(
    *,
    input_version: str,
    prompt: str,
    frame: Mapping[str, Any],
    tool_description: str,
    schema: Mapping[str, Any],
    request_context: Mapping[str, Any] | None = None,
) -> SemanticSurfaceIdentity:
    for value, field in (
        (input_version, "input_version"),
        (prompt, "prompt"),
        (tool_description, "tool_description"),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{field} must be non-empty text")
    if not isinstance(frame, Mapping):
        raise ValueError("frame must be an object")
    if not isinstance(schema, Mapping):
        raise ValueError("schema must be an object")
    if request_context is not None and not isinstance(request_context, Mapping):
        raise ValueError("request_context must be an object")
    parts = {
        "inputVersion": input_version,
        "promptHash": _sha256(prompt),
        "frameHash": _sha256(dict(frame)),
        "toolDescriptionHash": _sha256(tool_description),
        "schemaHash": _sha256(dict(schema)),
        "requestContextHash": _sha256(dict(request_context or {})),
    }
    return SemanticSurfaceIdentity(
        input_version=input_version,
        prompt_hash=parts["promptHash"],
        frame_hash=parts["frameHash"],
        tool_description_hash=parts["toolDescriptionHash"],
        schema_hash=parts["schemaHash"],
        request_context_hash=parts["requestContextHash"],
        composite_hash=_sha256(parts),
    )


__all__ = ["SemanticSurfaceIdentity", "freeze_semantic_surface"]
