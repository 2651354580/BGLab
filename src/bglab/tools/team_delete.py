""

from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from pathlib import Path

from bglab.owned_path import OwnedPathError, owned_child
from bglab.tools.base import Tool

TEAMS_DIR = Path.home() / ".bglab" / "teams"
_DELETE_LOCK = threading.Lock()


def _has_valid_team_marker(team_dir: Path) -> bool:
    if not team_dir.is_dir():
        return False
    config_path = team_dir / "config.json"
    try:
        config_target = config_path.resolve(strict=True)
    except OSError:
        return False
    return (
        config_path.is_file()
        and not config_path.is_symlink()
        and config_target.parent == team_dir
    )


def _restore_quarantined_team(quarantine: Path, team_dir: Path) -> None:
    """Best-effort rollback without replacing a concurrently-created path."""
    if not os.path.lexists(quarantine) or os.path.lexists(team_dir):
        return
    try:
        os.rename(quarantine, team_dir)
    except OSError:
        return


def team_delete(name: str) -> dict:
    try:
        team_dir = owned_child(TEAMS_DIR, name)
    except OwnedPathError as exc:
        return {"error": f"Invalid team name: {exc}"}

    with _DELETE_LOCK:
        if not team_dir.exists():
            return {"error": f"Team '{name}' does not exist"}
        if not team_dir.is_dir():
            return {"error": f"Team '{name}' is not a directory"}
        if not _has_valid_team_marker(team_dir):
            return {"error": f"Team '{name}' has no valid config.json marker"}

        try:
            quarantine = owned_child(TEAMS_DIR, f".deleting-{uuid.uuid4().hex}")
            os.rename(team_dir, quarantine)
        except (OSError, OwnedPathError) as exc:
            return {"error": f"Failed to isolate team '{name}' for deletion: {exc}"}

        try:
            quarantine = owned_child(TEAMS_DIR, quarantine.name)
            if not _has_valid_team_marker(quarantine):
                _restore_quarantined_team(quarantine, team_dir)
                return {
                    "error": f"Team '{name}' changed during deletion; deletion aborted",
                }
            shutil.rmtree(quarantine)
        except (OSError, OwnedPathError) as exc:
            _restore_quarantined_team(quarantine, team_dir)
            return {"error": f"Failed to delete team '{name}': {exc}"}
    return {"team_name": name, "deleted": True}


def _team_delete_call(args: dict) -> str:
    name = args.get("team_name", "")
    if not name:
        return "Error: team_name is required"
    result = team_delete(name)
    if "error" in result:
        return result["error"]
    return json.dumps(result, ensure_ascii=False)


TeamDeleteTool = Tool(
    name="TeamDelete",
    searchHint="delete agent team",
    description="Delete an agent team and all its data.",
    prompt="Deletes a team. All teammates must be shut down first.",
    parameters={
        "type": "object",
        "properties": {
            "team_name": {"type": "string", "description": "Team name to delete"},
        },
        "required": ["team_name"],
    },
    call=_team_delete_call,
    is_read_only=False,
)
