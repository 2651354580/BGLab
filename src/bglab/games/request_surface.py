"""Internal identity for the complete Game Player request surface."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping

from bglab.games.attachments import GAME_ATTACHMENT_PROVIDERS
from bglab.llm.client import _to_anthropic_messages, _to_openai_messages
from bglab.llm.providers import resolve_model


def stable_hash(value: Any) -> str:
    """Hash one Provider surface using the repository's canonical JSON form."""

    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class ExactModelVisibleSurface:
    payload: Mapping[str, Any]
    sha256: str


@dataclass(frozen=True)
class ProviderPolicyIdentity:
    payload: Mapping[str, Any]
    sha256: str


@dataclass(frozen=True)
class TransportIdentity:
    payload: Mapping[str, Any]
    sha256: str


def freeze_exact_model_visible_surface(
    *,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: Iterable[Any],
    protocol: str,
    preserve_reasoning_content: bool = False,
) -> ExactModelVisibleSurface:
    """Freeze fields after the selected Provider protocol serializer drops metadata."""

    if not isinstance(system_prompt, str):
        raise TypeError("system_prompt must be text")
    if not isinstance(messages, list) or not all(
        isinstance(message, dict) for message in messages
    ):
        raise TypeError("messages must be a list of objects")
    provider_tools = [
        tool.to_tool_definition() if hasattr(tool, "to_tool_definition") else tool
        for tool in tools
    ]
    if protocol == "openai-chat":
        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                *_to_openai_messages(
                    copy.deepcopy(messages),
                    preserve_reasoning_content=preserve_reasoning_content,
                ),
            ],
            "tools": [tool.to_openai_schema() for tool in provider_tools]
            if provider_tools
            else None,
        }
    elif protocol == "anthropic-messages":
        payload = {
            "system": system_prompt,
            "messages": _to_anthropic_messages(copy.deepcopy(messages)),
        }
        if provider_tools:
            payload["tools"] = [
                tool.to_anthropic_schema() for tool in provider_tools
            ]
    else:
        raise ValueError(f"unsupported Provider protocol: {protocol}")
    return ExactModelVisibleSurface(
        payload=copy.deepcopy(payload),
        sha256=stable_hash(payload),
    )


def freeze_provider_request_policy(
    *,
    model: str,
    max_tokens: int,
    temperature: float,
    thinking: Any,
    tool_choice: str | Mapping[str, Any] | None,
    parallel_tool_calls: bool | None,
    preserve_reasoning_content: bool = False,
) -> ProviderPolicyIdentity:
    """Freeze model-affecting controls without transport or credential data."""

    resolved = resolve_model(model)
    payload = {
        "model": resolved.reference,
        "protocol": resolved.model.protocol,
        "maxTokens": max_tokens,
        "temperature": temperature,
        "effectiveThinking": copy.deepcopy(thinking),
        "toolChoice": copy.deepcopy(tool_choice),
        "parallelToolCalls": parallel_tool_calls,
        "preserveReasoningContent": preserve_reasoning_content,
    }
    return ProviderPolicyIdentity(
        payload=copy.deepcopy(payload),
        sha256=stable_hash(payload),
    )


def freeze_transport_identity(
    *,
    provider_slot: Any,
    model_reference: str,
    attempt: int,
    max_attempts: int,
    attempt_timeout_seconds: float,
    progress_timeout_seconds: float,
) -> TransportIdentity:
    """Freeze non-secret request routing and attempt controls."""

    resolved = resolve_model(model_reference)
    payload = {
        "slot": str(getattr(provider_slot, "id", "unspecified")),
        "provider": str(
            getattr(provider_slot, "provider", resolved.provider.id)
        ),
        "modelReference": resolved.reference,
        "attempt": int(attempt),
        "maxAttempts": int(max_attempts),
        "attemptTimeoutSeconds": float(attempt_timeout_seconds),
        "progressTimeoutSeconds": float(progress_timeout_seconds),
    }
    return TransportIdentity(
        payload=copy.deepcopy(payload),
        sha256=stable_hash(payload),
    )


def build_game_request_context(
    *,
    definition: Any,
    feature_profile: Any,
    game_rules: str,
    tools: list[Any],
    thinking_policy: Mapping[str, Any] | Any,
    query_profile: Any = None,
) -> dict[str, Any]:
    attachment_profile = getattr(query_profile, "attachments", None)
    selected_attachment_ids = None
    if attachment_profile is not None:
        selected_attachment_ids = {
            provider.id
            for slot in (
                "head", "user_input", "thread", "main", "post_compact",
            )
            for provider in getattr(attachment_profile, slot, ())
        }
        if getattr(attachment_profile, "memory_prefetch", None) is not None:
            selected_attachment_ids.add("game_memory")
        if getattr(attachment_profile, "skill_prefetch", None) is not None:
            selected_attachment_ids.add("skill_prefetch")
    enabled_attachments = []
    for provider in GAME_ATTACHMENT_PROVIDERS:
        if (
            selected_attachment_ids is not None
            and provider.provider_id not in selected_attachment_ids
        ):
            continue
        capability = provider.capability
        if capability == "always" or bool(
            getattr(feature_profile, capability, False)
        ):
            enabled_attachments.append({
                "id": provider.provider_id,
                "slot": provider.slot,
                "timing": provider.timing,
            })
    tool_definitions = []
    for tool in tools:
        definition_value = (
            tool.to_tool_definition()
            if hasattr(tool, "to_tool_definition")
            else tool
        )
        tool_definitions.append({
            "name": str(getattr(definition_value, "name", "")),
            "description": str(getattr(definition_value, "description", "")),
            "parameters": getattr(definition_value, "parameters", {}),
        })
    if is_dataclass(thinking_policy):
        thinking = asdict(thinking_policy)
    elif isinstance(thinking_policy, Mapping):
        thinking = dict(thinking_policy)
    else:
        raise TypeError("thinking_policy must be a dataclass or mapping")
    return {
        "sessionHead": {
            "game": str(getattr(definition, "id", "")),
            "rulesVersion": int(getattr(definition, "snapshot_version", 0)),
            "rules": str(game_rules),
            "informationBoundary": "seat-authorized DecisionFrame only",
        },
        "tools": tool_definitions,
        "attachments": enabled_attachments,
        "thinking": thinking,
        "modules": (
            {
                "profile": str(getattr(query_profile, "name", "")),
                "prompt": str(getattr(getattr(query_profile, "prompt", None), "name", "")),
                "tools": str(getattr(getattr(query_profile, "tools", None), "name", "")),
                "attachments": str(getattr(attachment_profile, "name", "")),
                "contextShaping": str(getattr(getattr(query_profile, "context_shaping", None), "name", "")),
                "compaction": str(getattr(getattr(query_profile, "compaction", None), "name", "")),
                "memory": str(getattr(getattr(query_profile, "memory", None), "name", "")),
                "agentRuntime": str(getattr(getattr(query_profile, "agent_runtime", None), "name", "")),
                "stopHook": str(getattr(getattr(query_profile, "stop_hook", None), "name", "")),
                "recovery": str(getattr(getattr(query_profile, "recovery", None), "name", "")),
            }
            if query_profile is not None
            else {}
        ),
    }


__all__ = [
    "ExactModelVisibleSurface",
    "ProviderPolicyIdentity",
    "TransportIdentity",
    "build_game_request_context",
    "freeze_exact_model_visible_surface",
    "freeze_provider_request_policy",
    "freeze_transport_identity",
    "stable_hash",
]
