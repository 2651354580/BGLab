"""Provider-aware LLM client for OpenAI and Anthropic compatible APIs.

- 流式输出（text + tool_use delta）
- 工具调用
- 错误处理 & 重试
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import inspect
import json
import logging
import os
import re
import time
from importlib.metadata import PackageNotFoundError, version
from typing import AsyncGenerator, Callable
from uuid import uuid4

import httpx
from openai import AsyncOpenAI

from bglab.llm import credentials
from bglab.llm.usage import ProviderAttemptUsage, with_attempt_usage
from bglab.llm.provider_slots import ProviderSlot
from bglab.llm.providers import (
    PROVIDERS,
    ModelSpec,
    ResolvedModel,
    resolve_model,
)
from bglab.llm.retry import ProviderCompletionError, classify_provider_error, retry_delay_seconds
from bglab.engine.error_policy import PROVIDER_REQUEST_FAILED

_PROVIDER_PROGRESS = object()
_STREAM_CLOSE_TIMEOUT_SECONDS = 0.1
_REASONING_REPEAT_MIN_CHARS = 3_000
_REASONING_REPEAT_WINDOW_CHARS = 4_096
_REASONING_REPEAT_SCAN_STRIDE = 256


def _reasoning_repetition_detected(text: str) -> bool:
    """Probe a bounded periodic tail in an already identified private channel."""
    if len(text) < _REASONING_REPEAT_MIN_CHARS:
        return False
    tail = " ".join(text[-_REASONING_REPEAT_WINDOW_CHARS:].casefold().split())[::-1]
    size = len(tail)
    z = [0] * size
    left = right = 0
    for index in range(1, size):
        if index <= right:
            z[index] = min(right - index + 1, z[index - left])
        while index + z[index] < size and tail[z[index]] == tail[index + z[index]]:
            z[index] += 1
        if index + z[index] - 1 > right:
            left, right = index, index + z[index] - 1
    return any(
        z[period] >= 2 * period and z[period] + period >= _REASONING_REPEAT_MIN_CHARS
        for period in range(1, size // 3 + 1)
    )


class _StartingThinkBlock:
    """One reserved initial block; suffix is opaque, pending tags stay bounded."""

    def __init__(self, *, recognize_private: bool = True):
        self.state = "opening" if recognize_private else "public"
        self.tag = ""
        self.position = 0
        self.recognized = False
        self.closed_at: int | None = None
        self.public_progress = False

    @property
    def private_open(self) -> bool:
        return self.state in {"private", "malformed"}

    def feed(self, text: str) -> None:
        for char in text:
            self.position += 1
            folded = char.casefold()
            if self.state == "opening":
                if not self.tag and char.isspace():
                    continue
                if self.tag == "<think":
                    if char.isspace():
                        continue
                    if char == ">":
                        self.state, self.tag, self.recognized = "private", "", True
                        continue
                elif "<think".startswith(self.tag + folded):
                    self.tag += folded
                    continue
                self.public_progress = bool(self.tag) or not char.isspace()
                self.state, self.tag = "public", ""
            elif self.state == "private":
                if self.tag in {"<think", "</think"}:
                    if char.isspace():
                        continue
                    if char == ">":
                        if self.tag == "<think":
                            self.state = "malformed"
                        else:
                            self.state, self.closed_at = "public", self.position
                        self.tag = ""
                        continue
                proposed = self.tag + folded
                if self.tag and any(token.startswith(proposed) for token in ("<think", "</think")):
                    self.tag = proposed
                else:
                    self.tag = "<" if char == "<" else ""
            elif self.state == "public":
                self.public_progress |= not char.isspace()


async def _opencode_go_model_first_request(request: httpx.Request) -> None:
    """Work around the Go gateway's order-sensitive model parser.

    JSON object order is semantically irrelevant, but the official Go chat
    endpoint has returned ``Model  is not supported`` when OpenAI Python 2.32
    serializes ``messages`` before ``model``. Keep every value unchanged and
    move only the top-level model field to the front for this endpoint.
    """
    if not (
        request.method == "POST"
        and request.url.host == "opencode.ai"
        and request.url.path.endswith("/zen/go/v1/chat/completions")
    ):
        return
    original = await request.aread()
    try:
        payload = json.loads(original)
    except (TypeError, ValueError, UnicodeDecodeError):
        return
    if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
        return
    reordered = {
        "model": payload["model"],
        **{key: value for key, value in payload.items() if key != "model"},
    }
    encoded = json.dumps(
        reordered,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request.stream = httpx.ByteStream(encoded)
    request._content = encoded  # type: ignore[attr-defined]
    request.headers["content-length"] = str(len(encoded))


def _official_opencode_go_http_client(
    resolved: ResolvedModel,
    base_url: str,
) -> httpx.AsyncClient | None:
    if resolved.provider.id != "opencode-go":
        return None
    endpoint = httpx.URL(base_url)
    if not (
        endpoint.host == "opencode.ai"
        and endpoint.path.rstrip("/") == "/zen/go/v1"
    ):
        return None
    return httpx.AsyncClient(
        timeout=130.0,
        event_hooks={"request": [_opencode_go_model_first_request]},
    )


def load_env():
    """Auto-load .env, including the main repo behind a linked worktree."""
    env_file = credentials.find_secret_env()
    if env_file is None:
        return

    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            key = key.strip()
            if key and not os.environ.get(key):
                os.environ[key] = value.strip().strip("'\"")

class FallbackTriggeredError(Exception):
    """对齐 withRetry.ts FallbackTriggeredError — 触发模型回退的信号。"""
    def __init__(self, original_model: str, fallback_model: str):
        self.original_model = original_model
        self.fallback_model = fallback_model
        super().__init__(f"Model fallback triggered: {original_model} -> {fallback_model}")


from bglab.llm.types import (
    CompletionObservation,
    LoopEvent,
    LoopEventType,
    ProviderFailureInfo,
    StopReason,
    ToolDefinition,
    ToolUseBlock,
)


def _is_opencode_v4_nonthinking(
    resolved: ResolvedModel,
    *,
    disable_thinking: bool,
) -> bool:
    """Return whether this request uses Go's V4 non-thinking wire contract."""

    return bool(
        disable_thinking
        and resolved.provider.id == "opencode-go"
        and resolved.model.id.startswith("deepseek-v4-")
    )


def _split_misplaced_visible_deliberation(
    text: str,
) -> tuple[str, str] | None:
    """Recognize one initial think block; neither words nor Tools imply privacy."""
    prefix = _StartingThinkBlock()
    prefix.feed(text)
    if not prefix.recognized:
        return None
    if prefix.closed_at is None:
        return "", text
    return text[prefix.closed_at:], text[:prefix.closed_at]


def _reclassify_atomic_provider_content(
    events: list[LoopEvent],
    *,
    resolved: ResolvedModel,
    disable_thinking: bool,
) -> list[LoopEvent]:
    """Normalize a completed attempt or a locally ended output failure.

    The raw draft is carried only on the current DONE event as private
    diagnostic reasoning.  The shared QueryLoop deliberately does not persist
    content marked by the accompanying usage counter, so it cannot pollute the
    next request or the transcript's visible assistant text.
    """

    if not _is_opencode_v4_nonthinking(
        resolved,
        disable_thinking=disable_thinking,
    ):
        return events
    has_tool_call = any(
        event.type == LoopEventType.TOOL_USE_END and event.tool_use is not None
        for event in events
    )

    visible_text = "".join(
        event.text or ""
        for event in events
        if event.type == LoopEventType.TEXT
    )
    split = _split_misplaced_visible_deliberation(visible_text)
    if split is None:
        return events
    public_text, private_draft = split

    done_event = next(
        (event for event in reversed(events) if event.type == LoopEventType.DONE),
        None,
    )
    if done_event is None:
        return events

    prior_reasoning = str(done_event.reasoning_content or "")
    done_event.reasoning_content = (
        f"{prior_reasoning}\n{private_draft}".lstrip("\n")
        if prior_reasoning
        else private_draft
    )
    usage = dict(done_event.usage or {})
    usage.update({
        "provider_visible_deliberation_reclassified": 1,
        "provider_visible_deliberation_chars": len(private_draft),
        "provider_public_text_chars": len(public_text),
    })
    done_event.usage = usage
    if not has_tool_call and not public_text.strip():
        # A response containing only the leaked draft did not complete the
        # requested turn. Preserve ordinary QueryLoop recovery semantics after
        # moving the private draft out of the visible channel.
        done_event.stop_reason = StopReason.MAX_TOKENS

    normalized: list[LoopEvent] = []
    inserted_public = False
    for event in events:
        if event.type != LoopEventType.TEXT:
            normalized.append(event)
            continue
        if not inserted_public and public_text:
            normalized.append(LoopEvent(type=LoopEventType.TEXT, text=public_text))
            inserted_public = True
    return normalized


