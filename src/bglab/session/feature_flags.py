""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any


def _get_type_str(ftype: Any) -> str:
    """Normalize field type to string, handling PEP 563 string annotations."""
    if hasattr(ftype, '__name__'):
        return ftype.__name__
    return str(ftype)


def _env_flag(name: str, default: bool) -> bool:
    """Check BGLAB_FF_<NAME> env var. '1'/'true'/'yes' = true, '0'/'false'/'no' = false."""
    val = os.environ.get(f"BGLAB_FF_{name.upper()}")
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")


@dataclass
class FeatureFlags:
    """All feature flags in one place. One source of truth."""

    
    deferred_tools_delta: bool = False
    ""

    agent_listing_delta: bool = False
    ""

    mcp_instructions_delta: bool = False
    ""

    # ── UI / Debug toggles ──
    show_system_prompt: bool = False
    """Show the assembled system prompt in terminal output (via /show command).
    Separate from the /show infrastructure — just controls whether content renders."""

    show_user_context: bool = False
    """Show user context (CLAUDE.md + MEMORY.md index) in terminal output."""

    show_messages: bool = False
    """Show messages sent to LLM in terminal output (for debugging)."""

    # ── Prompt section toggles ──
    mcp_instructions_enabled: bool = False
    """Include MCP instructions only when a real MCP manager is installed."""

    memory_section_enabled: bool = True
    """Include auto-memory behavior guide + MEMORY.md index in system prompt."""

    # ── Tool toggles ──
    deferred_tools_enabled: str = "auto"
    """Deferred tool search mode: 'auto' | 'on' | 'off'.
    'off' = all tools always visible. 'on' = always defer. 'auto' = threshold-based."""

    # ── Display toggles ──
    show_token_usage: bool = True
    """Show token usage after each turn."""

    show_turn_count: bool = True
    """Show turn count in status line."""

    # ── BG (browser board games) ──
    bg_enabled: bool = True
    """Enable the persistent browser game runtime and /bg lifecycle commands."""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> FeatureFlags:
        """Create from settings dict (with env var overrides)."""
        if not data:
            data = {}
        ff = cls()
        for f in fields(cls):
            if f.name in data and data[f.name] is not None:
                setattr(ff, f.name, data[f.name])
        # Env var overrides take precedence
        for f in fields(cls):
            if f.type is bool:
                setattr(ff, f.name, _env_flag(f.name, getattr(ff, f.name)))
        return ff

    def to_dict(self) -> dict[str, Any]:
        """Serialize to settings dict."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

    def toggle(self, flag_name: str) -> bool | None:
        """Toggle a boolean flag. Returns new value or None if flag not found."""
        for f in fields(self):
            if f.name == flag_name:
                type_str = _get_type_str(f.type)
                if type_str == 'bool':
                    new_val = not getattr(self, f.name)
                    setattr(self, f.name, new_val)
                    return new_val
                else:
                    return None  # Not a boolean flag
        return None

    def set(self, flag_name: str, value: Any) -> bool:
        """Set a flag value. Returns True if flag was found."""
        for f in fields(self):
            if f.name == flag_name:
                type_str = _get_type_str(f.type)
                if type_str == 'bool' and isinstance(value, str):
                    value = value.lower() in ("1", "true", "yes", "on")
                setattr(self, f.name, value)
                return True
        return False

    def list_all(self) -> list[dict[str, Any]]:
        """List all flags with current values."""
        result = []
        for f in fields(self):
            ftype = f.type
            if hasattr(ftype, '__name__'):
                ftype = ftype.__name__
            else:
                ftype = str(ftype)
            result.append({
                "name": f.name,
                "value": getattr(self, f.name),
                "type": ftype,
            })
        return result


# ── Singleton (loaded once per session) ──
_current_flags: FeatureFlags | None = None


def get_feature_flags() -> FeatureFlags:
    """Get current feature flags (lazy init from settings)."""
    global _current_flags
    if _current_flags is None:
        from bglab.session.settings import load_settings
        settings = load_settings()
        ff_data = settings.get("feature_flags", {})
        if isinstance(ff_data, dict):
            _current_flags = FeatureFlags.from_dict(ff_data)
        else:
            _current_flags = FeatureFlags()
    return _current_flags


def set_feature_flags(flags: FeatureFlags) -> None:
    """Replace current feature flags and persist to settings."""
    global _current_flags
    _current_flags = flags
    from bglab.session.settings import save_setting
    save_setting("feature_flags", flags.to_dict())


def reload_feature_flags() -> FeatureFlags:
    """Force re-read from settings (e.g., after external change)."""
    global _current_flags
    _current_flags = None
    return get_feature_flags()
