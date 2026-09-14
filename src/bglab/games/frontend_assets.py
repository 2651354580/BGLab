"""Shared browser assets used by live games and verified replays."""

from __future__ import annotations

from pathlib import Path


ALLOWED_SHARED_UI_ASSETS = frozenset({
    "authority-confirmation.js",
    "action-history.js",
    "draft-lifecycle.js",
    "game-bridge.js",
    "game-bridge.d.ts",
    "game-shell.js",
    "game-shell.d.ts",
    "game-shell.css",
})


def resolve_shared_ui_asset(game_root: Path, requested_name: str) -> Path | None:
    """Resolve one allowlisted shared UI asset without allowing path traversal."""
    if (
        not requested_name
        or "/" in requested_name
        or "\\" in requested_name
        or requested_name in {".", ".."}
        or requested_name not in ALLOWED_SHARED_UI_ASSETS
    ):
        return None
    candidate = game_root.parent / "shared" / "ui" / requested_name
    return candidate if candidate.is_file() else None


def shared_ui_content_type(name: str) -> str:
    if name.endswith(".css"):
        return "text/css; charset=utf-8"
    if name.endswith(".d.ts"):
        return "application/typescript; charset=utf-8"
    return "text/javascript; charset=utf-8"