def _resolve_request_model(
    model: str,
    *,
    provider_slot: ProviderSlot | None = None,
) -> ResolvedModel:
    """Resolve a request using an optional immutable Provider slot identity.

    A configured slot is authoritative for provider/model selection.  Catalog
    models retain their declared protocol and thinking policy; manually entered
    model IDs use the selected Provider's OpenAI-compatible default protocol.
    """
    if provider_slot is None:
        return resolve_model(model)

    requested = (model or "").strip()
    if requested != provider_slot.reference:
        raise ValueError(
            "model reference does not match provider slot: "
            f"{requested!r} != {provider_slot.reference!r}",
        )

    provider = next(
        (candidate for candidate in PROVIDERS if candidate.id == provider_slot.provider),
        None,
    )
    if provider is None:  # ProviderSlot validation should make this unreachable.
        raise ValueError(f"Unknown provider '{provider_slot.provider}'")
    selected_model = provider.get_model(provider_slot.model)
    if selected_model is None:
        selected_model = ModelSpec(
            id=provider_slot.model,
            display_name=provider_slot.model,
            protocol="openai-chat",
        )
    return ResolvedModel(
        reference=provider_slot.reference,
        provider=provider,
        model=selected_model,
    )


def _build_client(
    model: str = "deepseek-chat",
    *,
    provider_slot: ProviderSlot | None = None,
    request_session_id: str | None = None,
):
    load_env()
    resolved = _resolve_request_model(model, provider_slot=provider_slot)
    credential_env = (
        provider_slot.credential_env
        if provider_slot is not None
        else resolved.provider.api_key_env
    )
    base_url = (
        provider_slot.base_url
        if provider_slot is not None
        else resolved.provider.openai_base_url
    )
    api_key = os.environ.get(credential_env)
    if not api_key:
        raise RuntimeError(
            f"{credential_env} environment variable not set "
            f"for {resolved.provider.display_name}"
        )

    if resolved.model.protocol == "openai-chat":
        kwargs = {
            "api_key": api_key,
            "base_url": base_url,
            # Game mode applies its own first-event, idle, and hard deadlines.
            # Keep the transport ceiling above the longest (120s) game attempt.
            "timeout": 130.0,
            # call_model owns the visible retry policy. Disabling the SDK's
            # hidden retries prevents 3x3 compounded waits in a locked turn.
            "max_retries": 0,
        }
        http_client = _official_opencode_go_http_client(resolved, base_url)
        if http_client is not None:
            kwargs["http_client"] = http_client
            endpoint = httpx.URL(base_url)
            if endpoint.scheme == "https" and endpoint.port in (None, 443):
                try:
                    client_version = version("bglab")
                except PackageNotFoundError:
                    client_version = "source"
                session_id = (
                    request_session_id if request_session_id is not None else uuid4().hex
                )
                # Session labels and paths must not become raw header values.
                kwargs["default_headers"] = {
                    "User-Agent": f"BGLab/{client_version}",
                    "x-opencode-session": "bglab-" + hashlib.sha256(
                        session_id.encode("utf-8"),
                    ).hexdigest(),
                }
        return AsyncOpenAI(**kwargs)
    if resolved.model.protocol == "anthropic-messages":
        if not resolved.provider.anthropic_base_url:
            raise RuntimeError(
                f"Anthropic endpoint not configured for {resolved.provider.display_name}"
            )
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:  # pragma: no cover - packaging failure
            raise RuntimeError(
                "Anthropic protocol support requires the 'anthropic' package"
            ) from exc
        return AsyncAnthropic(
            api_key=api_key,
            base_url=(
                provider_slot.base_url
                if provider_slot is not None
                else resolved.provider.anthropic_base_url
            ),
            timeout=130.0,
            max_retries=0,
        )
    raise RuntimeError(f"Unsupported model protocol: {resolved.model.protocol}")


def _to_openai_messages(
    messages: list[dict],
    *,
    preserve_reasoning_content: bool = False,
    require_reasoning_content: bool = False,
) -> list[dict]:
    """Convert internal (Anthropic-format) messages to OpenAI API format.

    Internal format uses content blocks like:
      {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
      {"type": "tool_result", "tool_use_id": "...", "content": "..."}

    OpenAI format uses:
      tool_calls on assistant messages
      role: "tool" for tool results
    """
    result: list[dict] = []
    pending_tool_calls: set[str] = set()
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        msg_type = msg.get("type", "")

        # ── Tool result message (internal format: {"type": "tool_result", ...}) ──
        if msg_type == "tool_result" or (role == "" and msg_type == "tool_result"):
            tool_use_id = str(msg.get("tool_use_id", ""))
            if tool_use_id and tool_use_id in pending_tool_calls:
                result.append({
                    "role": "tool",
                    "tool_call_id": tool_use_id,
                    "content": str(msg.get("content", "")),
                })
                pending_tool_calls.discard(tool_use_id)
            else:
                result.append({
                    "role": "user",
                    "content": (
                        "[Recovered result from a compacted tool call]\n"
                        f"{msg.get('content', '')}"
                    ),
                })
            continue

        # ── Attachment messages → convert to <system-reminder> user message ──
        if msg_type == "attachment" or "attachment_type" in msg:
            reminder_text = ""
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        reminder_text += b.get("text", "")
            result.append({
                "role": "user",
                "content": f"<system-reminder>{reminder_text}</system-reminder>",
            })
            continue

        # ── Regular messages with role ──
        if not isinstance(content, list):
            if role == "assistant" and preserve_reasoning_content and require_reasoning_content:
                content = [{"type": "text", "text": str(content) if content else ""}]
            else:
                result.append({"role": role, "content": str(content) if content else ""})
                continue

        # ── Content blocks (Anthropic style) → OpenAI format ──
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[dict] = []

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")

            if btype == "text":
                text_parts.append(str(block.get("text", "")))
            elif btype == "reasoning" and preserve_reasoning_content:
                reasoning_parts.append(str(block.get("text", "")))
            elif btype == "tool_use":
                import json
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                })

        # DeepSeek V4 cannot replay an assistant tool-call message while
        # thinking is enabled unless the provider's original reasoning block
        # is present.  Do not invent that private reasoning and do not make
        # thinking stay disabled for the rest of the session.  Rebase the
        # incompatible exchange as explicit factual history instead; the
        # following orphan tool_result is already recovered as a user fact.
        if (
            (tool_calls or require_reasoning_content)
            and role == "assistant"
            and preserve_reasoning_content
            and not reasoning_parts
        ):
            recovered_parts = [
                "[Recovered assistant "
                + ("tool exchange" if tool_calls else "public response")
                + " without replayable "
                "provider reasoning; factual history only.]",
            ]
            recovered_parts.extend(text_parts)
            recovered_parts.extend(
                "Tool call "
                f"{tool_call['function']['name']}: "
                f"{tool_call['function']['arguments']}"
                for tool_call in tool_calls
            )
            result.append({
                "role": "user",
                "content": "\n".join(recovered_parts),
            })
            continue

        # Build OpenAI-format message
        openai_msg: dict = {"role": role}
        if tool_calls:
            openai_msg["tool_calls"] = tool_calls
            openai_msg["content"] = (
                "\n".join(text_parts)
                if text_parts
                else ("" if preserve_reasoning_content else None)
            )
            pending_tool_calls.update(
                str(tool_call["id"])
                for tool_call in tool_calls
                if tool_call.get("id")
            )
        else:
            openai_msg["content"] = "\n".join(text_parts) if text_parts else ""
        if reasoning_parts and role == "assistant":
            openai_msg["reasoning_content"] = "".join(reasoning_parts)

        result.append(openai_msg)

    return result


