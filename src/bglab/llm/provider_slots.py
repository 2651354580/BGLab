"""Non-secret Provider Slot configuration and status helpers."""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse

from bglab.llm.providers import PROVIDERS

SlotId = Literal["primary", "standby"]

_PROVIDERS_BY_ID = {provider.id: provider for provider in PROVIDERS}


@dataclass(frozen=True)
class ProviderSlot:
    """An immutable, non-secret request identity for one Provider slot."""

    id: SlotId
    provider: str
    model: str
    base_url: str
    credential_env: str
    enabled: bool
    chat_stream: bool = True

    def __post_init__(self) -> None:
        if type(self.chat_stream) is not bool:
            raise ValueError("chatStream must be a boolean")

    @property
    def reference(self) -> str:
        """Return the qualified provider/model reference.

        Model IDs may be manually entered.  Provider validation happens when a
        slot is loaded, while model catalog membership is intentionally not a
        requirement for this non-secret domain object.
        """

        return f"{self.provider}/{self.model}"

    def to_settings(self) -> dict[str, object]:
        """Serialize only non-secret slot fields."""

        settings = {
            "provider": self.provider,
            "model": self.model,
            "baseUrl": self.base_url,
            "enabled": self.enabled,
        }
        if not self.chat_stream:
            settings["chatStream"] = False
        return settings


@dataclass(frozen=True)
class ProviderSlotPolicy:
    """Primary slot plus an optional standby and bounded attempt budget."""

    primary: ProviderSlot
    standby: ProviderSlot | None
    max_attempts: int = 2

    def to_settings(self) -> dict[str, object]:
        """Serialize policy metadata without credentials."""

        return {
            "primary": self.primary.to_settings(),
            "standby": self.standby.to_settings() if self.standby else None,
            "maxAttempts": self.max_attempts,
        }


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _provider(provider_id: Any) -> Any:
    provider_id = _required_text(provider_id, "provider")
    provider = _PROVIDERS_BY_ID.get(provider_id)
    if provider is None:
        available = ", ".join(sorted(_PROVIDERS_BY_ID))
        raise ValueError(
            f"Unknown provider '{provider_id}'. Available: {available}",
        )
    return provider


def _model(model_id: Any) -> str:
    model_id = _required_text(model_id, "model")
    if "/" in model_id:
        raise ValueError("model must not contain '/' when provider is selected")
    return model_id


def _base_url(value: Any, provider: Any) -> str:
    if value is None:
        return provider.openai_base_url
    value = _required_text(value, "baseUrl")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("baseUrl must be an absolute http(s) URL")
    return value


def _enabled(value: Any, *, slot_id: SlotId) -> bool:
    if value is None:
        return slot_id == "primary"
    if not isinstance(value, bool):
        raise ValueError("enabled must be a boolean")
    if slot_id == "primary" and not value:
        raise ValueError("primary slot must be enabled")
    return value


def _legacy_reference(value: Any) -> tuple[str, str]:
    reference = _required_text(value, "model")
    if "/" in reference:
        provider, model = reference.split("/", 1)
        return _required_text(provider, "provider"), _model(model)
    # Existing settings stored bare DeepSeek model IDs.  Keep that migration
    # path while allowing a manually entered model ID.
    return "deepseek", _model(reference)


def _slot_from_config(
    value: Any,
    *,
    slot_id: SlotId,
    fallback_reference: Any,
    primary_provider: str | None = None,
) -> ProviderSlot:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{slot_id} slot must be an object or null")
    config = dict(value)
    fallback_provider, fallback_model = _legacy_reference(fallback_reference)
    provider_id = config.get("provider", fallback_provider)
    provider = _provider(provider_id)
    model_id = _model(config.get("model", fallback_model))
    base_url = _base_url(config.get("baseUrl"), provider)
    enabled = _enabled(config.get("enabled"), slot_id=slot_id)
    credential_env = provider.api_key_env
    if (
        slot_id == "standby"
        and primary_provider is not None
        and provider.id == primary_provider
    ):
        credential_env = f"{credential_env}_STANDBY"
    return ProviderSlot(
        id=slot_id,
        provider=provider.id,
        model=model_id,
        base_url=base_url,
        credential_env=credential_env,
        enabled=enabled,
        chat_stream=config.get("chatStream", True),
    )


