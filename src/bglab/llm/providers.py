"""LLM provider catalog and canonical model-reference resolution."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    id: str
    display_name: str
    protocol: str = "openai-chat"
    tool_reasoning_roundtrip: bool = False
    explicit_thinking: bool = False
    thinking_effort: str | None = None
    disable_thinking_on_retry: bool = False
    supports_tool_choice: bool = True
    nonthinking_responses: bool = False
    context_window_tokens: int = 128_000
    serving_context_window_tokens: int | None = None


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    display_name: str
    openai_base_url: str
    anthropic_base_url: str | None
    api_key_env: str
    models: tuple[ModelSpec, ...]

    def get_model(self, model_id: str) -> ModelSpec | None:
        return next((model for model in self.models if model.id == model_id), None)


@dataclass(frozen=True)
class ResolvedModel:
    reference: str
    provider: ProviderSpec
    model: ModelSpec


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        id="deepseek",
        display_name="DeepSeek",
        openai_base_url="https://api.deepseek.com/v1",
        anthropic_base_url=None,
        api_key_env="DEEPSEEK_API_KEY",
        models=(
            ModelSpec("deepseek-chat", "DeepSeek Chat"),
            ModelSpec("deepseek-reasoner", "DeepSeek Reasoner"),
            ModelSpec(
                "deepseek-flash",
                "DeepSeek V4.1 Flash",
                context_window_tokens=1_000_000,
                tool_reasoning_roundtrip=True,
                explicit_thinking=True,
                thinking_effort="high",
            ),
            ModelSpec(
                "deepseek-v4-flash",
                "DeepSeek V4 Flash",
                context_window_tokens=1_000_000,
                tool_reasoning_roundtrip=True,
                explicit_thinking=True,
                thinking_effort="max",
            ),
            ModelSpec(
                "deepseek-v4-pro",
                "DeepSeek V4 Pro",
                context_window_tokens=1_000_000,
                tool_reasoning_roundtrip=True,
                explicit_thinking=True,
                thinking_effort="max",
            ),
        ),
    ),
    ProviderSpec(
        id="opencode-go",
        display_name="OpenCode Go",
        openai_base_url="https://opencode.ai/zen/go/v1",
        anthropic_base_url="https://opencode.ai/zen/go",
        api_key_env="OPENCODE_GO_API_KEY",
        models=(
            ModelSpec("grok-4.5", "Grok 4.5"),
            ModelSpec("glm-5.2", "GLM-5.2"),
            ModelSpec("glm-5.1", "GLM-5.1"),
            ModelSpec("kimi-k3", "Kimi K3"),
            ModelSpec("kimi-k2.7-code", "Kimi K2.7 Code"),
            ModelSpec("kimi-k2.6", "Kimi K2.6"),
            ModelSpec("mimo-v2.5", "MiMo-V2.5"),
            ModelSpec("mimo-v2.5-pro", "MiMo-V2.5-Pro"),
            ModelSpec(
                "deepseek-v4-flash",
                "DeepSeek V4 Flash",
                context_window_tokens=1_000_000,
                tool_reasoning_roundtrip=True,
                explicit_thinking=True,
                thinking_effort="low",
                serving_context_window_tokens=131_072,
            ),
            ModelSpec(
                "deepseek-v4-pro",
                "DeepSeek V4 Pro",
                context_window_tokens=1_000_000,
                tool_reasoning_roundtrip=True,
                explicit_thinking=True,
                thinking_effort="high",
            ),
            ModelSpec("minimax-m3", "MiniMax M3", "anthropic-messages"),
            ModelSpec("minimax-m2.7", "MiniMax M2.7", "anthropic-messages"),
            ModelSpec("minimax-m2.5", "MiniMax M2.5", "anthropic-messages"),
            ModelSpec("qwen3.7-max", "Qwen3.7 Max", "anthropic-messages"),
            ModelSpec("qwen3.7-plus", "Qwen3.7 Plus", "anthropic-messages"),
            ModelSpec("qwen3.6-plus", "Qwen3.6 Plus", "anthropic-messages"),
        ),
    ),
)

_PROVIDERS_BY_ID = {provider.id: provider for provider in PROVIDERS}
_LEGACY_MODELS = {
    "deepseek-chat": "deepseek/deepseek-chat",
    "deepseek-reasoner": "deepseek/deepseek-reasoner",
}


def get_model_context_window(reference: str) -> int:
    """Operational provider capacity; nominal model capability may be larger.

    Go Flash's 131072 serving ceiling was reproduced on 2026-09-08 by holding
    its 125190-token input fixed and varying output from 5882 to 5883. Another
    earlier route accepted more: this is a conservative serving policy, not a
    claim that all Go backends have the same limit. Nominal V4 remains 1M.
    This is independent of the output budget requested by a runtime profile.
    """
    try:
        model = resolve_model(reference).model
        return min(model.context_window_tokens, model.serving_context_window_tokens or model.context_window_tokens)
    except ValueError:
        return 128_000


def resolve_model(reference: str) -> ResolvedModel:
    """Resolve a qualified reference, accepting legacy bare DeepSeek IDs."""
    value = (reference or "").strip()
    value = _LEGACY_MODELS.get(value, value)

    if "/" not in value:
        raise ValueError(
            f"Unknown model '{reference}'. Use provider/model, for example "
            "opencode-go/deepseek-v4-flash."
        )

    provider_id, model_id = value.split("/", 1)
    provider = _PROVIDERS_BY_ID.get(provider_id)
    if provider is None:
        available = ", ".join(item.id for item in PROVIDERS)
        raise ValueError(f"Unknown provider '{provider_id}'. Available: {available}")

    model = provider.get_model(model_id)
    if model is None and not model_id:
        raise ValueError(
            f"Missing model ID for {provider.display_name}. Use provider/model."
        )
    if model is None:
        # The selected built-in Provider may accept a manually entered model
        # identifier.  Unknown IDs use its documented OpenAI-compatible
        # protocol until an adapter supplies a more specific declaration.
        model = ModelSpec(
            id=model_id,
            display_name=model_id,
            protocol="openai-chat",
        )
    if model.protocol not in {"openai-chat", "anthropic-messages"}:
        raise ValueError(f"Unsupported model protocol '{model.protocol}'.")

    return ResolvedModel(
        reference=f"{provider.id}/{model.id}",
        provider=provider,
        model=model,
    )


def canonical_model_reference(reference: str) -> str:
    return resolve_model(reference).reference