def _to_responses_input(messages: list[dict]) -> list[dict]:
    """Convert internal messages into stateless Responses API input items."""

    result: list[dict] = []
    for message in messages:
        role = str(message.get("role", ""))
        content = message.get("content", "")
        message_type = str(message.get("type", ""))
        if message_type == "tool_result":
            result.append({
                "type": "function_call_output",
                "call_id": str(message.get("tool_use_id", "")),
                "output": str(content),
            })
            continue

        blocks = content if isinstance(content, list) else [
            {"type": "text", "text": str(content)}
        ]
        text_parts: list[str] = []
        tool_uses: list[dict] = []
        tool_results: list[dict] = []
        for block in blocks:
            if not isinstance(block, dict):
                text_parts.append(str(block))
                continue
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(str(block.get("text", "")))
            elif block_type == "tool_use":
                tool_uses.append({
                    "type": "function_call",
                    "call_id": str(block.get("id", "")),
                    "name": str(block.get("name", "")),
                    "arguments": json.dumps(
                        block.get("input", {}),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                })
            elif block_type == "tool_result":
                tool_results.append({
                    "type": "function_call_output",
                    "call_id": str(block.get("tool_use_id", "")),
                    "output": str(block.get("content", "")),
                })
            # Reasoning blocks belong to the old provider wire.  The current
            # Responses route is selected only for explicit non-thinking calls.

        text = "\n".join(part for part in text_parts if part)
        if text:
            response_role = "assistant" if role == "assistant" else "user"
            part_type = "output_text" if response_role == "assistant" else "input_text"
            result.append({
                "type": "message",
                "role": response_role,
                "content": [{"type": part_type, "text": text}],
            })
        result.extend(tool_uses)
        result.extend(tool_results)
    return result


def _thinking_extra_body(
    resolved: ResolvedModel,
    messages: list[dict],
    *,
    has_tools: bool,
    force_disabled: bool = False,
    thinking_effort: str | None = None,
) -> dict | None:
    """Select a replay-safe DeepSeek V4 thinking mode for this request."""
    opencode_v4 = (
        resolved.provider.id == "opencode-go"
        and resolved.model.id.startswith("deepseek-v4-")
    )
    if opencode_v4:
        if force_disabled:
            # Retain the explicit compatibility request, not an effective-OFF
            # guarantee: Go may still return native reasoning for this value.
            return {"reasoning_effort": "none"}
        if (
            resolved.model.explicit_thinking
            or (has_tools and resolved.model.tool_reasoning_roundtrip)
        ):
            # Go's advertised variants use effort-only Chat parameters.
            # The serializer handles legacy history; it must not silently
            # switch this request back to the unverified OFF path.
            return {
                "reasoning_effort": (
                    thinking_effort or resolved.model.thinking_effort or "low"
                ),
            }
        return None
    if force_disabled:
        return {"thinking": {"type": "disabled"}}
    if has_tools and resolved.model.tool_reasoning_roundtrip:
        for message in messages:
            if (
                message.get("role") == "assistant"
                and message.get("tool_calls")
                and "reasoning_content" not in message
            ):
                return {"thinking": {"type": "disabled"}}
    if (
        resolved.model.explicit_thinking
        or (has_tools and resolved.model.tool_reasoning_roundtrip)
    ):
        body = {"thinking": {"type": "enabled"}}
        effective_effort = thinking_effort or resolved.model.thinking_effort
        if effective_effort is not None:
            body["reasoning_effort"] = effective_effort
        return body
    return None


def _to_anthropic_messages(messages: list[dict]) -> list[dict]:
    """Convert internal messages to the Anthropic Messages wire format."""
    result: list[dict] = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        msg_type = msg.get("type", "")

        if msg_type == "tool_result":
            result.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_use_id", ""),
                    "content": str(content),
                    "is_error": bool(msg.get("is_error", False)),
                }],
            })
            continue

        if msg_type == "attachment" or "attachment_type" in msg:
            reminder_text = ""
            if isinstance(content, list):
                reminder_text = "".join(
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            else:
                reminder_text = str(content)
            result.append({
                "role": "user",
                "content": f"<system-reminder>{reminder_text}</system-reminder>",
            })
            continue

        if role not in {"user", "assistant"}:
            continue
        if not isinstance(content, list):
            result.append({"role": role, "content": str(content) if content else ""})
            continue

        blocks: list[dict] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                blocks.append({"type": "text", "text": str(block.get("text", ""))})
            elif block_type == "tool_use":
                blocks.append({
                    "type": "tool_use",
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input": block.get("input", {}),
                })
            elif block_type == "tool_result":
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": block.get("tool_use_id", ""),
                    "content": str(block.get("content", "")),
                    "is_error": bool(block.get("is_error", False)),
                })
        result.append({"role": role, "content": blocks})
    return result


class IncompleteTextCompletion(ValueError):
    """A text-only request did not deliver a complete public response."""


def _require_complete_text_response(response: object, protocol: str) -> None:
    if protocol == "openai-chat":
        choice = response.choices[0]
        stop = getattr(choice, "finish_reason", None)
        if stop in {"insufficient_system_resource", "content_filter"}:
            raise ProviderCompletionError(stop)
        message = choice.message
        has_tool = bool(getattr(message, "tool_calls", None) or getattr(message, "function_call", None))
        complete = stop == "stop"
    else:
        stop = getattr(response, "stop_reason", None)
        has_tool = any(getattr(block, "type", None) == "tool_use" for block in response.content)
        complete = stop in {"end_turn", "stop_sequence"}
    if has_tool:
        raise IncompleteTextCompletion("Text-only completion attempted a tool call")
    if not complete:
        reason = "output_limit" if stop in {"length", "max_tokens"} else "unconfirmed_completion"
        raise IncompleteTextCompletion(reason)


