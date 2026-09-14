""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from bglab.owned_path import OwnedPathError, normalize_owned_component, owned_child
from bglab.tools.base import Tool

TEAMS_DIR = Path.home() / ".bglab" / "teams"
_CONFIG_LOCK = threading.RLock()


def _safe_team_name(name: str) -> str:
    return normalize_owned_component(name)


def _config_path(name: str) -> Path:
    return owned_child(TEAMS_DIR, name) / "config.json"


def _atomic_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".config.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _make_lead_agent_id(team_name: str) -> str:
    safe = normalize_owned_component(team_name, max_length=50)
    return f"lead@{safe}"


def team_create(name: str, description: str = "", model: str = "deepseek-chat") -> dict:
    try:
        team_dir = owned_child(TEAMS_DIR, name)
    except OwnedPathError as exc:
        return {"error": f"Invalid team name: {exc}", "team_name": name}
    team_dir.mkdir(parents=True, exist_ok=True)

    config_path = team_dir / "config.json"
    if config_path.exists():
        return {"error": f"Team '{name}' already exists", "team_name": name}

    lead_id = _make_lead_agent_id(name)
    config = {
        "name": name,
        "description": description,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "leadAgentId": lead_id,
        "leadModel": model,
        "members": [
            {
                "agentId": lead_id,
                "name": "leader",
                "joinedAt": datetime.now(timezone.utc).isoformat(),
                "isActive": True,
            }
        ],
    }

    _atomic_config(config_path, config)
    return {"team_name": name, "team_dir": str(team_dir), "lead_agent_id": lead_id}


def ensure_team(
    name: str,
    description: str = "",
    model: str = "deepseek-chat",
    *,
    metadata: dict | None = None,
) -> dict:
    """Create or reopen a durable team without destroying member identity."""
    with _CONFIG_LOCK:
        try:
            path = _config_path(name)
        except OwnedPathError as exc:
            return {"error": f"Invalid team name: {exc}", "team_name": name}
        if not path.exists():
            created = team_create(name, description, model)
            if "error" in created:
                return created
        data = json.loads(path.read_text(encoding="utf-8"))
        data["description"] = description or data.get("description", "")
        if metadata:
            data["metadata"] = {**data.get("metadata", {}), **metadata}
        _atomic_config(path, data)
        return {"team_name": name, "team_dir": str(path.parent), "config": data}


def register_teammate(
    team_name: str,
    name: str,
    *,
    model: str = "deepseek-chat",
    session_id: str = "",
    metadata: dict | None = None,
) -> dict:
    """Register or reactivate one persistent in-process teammate."""
    with _CONFIG_LOCK:
        try:
            path = _config_path(team_name)
        except OwnedPathError as exc:
            return {"error": f"Invalid team name: {exc}"}
        if not path.exists():
            return {"error": f"Team '{team_name}' does not exist"}
        data = json.loads(path.read_text(encoding="utf-8"))
        now = datetime.now(timezone.utc).isoformat()
        members = data.setdefault("members", [])
        member = next((item for item in members if item.get("name") == name), None)
        if member is None:
            member = {
                "agentId": f"{name}@{_safe_team_name(team_name)}",
                "name": name,
                "joinedAt": now,
            }
            members.append(member)
        member.update({
            "isActive": True,
            "model": model,
            "sessionId": session_id,
            "runtime": "in_process",
            "updatedAt": now,
        })
        if metadata:
            member["metadata"] = {**member.get("metadata", {}), **metadata}
        _atomic_config(path, data)
        return dict(member)


def set_teammates_active(team_name: str, active: bool) -> dict:
    """Activate/deactivate non-leader members while retaining durable records."""
    with _CONFIG_LOCK:
        try:
            path = _config_path(team_name)
        except OwnedPathError as exc:
            return {"error": f"Invalid team name: {exc}"}
        if not path.exists():
            return {"error": f"Team '{team_name}' does not exist"}
        data = json.loads(path.read_text(encoding="utf-8"))
        now = datetime.now(timezone.utc).isoformat()
        changed = 0
        for member in data.get("members", []):
            if member.get("name") == "leader":
                continue
            member["isActive"] = active
            member["updatedAt"] = now
            changed += 1
        _atomic_config(path, data)
        return {"team_name": team_name, "active": active, "changed": changed}


def _team_create_call(args: dict) -> str:
    name = args.get("team_name", "")
    desc = args.get("description", "")
    if not name:
        return "Error: team_name is required"
    result = team_create(name, desc)
    if "error" in result:
        return result["error"]
    return json.dumps(result, ensure_ascii=False)


TeamCreateTool = Tool(
    name="TeamCreate",
    searchHint="create agent team",
    description="Create a new agent team with a leader.",
    prompt=(
        "Creates a new team for multi-agent collaboration. "
        "The team has one leader (the caller) and can have multiple teammates added later."
    ),
    parameters={
        "type": "object",
        "properties": {
            "team_name": {"type": "string", "description": "Unique team name"},
            "description": {"type": "string", "description": "Short description of the team's purpose", "default": ""},
        },
        "required": ["team_name"],
    },
    call=_team_create_call,
    is_read_only=False,
)