def load_provider_slot_policy(settings: Mapping[str, object]) -> ProviderSlotPolicy:
    """Validate and normalize two non-secret Provider slots.

    The catalog validates Provider IDs and supplies default URLs.  Model IDs
    are deliberately not restricted to the current catalog: the UI supports
    manual model IDs, while a qualified ``provider/model`` reference remains
    unambiguous for later request construction.
    """

    if not isinstance(settings, Mapping):
        raise ValueError("settings must be an object")
    raw_slots = settings.get("provider_slots")
    legacy_reference = settings.get("model", "deepseek-chat")
    if raw_slots is None:
        raw_slots = {}
    if not isinstance(raw_slots, Mapping):
        raise ValueError("provider_slots must be an object")
    primary = _slot_from_config(
        raw_slots.get("primary"),
        slot_id="primary",
        fallback_reference=legacy_reference,
    )
    raw_standby = raw_slots.get("standby")
    standby = None
    if raw_standby is not None:
        standby = _slot_from_config(
            raw_standby,
            slot_id="standby",
            fallback_reference=legacy_reference,
            primary_provider=primary.provider,
        )
    max_attempts = raw_slots.get("maxAttempts", settings.get("maxAttempts", 2))
    if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("maxAttempts must be a positive integer")
    return ProviderSlotPolicy(
        primary=primary,
        standby=standby,
        max_attempts=max_attempts,
    )


def _secret_configured(secret_status: Any, env_name: str) -> bool:
    """Read only a configured/missing bit from a future credential service."""

    value = secret_status
    if isinstance(secret_status, Mapping):
        value = secret_status.get(env_name)
    elif callable(secret_status):
        value = secret_status(env_name)
    if isinstance(value, Mapping):
        value = value.get("configured", False)
    elif hasattr(value, "configured"):
        value = getattr(value, "configured")
    elif isinstance(value, str):
        value = value.lower() in {"configured", "present", "available", "true"}
    return bool(value)


def slot_status(
    policy: ProviderSlotPolicy,
    secret_status: Mapping[str, object] | Any,
) -> dict[str, dict[str, object] | None]:
    """Return public slot metadata and redacted credential status.

    ``secret_status`` may be a mapping or a credential-service callable.  No
    secret value, prefix, length, hash, or response detail is returned.
    """

    primary_configured = _secret_configured(
        secret_status, policy.primary.credential_env,
    )

    def describe(slot: ProviderSlot, *, primary: bool = False) -> dict[str, object]:
        configured = _secret_configured(secret_status, slot.credential_env)
        credential = "configured" if configured else "missing"
        if (
            not primary
            and slot.credential_env == policy.primary.credential_env
            and configured
            and primary_configured
        ):
            credential = "configured_same_as_primary"
        return {
            "id": slot.id,
            "provider": slot.provider,
            "model": slot.model,
            "baseUrl": slot.base_url,
            "enabled": slot.enabled,
            "credential": credential,
            "credentialConfigured": configured,
        }

    return {
        "primary": describe(policy.primary, primary=True),
        "standby": describe(policy.standby) if policy.standby else None,
    }


def sanitize_provider_slots(value: Any) -> dict[str, object] | None:
    """Copy only non-secret provider-slot fields for settings persistence."""

    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("provider_slots must be an object")
    sanitized: dict[str, object] = {}
    for key in ("primary", "standby"):
        slot = value.get(key)
        if slot is None:
            if key in value:
                sanitized[key] = None
            continue
        if not isinstance(slot, Mapping):
            raise ValueError(f"{key} slot must be an object or null")
        sanitized[key] = {
            field: copy.deepcopy(slot[field])
            for field in ("provider", "model", "baseUrl", "enabled", "chatStream")
            if field in slot
        }
    if "maxAttempts" in value:
        sanitized["maxAttempts"] = copy.deepcopy(value["maxAttempts"])
    return sanitized