async def complete_text(
    messages: list[dict],
    *,
    system_prompt: str = "",
    model: str = "deepseek-chat",
    provider_slot: ProviderSlot | None = None,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    response_format: dict | None = None,
    disable_thinking: bool = False,
    thinking_effort: str | None = None,
    require_complete_response: bool = False,
    timeout_seconds: float | None = None,
    observation_callback: Callable[[CompletionObservation], None] | None = None,
    request_session_id: str | None = None,
) -> str:
    """Return text unchanged; optionally observe the outcome after client cleanup."""
    started = time.perf_counter()
    client = None
    usage = None
    provider_failure = None
    provider_attempts = 0
    status = "failed"
    try:
        resolved = _resolve_request_model(model, provider_slot=provider_slot)
        if resolved.provider.id == "opencode-go":
            client = _build_client(
                resolved.reference,
                provider_slot=provider_slot,
                request_session_id=request_session_id,
            )
        elif provider_slot is None:
            client = _build_client(resolved.reference)
        else:
            client = _build_client(resolved.reference, provider_slot=provider_slot)
        if resolved.model.protocol == "openai-chat":
            kwargs = {
                "model": resolved.model.id,
                "messages": [
                    *([{"role": "system", "content": system_prompt}] if system_prompt else []),
                    *_to_openai_messages(messages),
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": False,
            }
            if response_format is not None:
                kwargs["response_format"] = response_format
            thinking_extra = _thinking_extra_body(
                resolved,
                kwargs["messages"],
                has_tools=False,
                force_disabled=disable_thinking,
                thinking_effort=thinking_effort,
            )
            if thinking_extra is not None:
                kwargs["extra_body"] = thinking_extra
            create = client.chat.completions.create
            provider_attempts = 1
            response = await _await_provider_request(
                create(**kwargs),
                timeout_seconds=timeout_seconds,
            )
            if observation_callback is not None:
                usage = _completion_usage(response, protocol=resolved.model.protocol)
            if require_complete_response:
                _require_complete_text_response(response, resolved.model.protocol)
            text = response.choices[0].message.content or ""
            status = "completed"
            return text

        anthropic_messages = _to_anthropic_messages(messages)
        create = client.messages.create
        provider_attempts = 1
        response = await _await_provider_request(
            create(
                model=resolved.model.id,
                messages=anthropic_messages,
                system=system_prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=False,
            ),
            timeout_seconds=timeout_seconds,
        )
        if observation_callback is not None:
            usage = _completion_usage(response, protocol=resolved.model.protocol)
        if require_complete_response:
            _require_complete_text_response(response, resolved.model.protocol)
        text = "".join(
            str(getattr(block, "text", ""))
            for block in response.content
            if getattr(block, "type", "") == "text"
        )
        status = "completed"
        return text
    except IncompleteTextCompletion:
        # Output/protocol failure is not a network outage. Keep actual usage,
        # but never return a partial body as a durable compaction checkpoint.
        status = "incomplete"
        raise
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except Exception as error:
        if observation_callback is not None:
            provider_failure = _provider_failure_info(error, classify_provider_error(error))
        raise
    finally:
        try:
            if client is not None:
                await _close_client_best_effort(client)
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            if observation_callback is not None:
                observation = CompletionObservation(
                    model=model, usage=usage, provider_failure=provider_failure,
                    provider_attempts=provider_attempts,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    status=status,
                )
                try:
                    observation_callback(observation)
                except BaseException:
                    # Never expose callback exception text or mask the call outcome.
                    logging.getLogger(__name__).warning("COMPLETION_OBSERVATION_CALLBACK_FAILED")


def _completion_usage(response: object, *, protocol: str) -> dict[str, int | bool] | None:
    """Preserve provider cache semantics; missing or unreadable usage is unknown."""
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        if protocol == "openai-chat":
            return _normalize_openai_usage(usage)
        # Match the Anthropic adapter: raw input excludes cache read/creation;
        # cache_miss_tokens is cache creation, not OpenAI's input minus hits.
        return {
            "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
            "cache_hit_tokens": int(getattr(usage, "cache_read_input_tokens", 0) or 0),
            "cache_miss_tokens": int(getattr(usage, "cache_creation_input_tokens", 0) or 0),
            "cache_details_supported": (
                hasattr(usage, "cache_read_input_tokens")
                or hasattr(usage, "cache_creation_input_tokens")
            ),
        }
    except Exception:
        # Optional accounting must not turn a completed response into a failure.
        logging.getLogger(__name__).warning("COMPLETION_OBSERVATION_USAGE_FAILED")
        return None


class _ToolArgumentsError(ValueError):
    """Invalid Tool parameters with any usage already reported by the Provider."""

    def __init__(self, usage: dict[str, int | bool] | None = None):
        super().__init__("Tool arguments must be a valid JSON object")
        self.usage = dict(usage) if usage is not None else None


class _ProviderAttemptTimeout(TimeoutError):
    """Keep discarded atomic events available only for failure classification."""

    def __init__(self, message: str, events: list[LoopEvent], *, hard_deadline: bool):
        super().__init__(message)
        self.events = tuple(events)
        self.hard_deadline = hard_deadline


def _reclassify_repeated_draft_timeout(
    error: Exception,
    *,
    resolved: ResolvedModel,
    disable_thinking: bool,
) -> list[LoopEvent] | None:
    """Return a withheld-output failure, never a partial successful response."""
    if not (
        isinstance(error, _ProviderAttemptTimeout)
        and error.hard_deadline
        and _is_opencode_v4_nonthinking(resolved, disable_thinking=disable_thinking)
        and all(event.type == LoopEventType.TEXT for event in error.events)
    ):
        return None
    visible_text = "".join(event.text or "" for event in error.events)
    prefix = _StartingThinkBlock()
    prefix.feed(visible_text)
    if not prefix.private_open or not _reasoning_repetition_detected(visible_text):
        return None
    failed = _reclassify_atomic_provider_content(
        [*error.events, LoopEvent(
            type=LoopEventType.DONE,
            stop_reason=StopReason.MAX_TOKENS,
            usage={"provider_visible_deliberation_at_deadline": 1},
        )],
        resolved=resolved,
        disable_thinking=disable_thinking,
    )
    # A closing thought boundary with useful public text is still an incomplete
    # stream. It follows the ordinary atomic timeout/discard path.
    if any(event.type != LoopEventType.DONE for event in failed):
        return None
    return failed


async def call_model(
    messages: list[dict],
    system_prompt: str,
    tools: list[ToolDefinition] | None = None,
    model: str = "deepseek-chat",
    provider_slot: ProviderSlot | None = None,
    max_tokens: int = 8192,
    temperature: float = 0.0,
    max_retries: int = 3,
    fallback_model: str | None = None,
    attempt_timeout_seconds: float | None = None,
    first_event_timeout_seconds: float | None = None,
    idle_timeout_seconds: float | None = None,
    atomic_attempts: bool = False,
    disable_thinking: bool = False,
    thinking_effort: str | None = None,
    tool_choice: str | dict | None = None,
    parallel_tool_calls: bool | None = None,
    deliberation_repetition_guard: bool = False,
    request_session_id: str | None = None,
) -> AsyncGenerator[LoopEvent, None]:
    """Call the selected provider and stream normalized LoopEvents."""
    models_to_try = [model]
    if fallback_model and fallback_model != model:
        models_to_try.append(fallback_model)

    for model_idx, current_model in enumerate(models_to_try):
        is_last_model = (model_idx == len(models_to_try) - 1)
        # ``max_retries`` is an upper bound for every model.  Callers such as
        # game mode use ``1`` because their outer query loop owns recovery.
        effective_retries = max_retries if is_last_model else min(2, max_retries)

        request_slot = provider_slot if model_idx == 0 else None
        resolved = _resolve_request_model(current_model, provider_slot=request_slot)
        if resolved.provider.id == "opencode-go":
            # Keep one identity for all attempts owned by this invocation.
            if request_session_id is None:
                request_session_id = uuid4().hex
            client = _build_client(
                resolved.reference,
                provider_slot=request_slot,
                request_session_id=request_session_id,
            )
        elif request_slot is None:
            client = _build_client(resolved.reference)
        else:
            client = _build_client(resolved.reference, provider_slot=request_slot)
        attempts_made = 0
        timeout_count = 0
        disable_thinking_for_attempt = bool(disable_thinking)
        thinking_fallbacks = 0
        attempt_reports: list[ProviderAttemptUsage] = []

        try:
            for attempt in range(effective_retries):
                attempts_made += 1
                emitted_event = False
                attempt_usage = ProviderAttemptUsage()
                attempt_reports.append(attempt_usage)
                chat_stream = request_slot is None or request_slot.chat_stream
                awaiting_complete_chat = (
                    resolved.model.protocol == "openai-chat"
                    and not chat_stream
                    and not (disable_thinking_for_attempt and resolved.model.nonthinking_responses)
                )
                try:
                    if resolved.model.protocol == "openai-chat":
                        if (
                            disable_thinking_for_attempt
                            and resolved.model.nonthinking_responses
                        ):
                            events = _request_openai_responses(
                                client,
                                resolved,
                                messages,
                                system_prompt,
                                tools,
                                max_tokens,
                                temperature=temperature,
                                tool_choice=tool_choice,
                                parallel_tool_calls=parallel_tool_calls,
                                deliberation_repetition_guard=deliberation_repetition_guard,
                                emit_progress=atomic_attempts,
                                attempt_usage=attempt_usage,
                            )
                        else:
                            events = _stream_openai(
                                client, resolved, messages, system_prompt, tools,
                                max_tokens, temperature,
                                emit_progress=atomic_attempts,
                                force_disable_thinking=disable_thinking_for_attempt,
                                thinking_effort=thinking_effort,
                                tool_choice=tool_choice,
                                parallel_tool_calls=parallel_tool_calls,
                                deliberation_repetition_guard=deliberation_repetition_guard,
                                attempt_usage=attempt_usage,
                                streaming=chat_stream,
                            )
                    else:
                        events = _stream_anthropic(
                            client, resolved, messages, system_prompt, tools,
                            max_tokens, temperature,
                            emit_progress=atomic_attempts,
                            attempt_usage=attempt_usage,
                        )
                    if atomic_attempts:
                        buffered = await _collect_attempt(
                            events,
                            first_event_timeout_seconds=(
                                None if awaiting_complete_chat else first_event_timeout_seconds
                            ),
                            idle_timeout_seconds=(
                                None if awaiting_complete_chat else idle_timeout_seconds
                            ),
                            hard_timeout_seconds=attempt_timeout_seconds,
                        )
                        buffered = _reclassify_atomic_provider_content(
                            buffered,
                            resolved=resolved,
                            disable_thinking=disable_thinking_for_attempt,
                        )
                        _attach_provider_attempt_usage(
                            buffered,
                            attempts=attempts_made,
                            timeouts=timeout_count,
                            thinking_fallbacks=thinking_fallbacks,
                            attempt_reports=attempt_reports,
                        )
                        for event in buffered:
                            yield event
                    else:
                        if attempt_timeout_seconds is None:
                            async for event in events:
                                _attach_provider_attempt_usage([event], attempts=attempts_made, timeouts=timeout_count,
                                                               thinking_fallbacks=thinking_fallbacks, attempt_reports=attempt_reports)
                                emitted_event = True
                                yield event
                        else:
                            # A socket read timeout is not a total stream deadline:
                            # intermittent chunks can keep it alive indefinitely.
                            async with asyncio.timeout(attempt_timeout_seconds):
                                async for event in events:
                                    _attach_provider_attempt_usage([event], attempts=attempts_made, timeouts=timeout_count,
                                                                   thinking_fallbacks=thinking_fallbacks, attempt_reports=attempt_reports)
                                    emitted_event = True
                                    yield event
                    return  # 成功，退出重试循环

                except FallbackTriggeredError:
                    raise  # 让上层 query_loop 处理模型切换
                except (asyncio.CancelledError, concurrent.futures.CancelledError):
                    raise
                except Exception as e:
                    if isinstance(e, _ToolArgumentsError):
                        attempt_usage.observe(e.usage)
                    attempt_usage.closed = True
                    if atomic_attempts and isinstance(e, _ToolArgumentsError):
                        # An incomplete/invalid generated object is not a
                        # network failure. Discard the whole atomic attempt;
                        # the query profile owns a changed, bounded recovery.
                        failed = [LoopEvent(
                            type=LoopEventType.DONE, stop_reason=StopReason.MAX_TOKENS,
                            usage={**(e.usage or {}), "provider_invalid_tool_arguments": 1},
                        )]
                        _attach_provider_attempt_usage(
                            failed, attempts=attempts_made, timeouts=timeout_count,
                            thinking_fallbacks=thinking_fallbacks, attempt_reports=attempt_reports,
                        )
                        for event in failed:
                            yield event
                        return
                    decision = classify_provider_error(e)
                    if decision.reason == "timeout":
                        timeout_count += 1
                    if atomic_attempts and deliberation_repetition_guard:
                        failed = _reclassify_repeated_draft_timeout(
                            e,
                            resolved=resolved,
                            disable_thinking=disable_thinking_for_attempt,
                        )
                        if failed is not None:
                            # The hard deadline already ended this attempt.
                            # Let QueryLoop's bounded output recovery own this
                            # failure instead of repeating the same payload.
                            _attach_provider_attempt_usage(
                                failed,
                                attempts=attempts_made,
                                timeouts=timeout_count,
                                thinking_fallbacks=thinking_fallbacks,
                                attempt_reports=attempt_reports,
                            )
                            for event in failed:
                                yield event
                            return
                    can_retry = (
                        decision.retryable
                        and attempt < effective_retries - 1
                        and (atomic_attempts or not emitted_event)
                    )
                    if can_retry:
                        if resolved.model.disable_thinking_on_retry:
                            disable_thinking_for_attempt = True
                            thinking_fallbacks = 1
                        await asyncio.sleep(retry_delay_seconds(
                            decision,
                            attempt_index=attempt,
                        ))
                        continue
                    if not is_last_model:
                        raise FallbackTriggeredError(
                            original_model=model,
                            fallback_model=fallback_model or "unknown",
                        )
                    provider_failure = _provider_failure_info(e, decision)
                    reported_usage = e.usage if isinstance(e, _ToolArgumentsError) else None
                    yield LoopEvent(
                        type=LoopEventType.ERROR,
                        error=("API call failed after all models: "
                                f"{_format_api_exception(e, decision=decision)}"),
                        provider_failure=provider_failure,
                    )
                    yield LoopEvent(
                        type=LoopEventType.DONE,
                        stop_reason=StopReason.END_TURN,
                        usage={
                            **with_attempt_usage(reported_usage, attempt_reports),
                            "provider_attempts": attempts_made,
                            "provider_retries": max(0, attempts_made - 1),
                            "provider_timeouts": timeout_count,
                            "thinking_fallbacks": thinking_fallbacks,
                        },
                    )
                    return
                finally:
                    attempt_usage.closed = True
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                close_result = close()
                if inspect.isawaitable(close_result):
                    await close_result


async def _collect_attempt(
    events: AsyncGenerator[object, None],
    *,
    first_event_timeout_seconds: float | None,
    idle_timeout_seconds: float | None,
    hard_timeout_seconds: float | None,
) -> list[LoopEvent]:
    """Buffer one provider stream until it completes or fails atomically."""
    buffered: list[LoopEvent] = []
    saw_progress = False
    loop = asyncio.get_running_loop()
    deadline = (
        loop.time() + hard_timeout_seconds
        if hard_timeout_seconds is not None
        else None
    )
    try:
        while True:
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                raise _ProviderAttemptTimeout(
                    "hard provider attempt timeout", buffered, hard_deadline=True,
                )
            progress_timeout = (
                first_event_timeout_seconds
                if not saw_progress
                else idle_timeout_seconds
            )
            candidates = [
                value for value in (remaining, progress_timeout)
                if value is not None
            ]
            timeout = min(candidates) if candidates else None
            event_task = asyncio.create_task(anext(events))
            try:
                if timeout is None:
                    await event_task
                else:
                    done, _pending = await asyncio.wait(
                        {event_task}, timeout=timeout,
                    )
                    if not done:
                        # asyncio.wait_for() waits for the cancelled awaitable
                        # to finish.  A provider stream can swallow
                        # CancelledError while its socket/SDK task is wedged,
                        # which would keep the game lease OPEN forever.  Fence
                        # this attempt immediately and detach the owned task;
                        # the outer client close below still releases the
                        # transport resources.
                        event_task.cancel()
                        _consume_detached_task(event_task)
                        raise _ProviderAttemptTimeout(
                            "provider stream progress timeout",
                            buffered,
                            hard_deadline=remaining is not None and timeout == remaining,
                        )
                event = event_task.result()
            except asyncio.CancelledError:
                if not event_task.done():
                    event_task.cancel()
                    _consume_detached_task(event_task)
                raise
            except StopAsyncIteration:
                return buffered
            saw_progress = True
            if event is _PROVIDER_PROGRESS:
                continue
            if not isinstance(event, LoopEvent):
                raise RuntimeError(
                    f"invalid provider stream event: {type(event).__name__}",
                )
            buffered.append(event)
    finally:
        await _close_stream_best_effort(events)


def _consume_detached_task(task: asyncio.Task) -> None:
    """Retrieve a detached task's eventual result without blocking its owner."""
    def consume(completed: asyncio.Task) -> None:
        try:
            completed.result()
        except BaseException:
            pass

    task.add_done_callback(consume)


async def _await_provider_request(
    awaitable,
    *,
    timeout_seconds: float | None,
):
    """Await a non-stream request without waiting on cancellation-resistant SDKs."""
    task = asyncio.create_task(awaitable)
    try:
        done, _pending = await asyncio.wait(
            {task},
            timeout=timeout_seconds,
        )
        if not done:
            task.cancel()
            _consume_detached_task(task)
            raise TimeoutError(
                f"provider request timed out after {timeout_seconds:g} seconds"
            )
        return task.result()
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
        _consume_detached_task(task)
        raise


async def _close_resource_best_effort(resource: object, *, method_name: str) -> None:
    """Close an async provider resource without masking the model failure."""
    close = getattr(resource, method_name, None)
    if close is None:
        return
    try:
        close_result = close()
    except BaseException:
        return
    if not inspect.isawaitable(close_result):
        return
    try:
        close_task = asyncio.create_task(close_result)
    except (TypeError, RuntimeError):
        return
    done, _pending = await asyncio.wait(
        {close_task}, timeout=_STREAM_CLOSE_TIMEOUT_SECONDS,
    )
    if done:
        # Always retrieve a completed close task's result.  Provider SDKs may
        # raise when cleanup races transport shutdown; that exception is a
        # resource-cleanup diagnostic, not a second model failure.
        _consume_detached_task(close_task)
    else:
        close_task.cancel()
        _consume_detached_task(close_task)


async def _close_stream_best_effort(events: object) -> None:
    """Close a provider stream without allowing a wedged close to block retry."""
    await _close_resource_best_effort(events, method_name="aclose")


async def _close_client_best_effort(client: object) -> None:
    """Close a provider client without masking the model failure."""
    await _close_resource_best_effort(client, method_name="close")


def _attach_provider_attempt_usage(
    events: list[LoopEvent],
    *,
    attempts: int,
    timeouts: int,
    thinking_fallbacks: int,
    attempt_reports: list[ProviderAttemptUsage],
) -> None:
    for event in reversed(events):
        if event.type != LoopEventType.DONE:
            continue
        usage = dict(event.usage or {})
        # Custom adapters may report only on DONE. Built-in adapters observe
        # wire reports earlier, so a locally generated DONE cannot finalize them.
        if not attempt_reports[-1].usage:
            attempt_reports[-1].observe(usage, finished=True)
        usage = with_attempt_usage(usage, attempt_reports)
        usage.update({
            "provider_attempts": attempts,
            "provider_retries": max(0, attempts - 1),
            "provider_timeouts": timeouts,
            "thinking_fallbacks": thinking_fallbacks,
        })
        event.usage = usage
        return


def _provider_failure_info(
    error: BaseException,
    decision,
) -> ProviderFailureInfo | None:
    """Build redacted metadata only for transport/provider/API failures."""
    reason = decision.reason
    if not _is_provider_failure(error, reason):
        return None
    return ProviderFailureInfo(
        reason=reason,
        retryable_same_slot=decision.retryable,
        switch_slot=decision.switch_slot,
        status_code=_status_code(error),
    )


def _is_provider_failure(error: BaseException, reason: str) -> bool:
    if isinstance(error, ProviderCompletionError):
        return True
    if reason in {"timeout", "connection"} or reason.startswith("http_"):
        return True
    name = type(error).__name__.lower()
    return any(token in name for token in ("api", "openai", "anthropic"))


def _status_code(error: BaseException) -> int | None:
    value = getattr(error, "status_code", None)
    if value is None:
        response = getattr(error, "response", None)
        value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _format_api_exception(error: BaseException, *, decision=None) -> str:
    """Return one stable public code; raw provider details stay internal."""
    del error, decision
    return PROVIDER_REQUEST_FAILED


def _chat_stop_reason(finish: object) -> StopReason:
    """Accept only declared completion states, never an interrupted tool draft."""
    if isinstance(finish, str) and finish in {"insufficient_system_resource", "content_filter"}:
        raise ProviderCompletionError(str(finish))
    reasons = {
        "stop": StopReason.END_TURN,
        "tool_calls": StopReason.TOOL_USE,
        "length": StopReason.MAX_TOKENS,
    }
    if not isinstance(finish, str) or finish not in reasons:
        raise httpx.RemoteProtocolError("Provider response has no valid completion signal")
    return reasons[finish]


async def _stream_openai(
    client,
    resolved: ResolvedModel,
    messages: list[dict],
    system_prompt: str,
    tools: list[ToolDefinition] | None,
    max_tokens: int,
    temperature: float,
    *,
    emit_progress: bool = False,
    force_disable_thinking: bool = False,
    thinking_effort: str | None = None,
    tool_choice: str | dict | None = None,
    parallel_tool_calls: bool | None = None,
    deliberation_repetition_guard: bool = False,
    attempt_usage: ProviderAttemptUsage | None = None,
    streaming: bool = True,
) -> AsyncGenerator[object, None]:
    # DeepSeek requires reasoning_content replay only while thinking mode is
    # enabled.  In an explicitly non-thinking request, preserving that mode's
    # replay contract makes ordinary assistant tool calls without reasoning
    # look incomplete, so _to_openai_messages rebases a valid tool exchange as
    # two user messages.  Keep the standard assistant tool_call -> tool result
    # wire shape when thinking is disabled.
    preserve_reasoning_content = (
        resolved.model.tool_reasoning_roundtrip
        and not force_disable_thinking
    )
    api_messages = [
        {"role": "system", "content": system_prompt},
        *_to_openai_messages(
            messages,
            preserve_reasoning_content=preserve_reasoning_content,
            # Official V4 requires native history even for non-tool replies
            # when this request carries tools. Legacy public prose is retained
            # as attributed history; never fabricate missing private reasoning.
            require_reasoning_content=bool(tools) and resolved.provider.id == "deepseek",
        ),
    ]
    api_tools = [tool.to_openai_schema() for tool in tools] if tools else None
    request_kwargs = dict(
        model=resolved.model.id,
        messages=api_messages,
        tools=api_tools,
        max_tokens=max_tokens,
        temperature=temperature,
        stream=True,
        stream_options={"include_usage": True},
    )
    thinking_extra = _thinking_extra_body(
        resolved,
        api_messages,
        has_tools=bool(api_tools),
        force_disabled=force_disable_thinking,
        thinking_effort=thinking_effort,
    )
    if thinking_extra is not None:
        request_kwargs["extra_body"] = thinking_extra
    if (
        api_tools
        and tool_choice is not None
        and resolved.model.supports_tool_choice
    ):
        request_kwargs["tool_choice"] = tool_choice
    if api_tools and parallel_tool_calls is not None:
        request_kwargs["parallel_tool_calls"] = parallel_tool_calls
    if not streaming:
        # Some compatible relays corrupt streamed argument deltas. An explicit
        # slot capability can request the completed response instead. Reuse the
        # same input serialization and the caller's independent total deadline.
        request_kwargs["stream"] = False
        request_kwargs.pop("stream_options")
        response = await client.chat.completions.create(**request_kwargs)
        async for event in _completed_openai_chat_events(response, attempt_usage):
            yield event
        return
    stream = await client.chat.completions.create(**request_kwargs)

    current_tool_id: str | None = None
    current_tool_name: str | None = None
    current_tool_args = ""
    current_reasoning = ""
    current_visible_text = ""
    saw_reasoning = False
    final_stop_reason = StopReason.END_TURN
    final_usage: dict[str, int] | None = None
    reasoning_repetition_interrupted = False
    output_budget_interrupted = False
    received_finish = False
    reasoning_scan_chars = visible_scan_chars = 0
    visible_prefix = _StartingThinkBlock(
        recognize_private=emit_progress and _is_opencode_v4_nonthinking(
            resolved, disable_thinking=force_disable_thinking,
        ),
    )

    async for chunk in stream:
        if chunk.usage:
            final_usage = _normalize_openai_usage(chunk.usage)
        delta = chunk.choices[0].delta if chunk.choices else None
        finish_reason = chunk.choices[0].finish_reason if chunk.choices else None
        if attempt_usage is not None:
            attempt_usage.observe(final_usage, finished=bool(finish_reason))
        reasoning_delta = (
            getattr(delta, "reasoning_content", None) if delta else None
        )
        if (
            emit_progress
            and delta is not None
            and not (reasoning_delta or delta.content or delta.tool_calls)
            and not finish_reason
        ):
            # Some relays emit assistant deltas while hiding reasoning text.
            # These prove transport activity, not a completed decision. Keep
            # the independent hard deadline; never publish a heartbeat as text
            # or let it replenish the total generation budget.
            yield _PROVIDER_PROGRESS
        if reasoning_delta is not None:
            saw_reasoning = True
            current_reasoning += str(reasoning_delta)
            if emit_progress and str(reasoning_delta):
                yield _PROVIDER_PROGRESS
        if delta and delta.content:
            current_visible_text += str(delta.content)
            visible_prefix.feed(str(delta.content))
            yield LoopEvent(type=LoopEventType.TEXT, text=delta.content)
        if delta and delta.tool_calls:
            for tool_call in delta.tool_calls:
                if tool_call.id:
                    if current_tool_id and current_tool_name:
                        yield _flush_tool(
                            current_tool_id, current_tool_name, current_tool_args,
                            usage=final_usage,
                        )
                    current_tool_id = tool_call.id
                    current_tool_name = (
                        tool_call.function.name if tool_call.function else ""
                    )
                    current_tool_args = ""
                    yield LoopEvent(type=LoopEventType.TOOL_USE_START)
                if tool_call.function and tool_call.function.arguments:
                    current_tool_args += tool_call.function.arguments
                    if emit_progress:
                        yield _PROVIDER_PROGRESS
        if finish_reason:
            received_finish = True
            final_stop_reason = _chat_stop_reason(finish_reason)

        # Some compatible gateways report cumulative output while continuing
        # past max_tokens. Stop at the first reported crossing, without guessing
        # tokens from characters. A finish on this chunk still completes its tool.
        if not received_finish and (final_usage or {}).get("output_tokens", 0) >= max_tokens:
            output_budget_interrupted = True
            break

        # A completion and public suffix can share the chunk with private text.
        # Interpret all fields before deciding whether generation is ongoing.
        if deliberation_repetition_guard and not received_finish and not visible_prefix.public_progress:
            if len(current_reasoning) - reasoning_scan_chars >= _REASONING_REPEAT_SCAN_STRIDE:
                reasoning_scan_chars = len(current_reasoning)
                reasoning_repetition_interrupted = _reasoning_repetition_detected(current_reasoning)
            if (
                visible_prefix.private_open
                and len(current_visible_text) - visible_scan_chars >= _REASONING_REPEAT_SCAN_STRIDE
            ):
                visible_scan_chars = len(current_visible_text)
                reasoning_repetition_interrupted |= _reasoning_repetition_detected(current_visible_text)
            if reasoning_repetition_interrupted:
                break

    if reasoning_repetition_interrupted or output_budget_interrupted:
        await _close_resource_best_effort(stream, method_name="close")
        final_stop_reason = StopReason.MAX_TOKENS
        marker = (
            "provider_output_budget_interrupted" if output_budget_interrupted
            else "provider_repetition_guard_interrupted"
        )
        final_usage = {**(final_usage or {}), marker: 1}

    if not received_finish and not (reasoning_repetition_interrupted or output_budget_interrupted):
        # EOF can be clean at the HTTP/SSE layer while the generation itself is
        # incomplete. Valid JSON is not a substitute for its completion signal.
        await _close_resource_best_effort(stream, method_name="close")
        raise httpx.RemoteProtocolError("Provider stream ended without a completion signal")

    # Syntax alone does not complete the active Tool after an explicit output
    # interruption. Earlier independent completed Tools retain their contract.
    if current_tool_id and current_tool_name and final_stop_reason != StopReason.MAX_TOKENS:
        yield _flush_tool(
            current_tool_id, current_tool_name, current_tool_args, usage=final_usage,
        )
    yield LoopEvent(
        type=LoopEventType.DONE,
        stop_reason=final_stop_reason,
        usage=final_usage,
        reasoning_content=current_reasoning if saw_reasoning else None,
    )


async def _completed_openai_chat_events(
    response: object,
    attempt_usage: ProviderAttemptUsage | None,
) -> AsyncGenerator[LoopEvent, None]:
    choices = getattr(response, "choices", None)
    usage = getattr(response, "usage", None)
    final_usage = _normalize_openai_usage(usage) if usage is not None else None
    choice = choices[0] if choices else None
    finish = getattr(choice, "finish_reason", None)
    if attempt_usage is not None:
        attempt_usage.observe(final_usage, finished=bool(finish))
    if not finish:
        raise httpx.RemoteProtocolError("Provider response has no completion signal")
    stop_reason = _chat_stop_reason(finish)
    message = getattr(choice, "message", None)
    if message is None:
        raise httpx.RemoteProtocolError("Provider response has no assistant message")
    if message.content:
        yield LoopEvent(type=LoopEventType.TEXT, text=message.content)
    if stop_reason != StopReason.MAX_TOKENS:
        for tool in message.tool_calls or []:
            yield LoopEvent(type=LoopEventType.TOOL_USE_START)
            yield _flush_tool(tool.id, tool.function.name, tool.function.arguments, usage=final_usage)
    yield LoopEvent(
        type=LoopEventType.DONE,
        stop_reason=stop_reason,
        usage=final_usage,
        reasoning_content=getattr(message, "reasoning_content", None),
    )


async def _request_openai_responses(
    client,
    resolved: ResolvedModel,
    messages: list[dict],
    system_prompt: str,
    tools: list[ToolDefinition] | None,
    max_tokens: int,
    *,
    temperature: float = 0.0,
    tool_choice: str | dict | None = None,
    parallel_tool_calls: bool | None = None,
    deliberation_repetition_guard: bool = False,
    emit_progress: bool = False,
    attempt_usage: ProviderAttemptUsage | None = None,
) -> AsyncGenerator[object, None]:
    """Use V4 Flash's Responses route for non-thinking turns.

    Provider attempts remain atomic at ``call_model``. Streaming is used only
    to surface transport progress and avoid treating a long generation as a
    missing first byte; normalized text and tool events are still released only
    after the complete response is available.
    """

    kwargs = {
        "model": resolved.model.id,
        "instructions": system_prompt,
        "input": _to_responses_input(messages),
        "max_output_tokens": max_tokens,
        "reasoning": {"effort": "none"},
        "temperature": temperature,
        "store": False,
        "stream": True,
    }
    if tools:
        kwargs["tools"] = [tool.to_responses_schema() for tool in tools]
    if tool_choice is not None:
        if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
            function = tool_choice.get("function")
            name = function.get("name") if isinstance(function, dict) else None
            if not isinstance(name, str) or not name:
                raise ValueError("Responses function tool_choice requires a name")
            kwargs["tool_choice"] = {"type": "function", "name": name}
        else:
            kwargs["tool_choice"] = tool_choice
    if parallel_tool_calls is not None:
        kwargs["parallel_tool_calls"] = parallel_tool_calls
    response_or_stream = await client.responses.create(**kwargs)
    response = response_or_stream
    if hasattr(response_or_stream, "__aiter__"):
        response = None
        try:
            async for stream_event in response_or_stream:
                if emit_progress:
                    yield _PROVIDER_PROGRESS
                event_type = str(getattr(stream_event, "type", ""))
                if event_type in {
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                }:
                    response = getattr(stream_event, "response", None)
                    if attempt_usage is not None:
                        attempt_usage.observe(_normalize_responses_usage(getattr(response, "usage", None)), finished=True)
        finally:
            await _close_resource_best_effort(
                response_or_stream,
                method_name="close",
            )
        if response is None:
            raise RuntimeError(
                "Responses provider stream ended without a final response",
            )
    status = str(getattr(response, "status", ""))
    if attempt_usage is not None:
        attempt_usage.observe(_normalize_responses_usage(getattr(response, "usage", None)),
                              finished=status in {"completed", "incomplete", "failed"})
    if status == "failed":
        raise RuntimeError("Responses provider returned failed status")

    raw_usage = getattr(response, "usage", None)
    usage = _normalize_responses_usage(raw_usage)
    saw_tool = False
    buffered_events: list[LoopEvent] = []
    for item in getattr(response, "output", ()):
        item_type = str(getattr(item, "type", ""))
        if item_type == "message":
            for part in getattr(item, "content", ()):
                text = str(getattr(part, "text", "") or "")
                if text:
                    buffered_events.append(
                        LoopEvent(type=LoopEventType.TEXT, text=text)
                    )
        elif item_type == "function_call":
            saw_tool = True
            buffered_events.append(LoopEvent(type=LoopEventType.TOOL_USE_START))
            buffered_events.append(_flush_tool(
                str(getattr(item, "call_id", "") or getattr(item, "id", "")),
                str(getattr(item, "name", "")),
                str(getattr(item, "arguments", "")),
                usage=usage if raw_usage is not None else None,
            ))

    for event in buffered_events:
        yield event

    stop_reason = StopReason.TOOL_USE if saw_tool else StopReason.END_TURN
    incomplete = getattr(response, "incomplete_details", None)
    if status == "incomplete" and getattr(incomplete, "reason", "") == "max_output_tokens":
        stop_reason = StopReason.MAX_TOKENS
    yield LoopEvent(
        type=LoopEventType.DONE,
        stop_reason=stop_reason,
        usage=usage,
        reasoning_content=None,
    )


def _normalize_responses_usage(usage: object | None) -> dict[str, int | bool]:
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    details = getattr(usage, "input_tokens_details", None)
    cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
    output_details = getattr(usage, "output_tokens_details", None)
    reasoning_tokens = int(getattr(output_details, "reasoning_tokens", 0) or 0)
    result = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_hit_tokens": cached_tokens,
        "cache_miss_tokens": max(input_tokens - cached_tokens, 0),
        "cache_details_supported": details is not None,
        "reasoning_tokens": reasoning_tokens,
    }
    for key in ("input_tokens", "output_tokens"):
        if getattr(usage, key, None) is None:
            result.pop(key)
    return result if usage is not None else {}


