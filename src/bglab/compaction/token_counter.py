"""Legacy character estimate for history shaping; not Provider token usage.

The four-characters-per-token heuristic can underestimate Chinese text.
System prompts, request heads and Tool schemas are not supplied here, and
content-only counting omits Tool arguments and nested Tool result payloads.
"""

from __future__ import annotations

import json
import math


def estimate_request_tokens(messages, *, system_prompt, tools, model, thinking_effort=None) -> int:
    """Preflight estimate of the full wire representation, never billed usage.

    Include native reasoning when replayed, Tool arguments/results, System and
    advertised schemas. Non-ASCII text receives a larger reserve than ASCII.
    This heuristic plus headroom cannot prove arbitrary provider admission.
    The legacy history estimator below remains shaping telemetry only.
    """
    from bglab.llm.client import _to_openai_messages, _to_anthropic_messages
    from bglab.llm.providers import resolve_model
    try:
        resolved = resolve_model(model)
        spec = resolved.model
    except ValueError:
        resolved = spec = None
    if spec is not None and spec.protocol == "anthropic-messages":
        wire = {"system": system_prompt, "messages": _to_anthropic_messages(messages),
                "tools": [tool.to_anthropic_schema() for tool in tools or ()]}
    else:
        wire = {"messages": [{"role": "system", "content": system_prompt},
                *_to_openai_messages(messages, preserve_reasoning_content=bool(
                    spec and spec.tool_reasoning_roundtrip and thinking_effort != "none"),
                    require_reasoning_content=bool(tools and resolved and resolved.provider.id == "deepseek"))],
                "tools": [tool.to_openai_schema() for tool in tools or ()]}
    encoded = json.dumps(wire, ensure_ascii=False, separators=(",", ":"))
    ascii_chars = sum(ord(char) < 128 for char in encoded)
    return math.ceil(ascii_chars / 4 + (len(encoded) - ascii_chars) * 2) + 8 * len(messages)


def estimate_tokens(messages: list[dict]) -> int:
    """估算 messages 的总 token 数。

    4 chars ≈ 1 token is an approximation, not a conservative bound.
    system prompt 和 API overhead 不计算在内。
    """
    total = 0
    for msg in messages:
        # Serialize the message content to string for estimation
        total += _estimate_message_tokens(msg)
    return total


def _estimate_message_tokens(msg: dict) -> int:
    """单条 message 的 token 估算。"""
    content = msg.get("content", "")

    if isinstance(content, str):
        return len(content) // 4

    if isinstance(content, list):
        # Content blocks: [{type: "text", text: "..."}, ...]
        total = 0
        for block in content:
            if isinstance(block, dict):
                text = block.get("text", "")
                if isinstance(text, str):
                    total += len(text) // 4
        return total

    # Fallback: serialize as JSON
    try:
        return len(json.dumps(content)) // 4
    except Exception:
        return 0
