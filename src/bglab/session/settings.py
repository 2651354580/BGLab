""

from __future__ import annotations

import json
import copy
import unicodedata
from pathlib import Path

from bglab.llm.provider_slots import sanitize_provider_slots

SETTINGS_FILE = ".bglab"
SETTINGS_NAME = "settings.json"

DEFAULTS = {
    "player_nickname": "Human",
    "model": "deepseek-chat",
    "provider_slots": {
        "primary": {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "baseUrl": "https://api.deepseek.com/v1",
            "enabled": True,
        },
        "standby": None,
        "maxAttempts": 2,
    },
    "permission_mode": "default",
    "language": None,
    "feature_flags": {
        "deferred_tools_delta": False,
        "agent_listing_delta": False,
        "mcp_instructions_delta": False,
        "show_system_prompt": False,
        "show_user_context": False,
        "show_messages": False,
        "mcp_instructions_enabled": False,
        "memory_section_enabled": True,
        "deferred_tools_enabled": "auto",
        "show_token_usage": True,
        "show_turn_count": True,
    },
    "permission_rules": {
        "deny": {},
        "allow": {},
        "ask": {},
    },
}


def _settings_path() -> Path:
    return Path.home() / SETTINGS_FILE / SETTINGS_NAME


def normalize_player_nickname(value: object) -> str:
    """Validate a single-line display name without interpreting it as markup."""
    if not isinstance(value, str):
        raise ValueError("昵称必须是文字。")
    if any(unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"} for char in value):
        raise ValueError("昵称不能包含换行或控制字符。")
    name = value.strip()
    if not name or len(name) > 40:
        raise ValueError("昵称请使用 1–40 个字符。")
    return name


def load_player_nickname() -> str:
    """Read the global nickname at new-game creation, including after a rename."""
    try:
        return normalize_player_nickname(load_settings().get("player_nickname"))
    except ValueError:
        return "Human"


def _project_settings_path(cwd: str | None = None) -> Path | None:
    """Project-level settings path: {cwd}/.bglab/settings.json"""
    if not cwd:
        return None
    return Path(cwd) / SETTINGS_FILE / SETTINGS_NAME


def _load_file(path: Path) -> dict:
    """Load JSON from a file path, return {} on failure."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _merge_into(base: dict, overlay: dict, keys: list[str]) -> None:
    """Merge overlay values into base for given keys."""
    for k in keys:
        if k in overlay and overlay[k] is not None:
            if (
                k == "provider_slots"
                and isinstance(base.get(k), dict)
                and isinstance(overlay[k], dict)
            ):
                merged = copy.deepcopy(base[k])
                for slot_key, slot_value in overlay[k].items():
                    # A slot is an identity-bearing unit.  Replacing it as a
                    # whole prevents a project Provider change from inheriting
                    # the user's old Provider URL or credential metadata.
                    merged[slot_key] = copy.deepcopy(slot_value)
                base[k] = merged
            else:
                base[k] = copy.deepcopy(overlay[k])


def _safe_data(data: dict) -> dict:
    """Drop secret-like provider slot fields before merging or persisting."""
    value = copy.deepcopy(data)
    if "provider_slots" in value:
        value["provider_slots"] = sanitize_provider_slots(value["provider_slots"])
    return value


def _legacy_primary(value: object) -> dict[str, object] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    reference = value.strip()
    if "/" in reference:
        provider, model = reference.split("/", 1)
    else:
        provider, model = "deepseek", reference
    if not provider.strip() or not model.strip() or "/" in model:
        return None
    return {"provider": provider.strip(), "model": model.strip()}


def _sync_model_to_primary(settings: dict) -> None:
    slots = settings.get("provider_slots")
    if not isinstance(slots, dict):
        return
    primary = slots.get("primary")
    if not isinstance(primary, dict):
        return
    provider = primary.get("provider")
    model = primary.get("model")
    if isinstance(provider, str) and provider.strip() and isinstance(model, str) and model.strip():
        settings["model"] = f"{provider.strip()}/{model.strip()}"


def load_settings(cwd: str | None = None) -> dict:
    """Load user + project settings. Project settings override user defaults."""
    settings = copy.deepcopy(DEFAULTS)

    # Load user settings (global)
    user_path = _settings_path()
    user_data = _safe_data(_load_file(user_path))
    if "provider_slots" not in user_data:
        legacy = _legacy_primary(user_data.get("model"))
        if legacy is not None:
            user_data["provider_slots"] = {"primary": legacy}
    _merge_into(settings, user_data, list(DEFAULTS.keys()))

    # Load project settings (per-directory, override user)
    proj_path = _project_settings_path(cwd)
    if proj_path:
        proj_data = _safe_data(_load_file(proj_path))
        if "provider_slots" not in proj_data:
            legacy = _legacy_primary(proj_data.get("model"))
            if legacy is not None:
                proj_data["provider_slots"] = {"primary": legacy}
        _merge_into(settings, proj_data, list(DEFAULTS.keys()))

    _sync_model_to_primary(settings)
    return settings


def save_setting(key: str, value: object, cwd: str | None = None) -> None:
    """Persist a setting to the project-level settings file if cwd is given,
    otherwise to user-level settings."""
    if cwd:
        path = _project_settings_path(cwd)
    else:
        path = _settings_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _load_file(path)
    if key == "provider_slots":
        current[key] = sanitize_provider_slots(value)
    else:
        current[key] = copy.deepcopy(value)
    if key == "model":
        legacy = _legacy_primary(value)
        if legacy is not None:
            slots = sanitize_provider_slots(current.get("provider_slots"))
            if slots is None:
                slots = copy.deepcopy(DEFAULTS["provider_slots"])
            slots["primary"] = {
                **(slots.get("primary") or {}),
                **legacy,
            }
            current["provider_slots"] = slots
    elif key == "provider_slots":
        _sync_model_to_primary(current)
    path.write_text(json.dumps(current, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def save_settings(settings: dict, cwd: str | None = None) -> None:
    """Persist full settings dict to project-level or user-level settings file."""
    if cwd:
        path = _project_settings_path(cwd)
    else:
        path = _settings_path()
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    value = copy.deepcopy(settings)
    if "provider_slots" in value:
        value["provider_slots"] = sanitize_provider_slots(value["provider_slots"])
        _sync_model_to_primary(value)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