def _normalize_openai_usage(usage: object) -> dict[str, int | bool]:
    """Normalize legacy and standard OpenAI-compatible cache usage fields."""
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    legacy_hit_present = hasattr(usage, "prompt_cache_hit_tokens")
    details = getattr(usage, "prompt_tokens_details", None)
    standard_hit_present = details is not None and hasattr(details, "cached_tokens")
    if legacy_hit_present:
        cache_hit = int(getattr(usage, "prompt_cache_hit_tokens", 0) or 0)
    elif standard_hit_present:
        cache_hit = int(getattr(details, "cached_tokens", 0) or 0)
    else:
        cache_hit = 0
    legacy_miss_present = hasattr(usage, "prompt_cache_miss_tokens")
    cache_miss = (
        int(getattr(usage, "prompt_cache_miss_tokens", 0) or 0)
        if legacy_miss_present
        else max(input_tokens - cache_hit, 0)
    )
    result = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_hit_tokens": cache_hit,
        "cache_miss_tokens": cache_miss,
        "cache_details_supported": legacy_hit_present or standard_hit_present,
    }
    for source, target in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")):
        if getattr(usage, source, None) is None:
            result.pop(target)
    return result


async def _stream_anthropic(
    client,
    resolved: ResolvedModel,
    messages: list[dict],
    system_prompt: str,
    tools: list[ToolDefinition] | None,
    max_tokens: int,
    temperature: float,
    *,
    emit_progress: bool = False,
    attempt_usage: ProviderAttemptUsage | None = None,
) -> AsyncGenerator[object, None]:
    kwargs = {
        "model": resolved.model.id,
        "messages": _to_anthropic_messages(messages),
        "system": system_prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "stream": True,
    }
    if tools:
        kwargs["tools"] = [tool.to_anthropic_schema() for tool in tools]
    stream = await client.messages.create(**kwargs)

    current_tool_id: str | None = None
    current_tool_name: str | None = None
    current_tool_args = ""
    saw_tool_argument_delta = False
    final_stop_reason = StopReason.END_TURN
    reported_usage: dict[str, int | bool] = {}

    async for event in stream:
        if emit_progress:
            yield _PROVIDER_PROGRESS
        event_type = getattr(event, "type", "")
        if event_type == "message_start":
            event_usage = getattr(getattr(event, "message", None), "usage", None)
            if event_usage:
                for source, target in (
                    ("input_tokens", "input_tokens"),
                    ("output_tokens", "output_tokens"),
                    ("cache_read_input_tokens", "cache_hit_tokens"),
                    ("cache_creation_input_tokens", "cache_miss_tokens"),
                ):
                    value = getattr(event_usage, source, None)
                    if value is not None:
                        reported_usage[target] = value
                if "cache_hit_tokens" in reported_usage or "cache_miss_tokens" in reported_usage:
                    reported_usage["cache_details_supported"] = True
                if attempt_usage is not None:
                    attempt_usage.observe(reported_usage)
        elif event_type == "content_block_start":
            block = getattr(event, "content_block", None)
            if getattr(block, "type", "") == "tool_use":
                current_tool_id = getattr(block, "id", "")
                current_tool_name = getattr(block, "name", "")
                current_tool_args = (
                    json.dumps(block.input) if hasattr(block, "input") else ""
                )
                saw_tool_argument_delta = False
                yield LoopEvent(type=LoopEventType.TOOL_USE_START)
        elif event_type == "content_block_delta":
            delta = getattr(event, "delta", None)
            delta_type = getattr(delta, "type", "")
            if delta_type == "text_delta":
                text = getattr(delta, "text", "")
                if text:
                    yield LoopEvent(type=LoopEventType.TEXT, text=text)
            elif delta_type == "input_json_delta":
                if not saw_tool_argument_delta:
                    # Stream deltas replace the initial input placeholder.
                    current_tool_args = ""
                    saw_tool_argument_delta = True
                current_tool_args += getattr(delta, "partial_json", "") or ""
        elif event_type == "content_block_stop":
            if current_tool_id and current_tool_name:
                yield _flush_tool(
                    current_tool_id, current_tool_name, current_tool_args,
                    usage=reported_usage or None,
                )
                current_tool_id = None
                current_tool_name = None
                current_tool_args = ""
        elif event_type == "message_delta":
            delta = getattr(event, "delta", None)
            stop_reason = getattr(delta, "stop_reason", None)
            final_stop_reason = {
                "end_turn": StopReason.END_TURN,
                "tool_use": StopReason.TOOL_USE,
                "max_tokens": StopReason.MAX_TOKENS,
                "stop_sequence": StopReason.STOP_SEQUENCE,
            }.get(stop_reason, final_stop_reason)
            event_usage = getattr(event, "usage", None)
            if event_usage:
                if getattr(event_usage, "output_tokens", None) is not None:
                    reported_usage["output_tokens"] = event_usage.output_tokens
            if attempt_usage is not None:
                attempt_usage.observe(reported_usage, finished=bool(stop_reason))

    if current_tool_id and current_tool_name:
        yield _flush_tool(
            current_tool_id, current_tool_name, current_tool_args,
            usage=reported_usage or None,
        )
    yield LoopEvent(
        type=LoopEventType.DONE,
        stop_reason=final_stop_reason,
        usage=reported_usage or None,
    )


def _flush_tool(
    tool_id: str, name: str, args_json: str, *,
    usage: dict[str, int | bool] | None = None,
) -> LoopEvent:
    import json as _json

    def reject_constant(_value: str) -> None:
        raise _ToolArgumentsError(usage)

    try:
        parsed = _json.loads(args_json, parse_constant=reject_constant)
    except _json.JSONDecodeError:
        raise _ToolArgumentsError(usage) from None
    if not isinstance(parsed, dict):
        raise _ToolArgumentsError(usage)

    return LoopEvent(
        type=LoopEventType.TOOL_USE_END,
        tool_use=ToolUseBlock(id=tool_id, name=name, input=parsed),
    )
