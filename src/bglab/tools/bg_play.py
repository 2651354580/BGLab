"""BgPlay — persistent browser game-session lifecycle.

The browser is the authoritative engine. Python owns persistent AI players,
snapshots, reports, and the HTTP/WebSocket lifecycle.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import os
import re
import secrets
import socket
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import websockets
from websockets.exceptions import ConnectionClosed

from bglab.games.registry import (
    GameDefinition,
    GameRegistryError,
    get_game,
    resolve_game,
)
from bglab.games.package_identity import (
    PackageIdentityError,
    ResumePackageIdentityError,
    package_runtime_fingerprint,
    require_resume_package_identity,
)
from bglab.games.authority_worker import AuthorityWorker
from bglab.games.replay import authority_hash
from bglab.games.features import (
    GameAgentProfile,
    GameFeatureProfile,
    RELEASE_GAME_PROFILE,
    RELEASE_CHAT_PROFILE,
)
from bglab.games.frontend_assets import (
    resolve_shared_ui_asset,
    shared_ui_content_type,
)
from bglab.games.persistence import store as store_module
from bglab.games.persistence.store import GameStore
from bglab.game_tui_projection import GamePresentationMetadata
from bglab.llm.provider_slots import ProviderSlotPolicy, load_provider_slot_policy
from bglab.tools.base import Tool, ToolCallResult

_LIVE_FILE = store_module.GAMES_DIR / ".live_events"
_SERVER_THREAD: threading.Thread | None = None
_SERVER_STOP_EVENT = threading.Event()
_SERVER_READY_EVENT = threading.Event()
_GAME_SESSION_DONE_EVENT = threading.Event()
_GAME_SESSION_PREPARED_EVENT = threading.Event()
_SERVER_ERROR: str | None = None
_ACTIVE_GAME_ID: str | None = None
_ACTIVE_SESSION_CAPABILITY: str | None = None
_ACTIVE_RUNTIME = None
_START_IN_PROGRESS = False
_START_CANCEL_EVENT = threading.Event()
_SERVER_LOCK = threading.RLock()
_HTTP_SERVER_CLASS = ThreadingHTTPServer
_GAME_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_BRIDGE_SEND_RE = re.compile(
    rb"(\.\.\.\s*[A-Za-z_$][A-Za-z0-9_$]*\s*,\s*gameId\s*:\s*window\.BG_GAME_ID\s*"
    rb"(?:\|\||\?\?)\s*null)(?!\s*,\s*sessionCapability\s*:)",
)


def _release_ai_turn_owner(
    turn_connections: dict[str, Any],
    pending_ai_turns: list[tuple[int, str, str, Any]],
    turn_id: str,
    target_ws: Any,
) -> None:
    """Release a frame route after admission fails before a provider call."""
    if turn_connections.get(turn_id) is target_ws:
        turn_connections.pop(turn_id, None)
    pending_ai_turns[:] = [
        item for item in pending_ai_turns
        if not (len(item) >= 4 and item[1] == turn_id and item[3] is target_ws)
    ]


def _reserve_ai_turn_owner(
    turn_connections: dict[str, Any],
    turn_id: str,
    target_ws: Any,
) -> bool:
    """Reserve one connection for a DecisionFrame without replacement."""
    if turn_id in turn_connections:
        return False
    turn_connections[turn_id] = target_ws
    return True


def _snapshot_owned_by_another_connection(
    turn_connections: dict[str, Any],
    *,
    pending_turn_id: str | None,
    snapshot_turn_id: str | None,
    target_ws: Any,
) -> bool:
    """Keep all snapshots for an in-flight decision on its owning socket."""
    ordered_turn_ids = (pending_turn_id, snapshot_turn_id)
    for turn_id in ordered_turn_ids:
        if not isinstance(turn_id, str):
            continue
        owner = turn_connections.get(turn_id)
        if owner is not None and owner is not target_ws:
            return True
    # An after-action snapshot usually has the *next* decision id, while its
    # durable pending replay still belongs to the previous id.  Also reject an
    # unrelated tab while any other live frame owner remains registered.
    return any(owner is not target_ws for owner in turn_connections.values())


def _session_url(*, host: str = "localhost", capability: str | None = None) -> str:
    token = _ACTIVE_SESSION_CAPABILITY if capability is None else capability
    if not isinstance(token, str) or not token:
        return f"http://{host}:8080"
    return f"http://{host}:8080/?capability={quote(token, safe='')}"


def _clear_active_session_capability(expected: str | None) -> None:
    """Clear only the capability owned by the finishing server instance."""
    global _ACTIVE_SESSION_CAPABILITY
    if not isinstance(expected, str):
        return
    with _SERVER_LOCK:
        if _ACTIVE_SESSION_CAPABILITY == expected:
            _ACTIVE_SESSION_CAPABILITY = None


def _session_capability_matches(value: Any, expected: str | None) -> bool:
    if not isinstance(value, str) or not isinstance(expected, str):
        return False
    try:
        return secrets.compare_digest(value, expected)
    except (TypeError, UnicodeEncodeError):
        return False


def _inject_bridge_capability(payload: bytes) -> bytes:
    """Make every native bridge send the per-page session capability."""
    return _BRIDGE_SEND_RE.sub(
        rb"\1,sessionCapability:window.BG_SESSION_CAPABILITY||null",
        payload,
    )


def _browser_bridge_retry_script() -> str:
    """Return an indefinite, rate-bounded reconnect safety net.

    The shared bridge already owns exponential reconnect scheduling.  This
    small page-level watchdog covers older/minified bridge shapes without
    racing an already scheduled reconnect and without expiring after a fixed
    number of attempts.
    """
    return (
        'setInterval(function(){'
        'if(typeof Bridge!=="undefined"&&(!Bridge._ws||Bridge._ws.readyState!==1)'
        '&&!Bridge._reconnectTimer){'
        'var _bcConnect=typeof Bridge._tryConnect==="function"?Bridge._tryConnect:'
        'typeof Bridge.init==="function"?Bridge.init:null;'
        'if(_bcConnect){_bcConnect.call(Bridge)}}},3000);'
    )


def _validated_manifest(
    manifest: dict, requested: str,
) -> tuple[str, str, tuple[str, ...], tuple[str, ...], int | None]:
    """Validate presentation-only fields without changing the manifest."""
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    if manifest.get("game_id") != requested:
        raise ValueError("manifest identity mismatch")
    title = manifest.get("game_title")
    status = manifest.get("status")
    player_types = manifest.get("player_types")
    players = manifest.get("players")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("game_title must be a non-empty string")
    if not isinstance(status, str) or not status.strip():
        raise ValueError("status must be a non-empty string")
    if (
        not isinstance(player_types, list)
        or not player_types
        or any(
            not isinstance(item, str) or item not in {"human", "ai"}
            for item in player_types
        )
    ):
        raise ValueError("player_types must contain only human/ai")
    # The authoritative writer can persist an empty human display name.  Seat
    # type and list length, not truthiness of the label, determine validity.
    if (
        not isinstance(players, list)
        or len(players) != len(player_types)
        or any(not isinstance(item, str) for item in players)
    ):
        raise ValueError("players must match player_types")
    raw_port = manifest.get("http_port")
    if raw_port is None:
        port = None
    elif (
        isinstance(raw_port, bool)
        or not isinstance(raw_port, int)
        or not 1 <= raw_port <= 65535
    ):
        raise ValueError("http_port must be 1..65535")
    else:
        port = raw_port
    return title, status, tuple(player_types), tuple(players), port


def get_presentation_metadata(
    game_id: str | None = None,
) -> GamePresentationMetadata:
    """Return exact, side-effect-free metadata for one persisted game.

    Identity, path containment, and manifest shape are checked before the
    ``GameStore`` constructor.  This keeps an invalid presentation request
    from creating a directory or touching lifecycle state.
    """
    with _SERVER_LOCK:
        active_game_id = _ACTIVE_GAME_ID
        active_capability = _ACTIVE_SESSION_CAPABILITY
        requested = active_game_id if game_id is None else game_id
    if requested is None:
        raise ValueError("没有活动对局")
    if not isinstance(requested, str) or _GAME_ID_RE.fullmatch(requested) is None:
        raise ValueError("非法 game_id")

    root = Path(store_module.GAMES_DIR).resolve()
    candidate = (root / requested).resolve()
    if candidate == root or root not in candidate.parents:
        raise ValueError("game_id 越界")
    manifest_path = candidate / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("manifest 不存在")
    manifest_real_path = manifest_path.resolve()
    if manifest_real_path == root or root not in manifest_real_path.parents:
        raise ValueError("manifest 路径越界")
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError(f"manifest 无法读取: {manifest_path}") from exc
    if not isinstance(raw_manifest, dict):
        raise ValueError("manifest 必须是 object")

    # Construction is deliberately after all path and raw-file checks.  A
    # valid candidate already exists, so this constructor cannot create an
    # escape path and only supplies the normal authoritative reader.
    store = GameStore(requested)
    if Path(store_module.GAMES_DIR).resolve() != root:
        raise ValueError("GAMES_DIR changed while reading metadata")
    if Path(store.dir).resolve() != candidate:
        raise ValueError("game directory changed while reading metadata")
    try:
        manifest = store.read_manifest()
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError(f"manifest 无法读取: {manifest_path}") from exc
    current_manifest_path = (Path(store.dir) / "manifest.json").resolve()
    if current_manifest_path != manifest_real_path or (
        current_manifest_path == root or root not in current_manifest_path.parents
    ):
        raise ValueError("manifest 路径改变或越界")
    if manifest != raw_manifest:
        raise ValueError("manifest 在读取期间发生变化")
    title, status, player_types, players, http_port = _validated_manifest(
        manifest, requested,
    )
    url = None
    if http_port is not None:
        url = f"http://localhost:{http_port}"
        if (
            requested == active_game_id
            and isinstance(active_capability, str)
            and active_capability
        ):
            url += f"/?capability={quote(active_capability, safe='')}"
    return GamePresentationMetadata(
        game_id=requested,
        game_dir=candidate,
        manifest_path=manifest_path,
        report_path=candidate / "turn_reports.jsonl",
        title=title,
        status=status,
        http_port=http_port,
        url=url,
        player_types=player_types,
        players=players,
    )


def _resolve_game_definition(engine: str) -> GameDefinition:
    """Accept a model's game id or a registered human-facing alias."""
    try:
        return get_game(engine)
    except GameRegistryError:
        return resolve_game(engine)


def _content_fingerprint(*paths: Path | None) -> str:
    digest = hashlib.sha256()
    for path in paths:
        if path is not None and path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _finished_result_signature(value: dict) -> tuple:
    """Extract the score-bearing fields used to compare terminal results."""
    players = tuple(
        (
            int(player["seat"]),
            int(player["total"]),
            tuple(
                (str(component["id"]), int(component["value"]))
                for component in player["components"]
            ),
        )
        for player in sorted(value["players"], key=lambda item: int(item["seat"]))
    )
    tie_breakers = tuple(
        (
            str(item["id"]),
            tuple(item["values"]),
            item["winner"],
        )
        for item in value["tieBreakers"]
    )
    return (
        value["winner"],
        tuple(value["winners"]),
        tie_breakers,
        players,
    )


def _validate_finished_result_store(store, definition) -> dict:
    """Validate one completed store without writing or starting a runtime.

    A result session is only safe when the persisted manifest, terminal
    snapshot, and the complete replay archive agree.  The replay archive is
    intentionally re-verified turn-by-turn, then the package adapter derives
    the terminal result again; a stored ``finalResult`` is never trusted on
    its own.
    """
    from bglab.games.adapter_process import AdapterProcess
    from bglab.games.final_result import validate_final_result
    from bglab.games.replay import authority_hash, verify_replay_turn

    manifest = store.read_manifest()
    if (
        manifest.get("game_id") != store.game_id
        or manifest.get("status") != "finished"
        or not manifest.get("finished_at")
    ):
        raise ValueError("finished manifest is incomplete")
    if manifest.get("engine") != definition.id:
        raise ValueError("finished manifest engine does not match the requested game")
    player_types = manifest.get("player_types")
    players = manifest.get("players")
    if (
        not isinstance(player_types, list)
        or not 2 <= len(player_types) <= 4
        or any(item not in {"human", "ai"} for item in player_types)
        or not isinstance(players, list)
        or len(players) != len(player_types)
    ):
        raise ValueError("finished manifest lineup is invalid")
    try:
        recorded_result = validate_final_result(
            manifest["final_result"], len(player_types),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("finished manifest final_result is invalid") from exc
    if manifest.get("final_scores") != [
        player["total"]
        for player in sorted(recorded_result["players"], key=lambda item: item["seat"])
    ]:
        raise ValueError("finished manifest final_scores disagree with final_result")
    if manifest.get("winner") != recorded_result.get("winner"):
        raise ValueError("finished manifest winner disagrees with final_result")

    snapshot = store.read_snapshot()
    state = snapshot.get("state") if isinstance(snapshot, dict) else None
    wrapper = state.get("wrapper", state.get("st", {})) if isinstance(state, dict) else {}
    if (
        not isinstance(state, dict)
        or not isinstance(wrapper, dict)
        or wrapper.get("phase") not in {"finished", "gameover"}
        or snapshot.get("turn_id") != manifest.get("last_confirmed_turn_id")
        or (
            "winner" in wrapper
            and wrapper.get("winner") != manifest.get("winner")
        )
    ):
        raise ValueError("terminal snapshot is missing or disagrees with manifest")

    replay_dir = Path(store.dir) / "replay"
    replay_manifest_path = replay_dir / "manifest.json"
    try:
        replay_manifest = json.loads(replay_manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError("replay manifest is unreadable") from exc
    if not isinstance(replay_manifest, dict):
        raise ValueError("replay manifest is incomplete")
    if replay_manifest.get("verified") is not True:
        raise ValueError("replay manifest is not verified")
    if replay_manifest.get("gameId") != store.game_id:
        raise ValueError("replay manifest gameId disagrees with the store")
    if replay_manifest.get("engine") != definition.id:
        raise ValueError("replay manifest engine does not match the game")
    allowed_versions = set(definition.restore_versions or ())
    allowed_versions.add(definition.snapshot_version)
    if replay_manifest.get("snapshotVersion") not in allowed_versions:
        raise ValueError("replay manifest snapshotVersion is unsupported")
    manifest_package = manifest.get("game_package_fingerprint")
    if not isinstance(manifest_package, str) or not manifest_package:
        execution_identity = manifest.get("execution_identity")
        manifest_package = (
            execution_identity.get("package")
            if isinstance(execution_identity, dict)
            else None
        )
    replay_package = replay_manifest.get("packageFingerprint")
    if (
        not isinstance(manifest_package, str)
        or not manifest_package
        or not isinstance(replay_package, str)
        or replay_package != manifest_package
    ):
        raise ValueError("replay manifest packageFingerprint disagrees with the store")
    try:
        turn_count = int(replay_manifest.get("turnCount", -1))
        frame_count = int(replay_manifest.get("frameCount", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("replay manifest counts are invalid") from exc
    if turn_count < 1 or frame_count != turn_count + 1:
        raise ValueError("replay manifest is incomplete")

    # verify_replay_turn performs clone dispatch, effect comparison, and
    # before/after authority hash checks.  Run it for every persisted turn so
    # an intermediate frame corruption cannot be hidden by a good terminal
    # frame.
    try:
        for index in range(turn_count):
            verify_replay_turn(store.game_id, index)
    except Exception as exc:
        raise ValueError(f"replay turn {index} failed verification: {exc}") from exc

    final_index = frame_count - 1
    frame_path = replay_dir / "frames" / f"{final_index:04d}.json"
    try:
        frame = json.loads(frame_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise ValueError("terminal replay frame is unreadable") from exc
    replay_state = frame.get("state") if isinstance(frame, dict) else None
    replay_wrapper = (
        replay_state.get("wrapper", replay_state.get("st", {}))
        if isinstance(replay_state, dict) else {}
    )
    if (
        not isinstance(replay_state, dict)
        or frame.get("index") != final_index
        or frame.get("authorityHash") != authority_hash(replay_state)
        or replay_wrapper != wrapper
    ):
        raise ValueError("terminal replay frame is invalid or disagrees with snapshot")

    # Always derive the terminal result from the package adapter.  This keeps
    # a forged archive ``finalResult`` or manifest score from becoming a
    # trusted result-only session.
    adapter = AdapterProcess(definition, request_timeout_s=30.0)
    try:
        adapter.restore(replay_state)
        adapter_terminal = adapter.snapshot()
        if authority_hash(adapter_terminal) != authority_hash(replay_state):
            raise ValueError("adapter terminal snapshot disagrees with replay frame")
        derived_result = validate_final_result(
            adapter.final_result(), len(player_types),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("adapter-derived terminal final_result is invalid") from exc
    finally:
        adapter.close()
    if _finished_result_signature(derived_result) != _finished_result_signature(recorded_result):
        raise ValueError("adapter-derived final_result disagrees with manifest final_result")
    archive_result = replay_manifest.get("finalResult")
    if archive_result is not None:
        try:
            archive_result = validate_final_result(archive_result, len(player_types))
        except (TypeError, ValueError) as exc:
            raise ValueError("replay finalResult is invalid") from exc
        if _finished_result_signature(archive_result) != _finished_result_signature(derived_result):
            raise ValueError("replay finalResult disagrees with adapter-derived result")
    return {
        "manifest": copy.deepcopy(manifest),
        "snapshot": copy.deepcopy(snapshot),
        "replayManifest": copy.deepcopy(replay_manifest),
        "finalResult": copy.deepcopy(derived_result),
        "playerTypes": list(player_types),
        "players": list(players),
    }


class _FrozenResultStore:
    """A frozen read-only facade for a completed result session."""

    def __init__(self, store, manifest: dict, snapshot: dict) -> None:
        self.game_id = store.game_id
        self.dir = store.dir
        self._manifest = copy.deepcopy(manifest)
        self._snapshot = copy.deepcopy(snapshot)

    def read_manifest(self) -> dict:
        return copy.deepcopy(self._manifest)

    def read_snapshot(self) -> dict:
        return copy.deepcopy(self._snapshot)

    def __getattr__(self, name):
        raise AttributeError(f"completed result store does not expose {name!r}")


class _FinishedResultRuntime:
    """Minimal immutable runtime used by a completed result-only session."""

    def __init__(
        self,
        game_id: str,
        player_types: list[str],
        player_names: list[str],
        store: _FrozenResultStore,
        final_result: dict,
    ) -> None:
        self.game_id = game_id
        self.player_types = list(player_types)
        self.names = list(player_names)
        self.player_names = list(player_names)
        self.store = store
        self.final_result = copy.deepcopy(final_result)
        self.status = "finished"
        self.browser_chat_callback = None
        self.last_confirmed_turn_id = store.read_snapshot().get("turn_id")

    def set_action_validator(self, _validator) -> None:
        return None

    def route_human_chat(self, *_args, **_kwargs):
        raise RuntimeError("result-only session does not accept chat")

    def consume_stale_turn_retries(self) -> list:
        return []

    async def stop(self) -> None:
        return None


def _frontend_confirmation(
    definition_id: str, turn_id: str, result: dict,
) -> tuple[str | None, str | None]:
    """Require browser-applied authority proof for every packaged game."""
    frontend_turn_id = result.get("frontendTurnId")
    frontend_state_hash = result.get("frontendStateHash")
    if (
        not isinstance(frontend_turn_id, str)
        or not frontend_turn_id
        or not isinstance(frontend_state_hash, str)
        or len(frontend_state_hash) != 64
        or any(character not in "0123456789abcdef" for character in frontend_state_hash)
    ):
        raise RuntimeError("authoritative frontend confirmation is missing or stale")
    # Splendor always advances its public decision ID. White Castle can finish
    # on the same ID; its changed after-state hash and exact committed
    # transaction are verified by the runtime pending-replay confirmation.
    if definition_id == "splendor" and frontend_turn_id == turn_id:
        raise RuntimeError("authoritative frontend confirmation is missing or stale")
    return frontend_turn_id, frontend_state_hash


def _ensure_game_team(
    game_id: str,
    player_types: list[str],
    player_names: list[str],
    *,
    model: str = "deepseek-chat",
    engine: str = "splendor",
) -> str:
    """Create/reactivate the Agent Team records owned by one game session."""
    from bglab.tools.team_create import ensure_team, register_teammate

    team_name = f"bg-{game_id}"
    result = ensure_team(
        team_name,
        f"Persistent AI players for board game {game_id}",
        model,
        metadata={"mode": "game", "game_id": game_id, "engine": engine},
    )
    if "error" in result:
        raise RuntimeError(result["error"])
    for pid, player_type in enumerate(player_types):
        if player_type != "ai":
            continue
        member = register_teammate(
            team_name,
            f"ai-p{pid}",
            model=model,
            session_id=f"game-{game_id}-p{pid}",
            metadata={
                "mode": "game",
                "game_id": game_id,
                "pid": pid,
                "display_name": player_names[pid],
            },
        )
        if "error" in member:
            raise RuntimeError(member["error"])
    return team_name


def _deactivate_game_team(team_name: str | None) -> None:
    if not team_name:
        return
    from bglab.tools.team_create import set_teammates_active

    result = set_teammates_active(team_name, False)
    if "error" in result:
        _write_live_event({
            "type": "team_shutdown_error", "team_name": team_name,
            "error": result["error"],
        })


def _write_live_event(event: dict) -> None:
    """Best-effort telemetry for the TUI; never participates in gameplay decisions."""
    try:
        _LIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_LIVE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def prepare_blocking_bg_tool_call() -> None:
    """Prepare one TUI-launched BgPlay wait before controls are exposed."""
    _GAME_SESSION_DONE_EVENT.clear()
    _START_CANCEL_EVENT.clear()
    _GAME_SESSION_PREPARED_EVENT.set()


def _owned_port_owners(ports: list[int]) -> dict[int, list[int]]:
    """Return LISTENING PIDs for ports owned by the current BGLab session."""
    owners: dict[int, list[int]] = {port: [] for port in ports}
    try:
        import psutil
    except ImportError:
        return owners
    try:
        connections = psutil.net_connections(kind="tcp")
    except Exception:
        return owners
    for connection in connections:
        if connection.status != psutil.CONN_LISTEN or not connection.laddr:
            continue
        port = int(connection.laddr.port)
        if port not in owners or connection.pid is None:
            continue
        owners[port].append(int(connection.pid))
    return {port: sorted(set(pids)) for port, pids in owners.items()}


def _owned_ports_released(ports: list[int]) -> bool:
    """Check that no TCP listener remains on the ports assigned to this game."""
    for port in ports:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return False
        except OSError:
            continue
    return True


def _stop_failure_details(thread: threading.Thread, ports: list[int]) -> str:
    owners = _owned_port_owners(ports)
    port_details = ", ".join(
        f"{port}(pid={','.join(str(pid) for pid in pids) or 'unknown'})"
        for port, pids in owners.items()
    )
    return f"process_pid={os.getpid()}, thread_alive={thread.is_alive()}, ports={port_details}"


def _verify_frontend_health(definition: GameDefinition) -> str | None:
    """Verify the served page, its adapter assets, and the owned WS listener."""
    from urllib.parse import urljoin
    from urllib.request import urlopen

    page_url = _session_url(host="127.0.0.1")
    try:
        with urlopen(page_url, timeout=2) as response:
            if response.status != 200:
                return f"HTTP page returned status {response.status}"
            page = response.read().decode("utf-8")
    except Exception as exc:
        return f"HTTP page health failed: {exc}"
    if "window.BG_GAME_ID" not in page or "BGLabFrontend.start" not in page:
        return "served page is missing the BGLab frontend boot contract"

    script_refs = re.findall(
        r"<script[^>]+src=[\"']([^\"']+)[\"']", page, flags=re.IGNORECASE,
    )
    for reference in script_refs:
        if reference.startswith(("http://", "https://")):
            continue
        asset_url = urljoin(page_url, reference)
        try:
            with urlopen(asset_url, timeout=2) as response:
                if response.status != 200:
                    return f"adapter/page asset returned status {response.status}: {reference}"
                response.read(1)
        except Exception as exc:
            return f"adapter/page asset health failed for {reference}: {exc}"

    # A bare TCP probe leaves the WebSocket server waiting for an HTTP upgrade
    # and can delay immediate stop/rematch or a same-session game switch until
    # the handshake timeout expires.  Complete a real handshake and close it.
    try:
        from websockets.sync.client import connect as websocket_connect

        with websocket_connect(
            "ws://127.0.0.1:7333",
            origin="http://127.0.0.1:8080",
            open_timeout=2,
            close_timeout=1,
        ):
            pass
    except Exception as exc:
        return f"WebSocket health failed on port 7333: {exc}"
    return None


def _stop_game_server(*, end_session: bool) -> str:
    global _SERVER_THREAD
    owned_ports = [8080, 7333]
    with _SERVER_LOCK:
        thread = _SERVER_THREAD
        game_id = _ACTIVE_GAME_ID
        if not thread or not thread.is_alive():
            if _START_IN_PROGRESS:
                _START_CANCEL_EVENT.set()
                if end_session:
                    _GAME_SESSION_DONE_EVENT.set()
                return "Game start cancelled before server startup. Code Agent mode restored."
            if end_session:
                _GAME_SESSION_DONE_EVENT.set()
            return "No active board game."
        _write_live_event({
            "type": "game_stopped", "game_id": game_id,
            "message": "frontend and owned ports are closing",
        })
        _SERVER_STOP_EVENT.set()
    thread.join(timeout=8)
    if thread.is_alive():
        return (
            "Game stop failed: owned server is still shutting down; "
            f"{_stop_failure_details(thread, owned_ports)}"
        )
    deadline = time.monotonic() + 2.0
    while not _owned_ports_released(owned_ports) and time.monotonic() < deadline:
        time.sleep(0.05)
    if not _owned_ports_released(owned_ports):
        return (
            "Game stop failed: owned server thread exited but ports remain LISTENING; "
            f"{_stop_failure_details(thread, owned_ports)}"
        )
    _write_live_event({
        "type": "game_closed", "game_id": game_id,
        "owned_pid": os.getpid(), "owned_ports": owned_ports,
        "ports_released": True, "thread_alive": False,
    })
    if end_session:
        _GAME_SESSION_DONE_EVENT.set()
    return "Game stopped. Code Agent mode restored."


def _finish_start() -> None:
    global _START_IN_PROGRESS
    with _SERVER_LOCK:
        _START_IN_PROGRESS = False


def stop_game() -> str:
    """Stop the owned frontend servers and release the blocked leader."""
    return _stop_game_server(end_session=True)


def abort_game() -> str:
    """Backward-compatible Python alias; slash commands expose only stop."""
    return stop_game()


def rematch_game() -> str:
    """Finish the active result session and start a fresh game with its lineup."""
    with _SERVER_LOCK:
        runtime = _ACTIVE_RUNTIME
        thread = _SERVER_THREAD
        if runtime is None or thread is None or not thread.is_alive():
            return "ERROR: no completed game is available for a rematch."
        if getattr(runtime, "status", "") != "finished":
            return "ERROR: the current game is not finished yet."
        player_types = list(runtime.player_types)
        player_count = len(player_types)
        human_mode = bool(player_types and player_types[0] == "human")
        try:
            manifest = runtime.store.read_manifest()
            delay = int(manifest.get("ai_delay", 800))
            engine = str(manifest.get("engine", "splendor"))
        except Exception:
            delay = 800
            engine = "splendor"
        session_capability = _ACTIVE_SESSION_CAPABILITY
    stopped = _stop_game_server(end_session=False)
    if not stopped.startswith("Game stopped"):
        return f"ERROR: cannot close completed game: {stopped}"
    return _bg_tool_call({
        "engine": engine,
        "mode": "human_vs_ai" if human_mode else "ai_vs_ai",
        "player_count": player_count,
        "delay": delay,
        "open_browser": False,
        # Browser rematches retain the unguessable page capability so the
        # existing result page can reload into the fresh game.  The new game
        # still receives a new gameId and isolated persistent state.
        "_session_capability": session_capability,
    })


def _open_game_page() -> None:
    """Compatibility hook: BGLab reports the URL and never opens a browser."""
    return None


def _inline_json(value: object) -> str:
    """Serialize data inside script elements without allowing an HTML boundary."""
    return json.dumps(value, ensure_ascii=True).replace("<", "\\u003c")


def _browser_ui_script(
    game_id: str,
    player_types: list[str],
    player_names: list[str],
    capability: str | None = None,
    *, chat_enabled: bool = False,
) -> str:
    """Return the small browser-only chat/result UI for the current protocol."""
    human_pids = [i for i, kind in enumerate(player_types) if kind == "human"]
    ai_pids = [i for i, kind in enumerate(player_types) if kind == "ai"]
    config = _inline_json({
        "gameId": game_id,
        "humanPid": human_pids[0] if human_pids else None,
        "aiPids": ai_pids,
        "names": player_names,
        "manualTest": bool(player_types) and all(kind == "human" for kind in player_types),
        "chatEnabled": chat_enabled,
        "sessionCapability": capability,
    })
    return r"""
(function(){
  'use strict';
  var cfg=__CONFIG__, hooked=null,initialAnchor={turnId:null,sentAt:0,confirmed:false},initialAnchorResolve;
  var initialAnchorPromise=new Promise(function(resolve){initialAnchorResolve=resolve});
  window.__BGLAB_INITIAL_SNAPSHOT_CONFIRMED__=false;
  window.__BGLAB_SERVER_SNAPSHOT_READY__=false;
  function socket(){return window.Bridge&&Bridge._ws&&Bridge._ws.readyState===1?Bridge._ws:null}
  function send(payload){var ws=socket();if(!ws)return false;payload.gameId=cfg.gameId;payload.sessionCapability=window.BG_SESSION_CAPABILITY||cfg.sessionCapability||null;ws.send(JSON.stringify(payload));return true}
  function playerName(pid){return cfg.names[pid]||('P'+pid)}
  function cinematic(pid,text,kind,to){var stage=document.getElementById('bglab-dialogue-stage');if(!stage)return;var old=stage.querySelector('[data-bg-pid="'+pid+'"][data-bg-kind="'+kind+'"]');if(old)old.remove();var card=document.createElement('div'),label=document.createElement('div'),body=document.createElement('div'),row=Math.floor(Number(pid||0)/2),left=Number(pid||0)%2===0;card.dataset.bgPid=pid;card.dataset.bgKind=kind;card.style.cssText='position:absolute;'+(left?'left:2.5vw':'right:2.5vw')+';top:'+(14+row*23)+'vh;max-width:34vw;min-width:220px;padding:12px 15px;border-radius:'+(left?'5px 18px 18px 18px':'18px 5px 18px 18px')+';background:'+(kind==='thought'?'rgba(14,20,34,.88)':'rgba(24,42,66,.94)')+';border:1px '+(kind==='thought'?'dashed':'solid')+' '+(left?'#65a7ff':'#e89b55')+';box-shadow:0 8px 28px #0009;color:#f4f7ff;backdrop-filter:blur(8px);animation:bgDialogueIn .3s ease-out,bgDialogueOut .7s ease-in '+(kind==='thought'?'11.3s':'7.3s')+' forwards';label.style.cssText='font:700 12px sans-serif;letter-spacing:.08em;color:'+(left?'#8fc1ff':'#ffc088')+';margin-bottom:6px';label.textContent=kind==='thought'?(playerName(pid)+' · 内心独白'):(playerName(pid)+(to===undefined?'':' → '+playerName(to)));body.style.cssText='font:'+(kind==='thought'?'italic ':'')+'15px/1.55 sans-serif;word-break:break-word;text-shadow:0 1px 2px #000';body.textContent=(kind==='thought'?'“':'')+text+(kind==='thought'?'”':'');card.appendChild(label);card.appendChild(body);stage.appendChild(card);setTimeout(function(){if(card.parentNode)card.remove()},kind==='thought'?12000:8000)}
  function systemLine(text){cinematic(cfg.humanPid===null?0:cfg.humanPid,text,'dialogue')}
  function buildInitialAnchorGate(){if(document.getElementById('bglab-initial-snapshot-gate'))return;var gate=document.createElement('div');gate.id='bglab-initial-snapshot-gate';gate.setAttribute&&gate.setAttribute('role','status');gate.style.cssText='position:fixed;inset:0;z-index:20000;pointer-events:auto;display:flex;align-items:center;justify-content:center;background:rgba(7,12,22,.38);color:#fff;font:600 15px sans-serif;backdrop-filter:blur(2px)';gate.textContent='正在确认初始对局快照…';document.body.appendChild(gate)}
  function releaseInitialAnchor(turnId){if(initialAnchor.confirmed||!initialAnchor.turnId||turnId!==initialAnchor.turnId)return;initialAnchor.confirmed=true;window.__BGLAB_INITIAL_SNAPSHOT_CONFIRMED__=true;var gate=document.getElementById('bglab-initial-snapshot-gate');if(gate)gate.remove();initialAnchorResolve()}
  var rematchReloadPending=false;
  function waitForRematch(){if(rematchReloadPending)return;rematchReloadPending=true;setTimeout(async function probe(){try{var response=await fetch(window.location.href,{cache:'no-store'}),content=await response.text();if(response.ok&&content.indexOf('window.BG_GAME_ID=')!==-1&&content.indexOf('window.BG_GAME_ID="'+cfg.gameId+'"')===-1){window.location.reload();return}}catch(e){}setTimeout(probe,500)},500)}
  function onMessage(event){try{var msg=JSON.parse(event.data);if(msg.type==='frontend_restore'){if(typeof window.__BGLAB_APPLY_SERVER_SNAPSHOT__!=='function')return;try{window.__BGLAB_APPLY_SERVER_SNAPSHOT__(msg.state||null,msg.turnId||null);window.__BGLAB_SERVER_SNAPSHOT_READY__=true}catch(e){if(window.BGLabFrontend&&typeof BGLabFrontend.pause==='function')BGLabFrontend.pause('Confirmed snapshot restore failed: '+(e&&e.message?e.message:e));return}}else if(msg.type==='snapshot_ack')releaseInitialAnchor(msg.turnId);else if(msg.type==='game_retry_ready'){var key='bglab-game-retry:'+cfg.gameId,epoch=String(msg.epoch||'');if(epoch&&window.sessionStorage&&sessionStorage.getItem(key)!==epoch){sessionStorage.setItem(key,epoch);window.location.reload()}}else if(msg.type==='persist_snapshot'){var snapshot=window.BGLabGameAdapter&&BGLabGameAdapter.snapshot();if(snapshot&&(!msg.expectedTurnId||String(snapshot.decisionId)===String(msg.expectedTurnId))&&window.Bridge&&typeof Bridge.persist==='function'){if(msg.expectedTurnId)initialAnchor.turnId=String(msg.expectedTurnId);Bridge.persist()}}else if(msg.type==='game_chat'||msg.type==='game_chat_status')receiveChat(msg);else if(msg.type==='game_chat_history')(msg.events||[]).forEach(receiveChat);else if(msg.type==='chat_ack')chatReceipt(msg);else if(msg.type==='turn_report')cinematic(msg.pid,msg.text,'thought',msg.to_pid);else if(msg.type==='chat_error')chatReceipt(msg);else if(msg.type==='game_control_ack'){systemLine(msg.message||'请求已接收');if(msg.command==='rematch')waitForRematch()}else if(msg.type==='game_control_error')systemLine('操作失败: '+msg.error)}catch(e){}}
  function hook(){var ws=socket();if(!ws||ws===hooked)return;hooked=ws;ws.addEventListener('message',onMessage);send({type:'game_retry_probe'});send({type:'frontend_sync'});if(cfg.chatEnabled){send({type:'game_chat_history'});if(pendingChat)send(pendingChat)}}
  function guardAIRequest(){if(!window.Bridge||typeof Bridge.requestAITurn!=='function'||Bridge.requestAITurn.__bglabInitialAnchorGuard)return;var original=Bridge.requestAITurn.bind(Bridge);var guarded=async function(){await initialAnchorPromise;return original.apply(null,arguments)};guarded.__bglabInitialAnchorGuard=true;Bridge.requestAITurn=guarded}
  function confirmInitialAnchor(){if(initialAnchor.confirmed||!window.__BGLAB_SERVER_SNAPSHOT_READY__||!socket()||!window.BGLabGameAdapter||typeof BGLabGameAdapter.snapshot!=='function'||!window.Bridge||typeof Bridge.persist!=='function')return;var now=Date.now();if(initialAnchor.sentAt&&now-initialAnchor.sentAt<1500)return;var snapshot;try{snapshot=BGLabGameAdapter.snapshot()}catch(e){return}var turnId=snapshot&&snapshot.decisionId;if(!turnId)return;initialAnchor.turnId=String(turnId);if(Bridge.persist())initialAnchor.sentAt=now}
  var chatSeen=new Set(),pendingChat=null;
  function chatLine(text){var log=document.getElementById('bglab-chat-log');if(!log)return;var row=document.createElement('div');row.style.cssText='padding:7px 0;border-bottom:1px solid #ffffff12;white-space:pre-wrap;overflow-wrap:anywhere';row.textContent=text;log.appendChild(row);while(log.children.length>40)log.removeChild(log.firstChild);log.scrollTop=log.scrollHeight}
  function receiveChat(msg){var id=msg.type+':'+(msg.message_id||msg.reply_to);if(chatSeen.has(id))return;chatSeen.add(id);chatLine(msg.type==='game_chat_status'?msg.message:playerName(msg.from_pid)+' → '+playerName(msg.to_pid)+'：'+msg.message);if(msg.type==='game_chat'&&msg.from_pid!==cfg.humanPid)cinematic(msg.from_pid,msg.message,'dialogue',msg.to_pid)}
  function chatReceipt(msg){if(msg.type==='chat_error'){chatLine('发送失败：'+msg.error);return}chatLine(msg.message);if(pendingChat&&msg.clientId===pendingChat.clientId){var input=document.getElementById('bglab-chat-input');if(input&&input.value.trim()===pendingChat.message)input.value='';pendingChat=null}}
  function buildStage(){
    if(!cfg.aiPids.length)return;
    var style=document.createElement('style');style.textContent='@keyframes bgDialogueIn{from{opacity:0;transform:translateY(12px) scale(.97)}to{opacity:1;transform:none}}@keyframes bgDialogueOut{to{opacity:0;transform:translateY(-8px) scale(.98)}}';document.head.appendChild(style);
    var stage=document.createElement('div');stage.id='bglab-dialogue-stage';stage.style.cssText='position:fixed;inset:0;z-index:12000;pointer-events:none';document.body.appendChild(stage);
    if(!cfg.chatEnabled||cfg.humanPid===null)return;
    var dock=document.createElement('details');dock.id='bglab-game-chat';dock.style.cssText='position:fixed;right:18px;bottom:18px;z-index:12010;width:min(360px,calc(100vw - 36px));padding:12px;border-radius:12px;background:rgba(18,23,30,.96);border:1px solid #61717b;color:#edf1ec;box-shadow:0 5px 22px #0006;font-family:inherit;font-size:14px;line-height:1.5';
    dock.innerHTML='<summary style="cursor:pointer">对局聊天 <small style="opacity:.65">AI 在下次行动时回复</small></summary><div id="bglab-chat-log" role="log" aria-live="polite" style="max-height:220px;overflow:auto;margin:8px 0"></div><div style="display:flex;gap:6px"><select id="bglab-chat-to" aria-label="聊天对象" style="max-width:100px"></select><input id="bglab-chat-input" aria-label="消息" maxlength="500" style="min-width:0;flex:1" placeholder="对 AI 说句话…"><button id="bglab-chat-send">发送</button></div>';
    document.body.appendChild(dock);var select=document.getElementById('bglab-chat-to');cfg.aiPids.forEach(function(pid){var option=document.createElement('option');option.value=pid;option.textContent=playerName(pid);select.appendChild(option)});
    function submit(){var input=document.getElementById('bglab-chat-input'),message=input.value.trim();if(!message)return;var to=Number(select.value);if(!pendingChat||pendingChat.message!==message||pendingChat.toPid!==to)pendingChat={type:'game_chat',toPid:to,message:message,clientId:window.crypto&&crypto.randomUUID?crypto.randomUUID():String(Date.now())+'-'+Math.random().toString(36).slice(2)};if(!send(pendingChat))chatLine('连接未就绪，消息已保留，请稍后重试。')}
    document.getElementById('bglab-chat-send').onclick=submit;document.getElementById('bglab-chat-input').onkeydown=function(e){if(e.key==='Enter'&&!e.isComposing)submit()};
  }
  function resultControls(){if(!initialAnchor.confirmed||!window.BGLabFrontend||!BGLabFrontend.status||BGLabFrontend.status().phase!=='finished'){var stale=document.getElementById('bglab-result-controls');if(stale)stale.remove();return;}if(document.getElementById('bglab-result-controls'))return;var box=document.createElement('div');box.id='bglab-result-controls';box.style.cssText='position:fixed;left:50%;bottom:28px;transform:translateX(-50%);z-index:13000;background:#11182a;border:1px solid #60739b;border-radius:10px;padding:12px;color:#fff;font:14px sans-serif;box-shadow:0 4px 20px #000a';box.innerHTML='<span style="margin-right:10px">对局已结束</span><button data-command="rematch">再来一局</button> <button data-command="end">结束</button>';box.onclick=function(e){var command=e.target&&e.target.dataset&&e.target.dataset.command;if(!command)return;if(send({type:'game_control',command:command})){box.querySelectorAll('button').forEach(function(b){b.disabled=true});box.firstChild.textContent=command==='rematch'?'正在创建新对局…':'正在关闭…'}else alert('连接未就绪，请使用 TUI 的 /bg stop')};document.body.appendChild(box)}
  function maintain(){try{hook()}catch(e){}try{guardAIRequest()}catch(e){}try{confirmInitialAnchor()}catch(e){}try{resultControls()}catch(e){}}
  function init(){buildInitialAnchorGate();buildStage();setInterval(maintain,100);maintain()}
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();
})();
""".replace("__CONFIG__", config)


_SERVER_RUNNING = False  # global guard
_RELEASE_ACTION_PROTOCOL = "semantic-v2"
_RELEASE_GAME_PROFILE = RELEASE_GAME_PROFILE


def _release_action_protocol(manifest: dict[str, Any], *, restore: bool) -> str:
    """Reject removed protocol identities; current games use semantic-v2."""
    stored = manifest.get("action_protocol") if restore else None
    if stored is not None and stored != _RELEASE_ACTION_PROTOCOL:
        raise RuntimeError(
            f"historical action protocol {stored!r} is no longer executable; "
            "current runtime requires semantic-v2"
        )
    return _RELEASE_ACTION_PROTOCOL


def _release_provider_policy(
    model: str,
    *,
    settings: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    restore: bool = False,
) -> ProviderSlotPolicy:
    """Preserve the selected primary identity without gameplay fallback."""
    selected = load_provider_slot_policy({"model": model})
    if restore and isinstance(manifest, dict) and isinstance(
        manifest.get("provider_policy"), dict,
    ):
        configured = load_provider_slot_policy({
            "model": model,
            "provider_slots": manifest["provider_policy"],
        })
    else:
        if settings is None:
            from bglab.session.settings import load_settings

            settings = load_settings()
        configured = load_provider_slot_policy(settings)
    primary = (
        configured.primary
        if configured.primary.reference == selected.primary.reference
        else selected.primary
    )
    return ProviderSlotPolicy(
        primary=primary,
        standby=None,
        max_attempts=(
            configured.max_attempts
            if configured.primary.reference == selected.primary.reference
            else selected.max_attempts
        ),
    )


def _release_agent_profiles(
    manifest: dict[str, Any],
    player_count: int,
    *,
    restore: bool,
) -> list[GameAgentProfile]:
    """Restore every persisted seat identity or build the new release seats."""
    if not restore:
        return [
            GameAgentProfile(features=_RELEASE_GAME_PROFILE)
            for _ in range(player_count)
        ]
    from bglab.games.runtime import restore_agent_profiles

    return restore_agent_profiles(manifest, player_count)


def _release_game_profile(
    manifest: dict[str, Any],
    *,
    restore: bool,
) -> GameFeatureProfile:
    """Preserve the capability identity of supported released saves."""
    if not restore:
        return _RELEASE_GAME_PROFILE
    profiles = manifest.get("agent_profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("restored game is missing persisted agent_profiles")
    restored = _release_agent_profiles(
        manifest, len(profiles), restore=True,
    )
    if any(item.features not in {RELEASE_GAME_PROFILE, RELEASE_CHAT_PROFILE} for item in restored):
        raise ValueError(
            "historical release profile is no longer executable; "
            "current runtime requires release-semantic-v2"
        )
    return restored[0].features

async def _run_ai_server(definition: GameDefinition, player_types: list[str] | None = None,
                         player_names: list[str] | None = None,
                         ai_delay: int = 800, game_id: str | None = None,
                         restore: bool = False,
                         model: str | None = "deepseek-chat",
                         mode: str = "human_vs_ai",
                         result_only: bool = False,
                         result_evidence: dict | None = None,
                         session_capability: str | None = None) -> str:
    """启动 HTTP+WS 服务器，浏览器前端 + AI 决策。

    player_types: ["human","ai"] = 人 vs AI, ["ai","ai"] = AI 观战
    """
    global _SERVER_RUNNING, _ACTIVE_RUNTIME, _ACTIVE_SESSION_CAPABILITY
    if _SERVER_RUNNING:
        raise RuntimeError("Server already running. Use /bg stop to restart.")
    _SERVER_RUNNING = True

    if player_types is None:
        player_types = ["human", "ai"]
    if player_names is None:
        player_names = ["" if t == "human" else f"AI {i+1}" for i, t in enumerate(player_types)]
    pc = len(player_types)
    manual_test = mode == "manual-test"

    from bglab.games.persistence.store import GameStore
    if not game_id:
        import uuid
        game_id = uuid.uuid4().hex[:8]
    if not isinstance(session_capability, str) or not session_capability:
        session_capability = secrets.token_urlsafe(32)
    with _SERVER_LOCK:
        _ACTIVE_SESSION_CAPABILITY = session_capability
    raw_store = GameStore(game_id)
    if result_only:
        if not isinstance(result_evidence, dict):
            raise RuntimeError("result-only session requires validated evidence")
        store = _FrozenResultStore(
            raw_store,
            result_evidence["manifest"],
            result_evidence["snapshot"],
        )
        game_manifest = store.read_manifest()
    else:
        store = raw_store
        game_manifest = raw_store.read_manifest()

    team_name = None
    if not result_only:
        game_rules = definition.session_head_path.read_text(encoding="utf-8")
        if not manual_test:
            team_name = _ensure_game_team(
                game_id, player_types, player_names,
                model=model or "deepseek-chat", engine=definition.id,
            )
    if result_only:
        runtime = _FinishedResultRuntime(
            game_id, player_types, player_names, store,
            result_evidence["finalResult"],
        )
    else:
        from bglab.games.runtime import AuthorityBindingError, GameSessionRuntime

        release_action_protocol = _release_action_protocol(
            game_manifest,
            restore=restore,
        )
        release_agent_profiles = _release_agent_profiles(
            game_manifest, len(player_types), restore=restore,
        )
        release_game_profile = _release_game_profile(
            game_manifest,
            restore=restore,
        )
        release_provider_policy = _release_provider_policy(
            model or "deepseek-chat",
            manifest=game_manifest,
            restore=restore,
        )
        runtime = GameSessionRuntime(
            game_id, player_types, player_names, game_rules,
            store=store, event_callback=_write_live_event, restore=restore,
            team_name=team_name, model=model, definition=definition,
            # Production exposes one selected action protocol. Alternative
            # protocols remain available only to explicit diagnostic runners.
            action_protocol=release_action_protocol,
            profile=release_game_profile,
            agent_profiles=release_agent_profiles,
            stop_after_action=True,
            # Recovery may ask the model again; the host must not choose a move.
            host_fallback_enabled=False,
            provider_policy=release_provider_policy,
        )
    _ACTIVE_RUNTIME = runtime

    frontend_dir = str(definition.frontend_entry.parent)

    # Pre-compute frontend config
    import json as _j
    pcfg = _inline_json({
        "playerCount": pc,
        "playerTypes": player_types,
        "names": player_names,
        "viewerSeat": next((index for index, kind in enumerate(player_types) if kind == "human"), None)
        if player_types.count("human") == 1 else (0 if manual_test else None),
        "aiDelay": ai_delay,
        "gameId": game_id,
        "seed": int(game_manifest.get("seed", 1)),
        "mode": mode,
        "manualTest": manual_test,
    })

    # Generic adapter/frontend boot. Game rules and state fields stay inside the package.
    js_code = (
        'window.BG_GAME_ID="' + game_id + '";'
        'window.__BGLAB_HSA026_TRACE__=__HSA026_TRACE__;'
        'var _srv=__BG_SERVER_SNAPSHOT__,_cfg=' + pcfg + ';'
        'window.__BGLAB_APPLY_SERVER_SNAPSHOT__=function(state){if(state)BGLabFrontend.restore(state,_cfg)};'
        'function _boot(){if(!window.BGLabGameAdapter||!window.BGLabFrontend||!BGLabFrontend.start){setTimeout(_boot,100);return}'
        'try{if(_srv)BGLabFrontend.restore(_srv,_cfg);else BGLabFrontend.start(_cfg);'
        '}catch(e){console.error("bglab frontend boot",e);if(BGLabFrontend.pause)BGLabFrontend.pause(e.message)}}'
        'setTimeout(_boot,100);'
    )

    class _GameHandler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=frontend_dir, **kw)

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            super().end_headers()

        def log_message(self, *a): pass
        def do_GET(self):
            requested = self.path.split("?", 1)[0].lower()
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            page_paths = {"/", "/" + definition.frontend_entry.name.lower()}
            if requested in page_paths and not _session_capability_matches(
                query.get("capability", [None])[0], session_capability,
            ):
                self.send_error(403, "invalid session capability")
                return
            if requested.endswith(("/src/adapter/ws-bridge.js", "/src/adapter/ws-bridge.ts")):
                bridge_path = Path(frontend_dir) / requested.lstrip("/").replace("/", os.sep)
                if bridge_path.is_file():
                    payload = _inject_bridge_capability(bridge_path.read_bytes())
                    self.send_response(200)
                    self.send_header("Content-Type", "text/javascript; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
            if requested.startswith("/__bglab_shared/"):
                shared_name = requested[len("/__bglab_shared/"):]
                shared_path = resolve_shared_ui_asset(definition.root, shared_name)
                if shared_path is not None:
                    payload = shared_path.read_bytes()
                    self.send_response(200)
                    self.send_header(
                        "Content-Type", shared_ui_content_type(shared_name),
                    )
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                self.send_error(404, "shared UI asset not found")
                return
            if definition.id == "the-white-castle" and requested.startswith("/assets/") and requested.endswith(".js"):
                asset_path = Path(frontend_dir) / requested.lstrip("/").replace("/", os.sep)
                if asset_path.is_file():
                    payload = asset_path.read_bytes()
                    browser_explorer = (
                        b"var{createDecisionExplorer:Ht}=(0,e(((e,t)=>{t.exports={}}))().createRequire)"
                        b"(import.meta.url)(`../../../_sdk/decision-explorer/index.cjs`),"
                    )
                    browser_safe_explorer = (
                        b'var Ht=()=>{throw new Error("Decision Explorer is available only in the host worker.")},'
                    )
                    bridge_onopen = (
                        b"this.send({type:`frontend_hello`})}"
                    )
                    bridge_ready_onopen = (
                        b"this.send({type:`frontend_hello`});window.BGLabFrontend?.bridgeReady?.()}"
                    )
                    bridge_ai_error = (
                        b"t.type===`ai_error`&&this._pending?.turnId===t.turnId?"
                        b"(this._pending.reject(Error(t.error||`AI API error; game paused`)),"
                        b"this._pending=null):t.type===`snapshot_error`"
                    )
                    bridge_ai_retry = (
                        b"t.type===`ai_error`&&this._pending?.turnId===t.turnId?"
                        b"(this._pending.reject(Error(t.error||`AI API error; game paused`)),"
                        b"this._pending=null):t.type===`ai_retry`&&this._pending?.turnId===t.turnId?"
                        b"(this._pending.resolve({retry:!0}),this._pending=null):t.type===`snapshot_error`"
                    )
                    frontend_global = (
                        b"window.BGLabFrontend={start:Dr,restore:Or,pause(e){J=e,R=!0,z=e,Q()},"
                        b"status(){return{...F.snapshot().wrapper,paused:J}}}"
                    )
                    frontend_with_bridge_ready = (
                        b"window.BGLabFrontend={start:Dr,restore:Or,bridgeReady(){"
                        b"if(J===`Bridge not connected`){J=null;R=!1;z=null;Q();"
                        b"window.setTimeout(()=>void vr(),0)}},pause(e){J=e,R=!0,z=e,Q()},"
                        b"status(){return{...F.snapshot().wrapper,paused:J}}}"
                    )
                    ai_pending_guard = (
                        b"if(this._pending)return Promise.reject(Error(`An AI request is already pending`));"
                        b"let t=window.BGLabGameAdapter.snapshot(),"
                    )
                    ai_pending_with_snapshot = (
                        b"if(this._pending)return Promise.reject(Error(`An AI request is already pending`));"
                        b"if(!this.persist())return Promise.reject(Error(`Bridge not connected`));"
                        b"let t=window.BGLabGameAdapter.snapshot(),"
                    )
                    transformed = payload.replace(browser_explorer, browser_safe_explorer, 1)
                    transformed = transformed.replace(bridge_onopen, bridge_ready_onopen, 1)
                    transformed = transformed.replace(bridge_ai_error, bridge_ai_retry, 1)
                    transformed = transformed.replace(frontend_global, frontend_with_bridge_ready, 1)
                    transformed = transformed.replace(ai_pending_guard, ai_pending_with_snapshot, 1)
                    transformed = _inject_bridge_capability(transformed)
                    if transformed != payload:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/javascript; charset=utf-8")
                        self.send_header("Content-Length", str(len(transformed)))
                        self.end_headers()
                        self.wfile.write(transformed)
                        return
            if requested in {
                "/ai-player.js", "/ai-player.min.js", "/game.min.js", "/bg-patches.js",
            }:
                self.send_error(404, "legacy gameplay asset is disabled")
                return
            if requested in ('/', '/' + definition.frontend_entry.name.lower()):
                self.send_response(200)
                self.send_header('Content-type', 'text/html; charset=utf-8')
                self.end_headers()
                with open(definition.frontend_entry, 'rb') as f:
                    content = f.read().decode('utf-8')
                if "ai-player" in content.lower() or "game.min.js" in content.lower():
                    self.send_error(500, "legacy gameplay bundle could not be removed")
                    return
                head = re.search(r"<head(?:\s[^>]*)?>", content, re.IGNORECASE)
                if head is None:
                    self.send_error(500, "frontend page is missing a head element")
                    return
                session_bootstrap = (
                    "<script>window.BG_SESSION_CAPABILITY="
                    + _j.dumps(session_capability)
                    + ";</script>"
                )
                content = (
                    content[:head.end()] + session_bootstrap + content[head.end():]
                )
                try:
                    latest_record = store.read_snapshot()
                    latest_state = (latest_record or {}).get("state") if latest_record else None
                    latest_json = _inline_json(latest_state) if latest_state else "null"
                except Exception as exc:
                    self.send_error(500, f"confirmed snapshot is unreadable: {exc}")
                    return
                trace_enabled = parse_qs(urlparse(self.path).query).get("hsa026Trace") == ["1"]
                served_js = (
                    js_code
                    .replace("__BG_SERVER_SNAPSHOT__", latest_json, 1)
                    .replace("__HSA026_TRACE__", "true" if trace_enabled else "false", 1)
                )
                content += (
                    '<script src="/__bglab_shared/action-history.js"></script>'
                    '<script src="/__bglab_shared/draft-lifecycle.js"></script>'
                    '\n<script>' + served_js + '</script>'
                    '\n<script>'
                    + _browser_ui_script(
                        game_id, player_types, player_names, session_capability,
                        chat_enabled=any(item.features.chat for item in getattr(runtime, 'agent_profiles', [])),
                    )
                    + '</script>'
                )
                content = content.replace(
                    '</body>',
                    '<script>' + _browser_bridge_retry_script() + '</script></body>')
                self.wfile.write(content.encode('utf-8'))
                return
            super().do_GET()

    turn_count = 0
    ai_call_count = 0
    tool_stats: dict[str, int] = {}
    authenticated_frontends: set[int] = set()
    frontend_generation = 0
    turn_connections: dict[str, Any] = {}
    validation_waiters: dict[str, tuple[asyncio.Future, int]] = {}
    ai_tasks: set[asyncio.Task] = set()
    # A socket owner and the provider attempt it admitted are one fenced
    # generation.  A replacement socket may reuse the same confirmed frame
    # only after the previous generation has drained.
    ai_attempts: dict[str, dict[str, Any]] = {}
    attempt_generations: dict[str, int] = {}
    fenced_attempts: dict[str, int] = {}
    fenced_attempt_drains: dict[str, asyncio.Task] = {}
    recovery_tasks: set[asyncio.Task] = set()
    recovery_by_connection: dict[int, dict[str, Any]] = {}
    recovery_by_turn: dict[str, dict[str, Any]] = {}
    # Admission deliberately retains only the authority digest.  Browser state
    # (especially adapterView) must not sit in a queue waiting to become model
    # input after the confirmed worker is rebuilt.
    pending_ai_turns: list[tuple[int, str, str, Any]] = []
    authority_worker: AuthorityWorker | None = None
    authority_worker_lock = asyncio.Lock()

    async def validate_with_frontend(
        turn_id: str,
        pid: int,
        transaction: dict,
        *,
        target_ws: Any = None,
    ) -> dict:
        if transaction.get("mode") == "decision_frame_summary":
            async with authority_worker_lock:
                worker = authority_worker
                if worker is None:
                    return {
                        "coverageStatus": "not_explored",
                        "enumerationComplete": False,
                    }
                return await asyncio.to_thread(worker.outcome_index, pid, {
                    "maxNodes": transaction["maxNodes"], "maxTimeMs": transaction["maxTimeMs"],
                })
        ws = target_ws or turn_connections.get(turn_id)
        if ws is None:
            raise RuntimeError("authoritative frontend adapter is not connected")
        request_id = f"{turn_id}:{time.time_ns()}"
        future = asyncio.get_running_loop().create_future()
        validation_waiters[request_id] = (future, id(ws))
        try:
            await ws.send(_j.dumps({
                "type": "validate_action", "gameId": game_id,
                "requestId": request_id, "turnId": turn_id, "pid": pid,
                "decisionId": turn_id, "mode": "commit", "transaction": transaction,
            }, ensure_ascii=False))
            result = await asyncio.wait_for(future, timeout=30)
        finally:
            validation_waiters.pop(request_id, None)
        if result.get("ok"):
            frontend_turn_id, frontend_state_hash = _frontend_confirmation(
                definition.id, turn_id, result,
            )
            return {
                "status": "committed", "transaction": transaction,
                "canonicalAction": result.get("action"),
                "effects": result.get("events", []),
                "outcome": result.get("outcome", {}),
                "boundaryReason": result.get("boundaryReason", "turn_passed"),
                "acceptedSteps": list(range(len(transaction.get("steps", [])))),
                "failedStep": None, "error": None, "stateChanged": True,
                "frontendTurnId": frontend_turn_id,
                "frontendStateHash": frontend_state_hash,
                "instruction": "事务已由游戏 Adapter 原子提交；本回合不要再次调用 BgAct。",
            }
        prefix = result.get("validatedPrefix") or []
        return {
            "status": "invalid", "transaction": transaction,
            "canonicalAction": None, "effects": [],
            "acceptedSteps": list(range(len(prefix))),
            "failedStep": result.get("failedStep"),
            "continuationStatus": result.get("continuationStatus"),
            "declineActions": result.get("declineActions", []),
            "error": {
                "code": result.get("code"),
                "message": result.get("message"),
                "nextActions": result.get("nextActions", []),
                "facts": result.get("facts"),
            },
            "stateChanged": False,
            "instruction": result.get("correction") or "修正后重新提交完整 steps。",
        }

    runtime.set_action_validator(validate_with_frontend)

    # Clear live events file for new game
    try:
        _LIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LIVE_FILE.write_text("", encoding="utf-8")
    except Exception:
        pass

    async def ws_handler(ws):
        nonlocal turn_count, ai_call_count, frontend_generation, authority_worker
        connection_id = id(ws)
        authenticated = False

        async def send_stale_authority(
            pid: int,
            turn_id: str,
            target_ws: Any,
            *,
            expected_turn_id: str | None = None,
            reason: str = "STALE_AUTHORITY_FRAME",
        ) -> None:
            await target_ws.send(_j.dumps({
                "type": "ai_retry", "pid": pid, "turnId": turn_id,
                "reason": reason, "paused": False,
                "expectedTurnId": expected_turn_id,
            }, ensure_ascii=False))

        def detach_ai_attempt(
            turn_id: str, target_ws: Any,
        ) -> dict[str, Any] | None:
            """Compare-and-pop one socket's AI attempt generation."""
            if turn_connections.get(turn_id) is target_ws:
                turn_connections.pop(turn_id, None)
            record = ai_attempts.get(turn_id)
            if not isinstance(record, dict) or record.get("ws") is not target_ws:
                return None
            ai_attempts.pop(turn_id, None)
            generation = record.get("generation")
            if isinstance(generation, int) and not isinstance(generation, bool):
                attempt_generations[turn_id] = max(
                    attempt_generations.get(turn_id, 0), generation + 1,
                )
            return record

        def attempt_is_current(
            turn_id: str, generation: int, target_ws: Any,
        ) -> bool:
            record = ai_attempts.get(turn_id)
            return (
                isinstance(record, dict)
                and record.get("generation") == generation
                and record.get("ws") is target_ws
                and turn_connections.get(turn_id) is target_ws
            )

        def remember_attempt_drain(turn_id: str, drain: asyncio.Task) -> None:
            fenced_attempt_drains[turn_id] = drain

            def clear(done: asyncio.Task) -> None:
                if fenced_attempt_drains.get(turn_id) is done:
                    fenced_attempt_drains.pop(turn_id, None)

            drain.add_done_callback(clear)

        async def fence_disconnected_attempt(
            turn_id: str, target_ws: Any,
        ) -> None:
            """Fence provider/validation work before a socket can be replaced."""
            record = detach_ai_attempt(turn_id, target_ws)
            if record is None:
                return
            pid = record.get("pid")
            process_task = record.get("task")
            pending_replay = None
            try:
                pending_replay = runtime.store.read_pending_replay_turn()
            except Exception:
                # A corrupt pending record is still a durable recovery fence;
                # do not turn a transport disconnect into another provider call.
                pending_replay = {"corrupt": True}

            drain: asyncio.Task | None = None
            agent = (
                runtime.agents.get(pid)
                if isinstance(pid, int)
                and isinstance(getattr(runtime, "agents", None), dict)
                else None
            )
            # A committed pending replay is already the model/commit fence.  It
            # must be recovered by the next tab without recalling the provider.
            if pending_replay is None and agent is not None:
                cancel = getattr(agent, "schedule_cancel_current_attempt", None)
                if callable(cancel):
                    try:
                        result = cancel("frontend_disconnected")
                        if inspect.isawaitable(result):
                            drain = asyncio.create_task(result)
                    except Exception:
                        drain = None
                if drain is None:
                    candidate = getattr(agent, "_pending_drain", None)
                    if isinstance(candidate, asyncio.Task):
                        drain = candidate
                if drain is None:
                    cancel_now = getattr(agent, "cancel_current_attempt", None)
                    if callable(cancel_now):
                        try:
                            result = cancel_now("frontend_disconnected")
                            if inspect.isawaitable(result):
                                drain = asyncio.create_task(result)
                        except Exception:
                            drain = None
                if drain is not None:
                    fenced_attempts[turn_id] = pid
                    remember_attempt_drain(turn_id, drain)

            if (
                isinstance(process_task, asyncio.Task)
                and process_task is not asyncio.current_task()
                and not process_task.done()
            ):
                process_task.cancel()

            if drain is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(drain), timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except Exception:
                    pass
            if (
                isinstance(process_task, asyncio.Task)
                and process_task is not asyncio.current_task()
                and not process_task.done()
            ):
                try:
                    await asyncio.wait_for(asyncio.shield(process_task), timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except Exception:
                    pass

        async def await_fenced_attempt(turn_id: str) -> bool:
            """Wait for a disconnected generation before admitting its retry."""
            drain = fenced_attempt_drains.get(turn_id)
            if drain is None:
                return True
            try:
                await asyncio.wait_for(asyncio.shield(drain), timeout=5)
            except asyncio.TimeoutError:
                return False
            except Exception:
                pass
            if fenced_attempt_drains.get(turn_id) is drain:
                fenced_attempt_drains.pop(turn_id, None)
            return True

        def reopen_fenced_agent_turn(turn_id: str, pid: int) -> bool:
            """Drop only the fully drained, uncommitted handle for a retry."""
            if fenced_attempts.get(turn_id) != pid:
                return False
            if fenced_attempt_drains.get(turn_id) is not None:
                return False
            fenced_attempts.pop(turn_id, None)
            agent = (
                runtime.agents.get(pid)
                if isinstance(getattr(runtime, "agents", None), dict)
                else None
            )
            handles = getattr(agent, "_handles", None)
            if not isinstance(handles, dict):
                return True
            old_handle = handles.pop(turn_id, None)
            if old_handle is None:
                return True
            action = getattr(old_handle, "action", None)
            if action is not None and not action.done():
                action.cancel()
            elif action is not None:
                try:
                    action.exception()
                except BaseException:
                    pass
            return True

        def release_turn_owner(turn_id: str, target_ws: Any) -> None:
            detach_ai_attempt(turn_id, target_ws)
            _release_ai_turn_owner(
                turn_connections, pending_ai_turns, turn_id, target_ws,
            )

        async def process_ai_turn(
            pid: int,
            turn_id: str,
            claimed_authority_hash: str,
            target_ws: Any,
            authority_frame: Any = None,
            attempt_generation: int | None = None,
            retry_reason: str | None = None,
        ) -> None:
            nonlocal turn_count, authority_worker
            try:
                if (
                    attempt_generation is not None
                    and not attempt_is_current(
                        turn_id, attempt_generation, target_ws,
                    )
                ):
                    return
                worker_issue: tuple[str, str | None] | None = None
                failed_worker = None
                model_state = None
                async with authority_worker_lock:
                    worker = authority_worker
                    if worker is None:
                        confirmed_state = (
                            authority_frame.snapshot
                            if authority_frame is not None
                            else None
                        )
                        if not isinstance(confirmed_state, dict):
                            confirmed_record = runtime.store.read_snapshot()
                            confirmed_state = (
                                confirmed_record.get("state")
                                if isinstance(confirmed_record, dict)
                                else None
                            )
                        try:
                            if not isinstance(confirmed_state, dict):
                                raise RuntimeError(
                                    "confirmed snapshot worker state is unavailable",
                                )
                            worker = await asyncio.to_thread(
                                AuthorityWorker,
                                definition,
                                confirmed_state,
                                decision_id=turn_id,
                            )
                            authority_worker = worker
                        except Exception:
                            worker = None
                            worker_issue = (
                                "AUTHORITY_WORKER_UNAVAILABLE", turn_id,
                            )
                    model_state_builder = (
                        getattr(worker, "model_state", None)
                        if worker is not None else None
                    )
                    if worker_issue is not None:
                        pass
                    elif callable(model_state_builder):
                        try:
                            model_state = await asyncio.to_thread(
                                model_state_builder, pid,
                            )
                        except Exception as exc:
                            if authority_worker is worker:
                                authority_worker = None
                            failed_worker = worker
                            stale_worker = any(
                                marker in str(exc).casefold()
                                for marker in (
                                    "stale authority", "authority adapter view",
                                    "confirmed authority decision",
                                )
                            )
                            current_record = runtime.store.read_snapshot()
                            worker_issue = (
                                (
                                    "STALE_AUTHORITY_FRAME"
                                    if stale_worker
                                    else "AUTHORITY_WORKER_FAILURE"
                                ),
                                (
                                    current_record.get("turn_id")
                                    if isinstance(current_record, dict)
                                    else turn_id
                                ),
                            )
                    elif worker is not None:
                        # Diagnostic worker doubles predating the host-owned view
                        # API may expose only a confirmed snapshot.  They still
                        # cannot reintroduce the queued browser state.
                        current_record = runtime.store.read_snapshot()
                        model_state = copy.deepcopy(
                            current_record.get("state")
                            if isinstance(current_record, dict)
                            else None
                        )
                        if not isinstance(model_state, dict):
                            raise AuthorityBindingError(
                                "confirmed authority snapshot is unavailable",
                                expected_turn_id=turn_id,
                            )
                        model_state.pop("adapterView", None)
                if worker_issue is not None:
                    if failed_worker is not None:
                        try:
                            await asyncio.to_thread(failed_worker.close)
                        except Exception:
                            pass
                    release_turn_owner(turn_id, target_ws)
                    await send_stale_authority(
                        pid, turn_id, target_ws,
                        expected_turn_id=worker_issue[1],
                        reason=worker_issue[0],
                    )
                    return
                if not isinstance(model_state, dict):
                    release_turn_owner(turn_id, target_ws)
                    await send_stale_authority(
                        pid, turn_id, target_ws,
                        expected_turn_id=turn_id,
                        reason="AUTHORITY_WORKER_FAILURE",
                    )
                    return
                if authority_hash(model_state) != claimed_authority_hash:
                    release_turn_owner(turn_id, target_ws)
                    current_record = runtime.store.read_snapshot()
                    await send_stale_authority(
                        pid, turn_id, target_ws,
                        expected_turn_id=current_record.get("turn_id")
                        if isinstance(current_record, dict)
                        else None,
                    )
                    return
                if (
                    attempt_generation is not None
                    and not attempt_is_current(
                        turn_id, attempt_generation, target_ws,
                    )
                ):
                    return
                if authority_frame is None:
                    authority_frame = runtime.bind_confirmed_ai_turn(
                        pid, turn_id, model_state,
                    )
                retry_options = (
                    {"retry_reason": retry_reason}
                    if retry_reason is not None
                    else {}
                )
                response = await runtime.handle_ai_turn(
                    pid, turn_id, model_state,
                    model_state=model_state,
                    authority_frame=authority_frame,
                    **retry_options,
                )
                if (
                    attempt_generation is not None
                    and not attempt_is_current(
                        turn_id, attempt_generation, target_ws,
                    )
                ):
                    # A disconnected generation may have completed only after
                    # its lease was fenced.  Durable pending replay, if any,
                    # is recovered by the next frontend snapshot; never send
                    # a stale result into a replacement lifecycle.
                    return
                response["adapterCommitted"] = True
                turn_count += 1
                action_type = (response.get("canonicalAction") or {}).get("type", "?")
                print(f"  [{turn_count}] P{pid} -> {action_type} committed")
                await target_ws.send(_j.dumps(response, ensure_ascii=False))
            except AuthorityBindingError as exc:
                release_turn_owner(turn_id, target_ws)
                await send_stale_authority(
                    pid, turn_id, target_ws,
                    expected_turn_id=exc.expected_turn_id,
                )
            except RuntimeError as exc:
                release_turn_owner(turn_id, target_ws)
                if any(
                    marker in str(exc).casefold()
                    for marker in (
                        "stale authority", "authority adapter view",
                        "confirmed authority decision",
                    )
                ):
                    await send_stale_authority(
                        pid, turn_id, target_ws,
                        expected_turn_id=turn_id,
                    )
                    return
                status = str(getattr(runtime, "status", ""))
                if not status.startswith("paused"):
                    current_record = runtime.store.read_snapshot()
                    await send_stale_authority(
                        pid, turn_id, target_ws,
                        expected_turn_id=(
                            current_record.get("turn_id")
                            if isinstance(current_record, dict)
                            else turn_id
                        ),
                        reason="AI_TURN_RUNTIME_ERROR",
                    )
                    return
                await target_ws.send(_j.dumps({
                    "type": "ai_error", "pid": pid, "turnId": turn_id,
                    "error": str(exc)[:300], "paused": True,
                    "status": status,
                    "recoverable": status in {
                        "paused_api_error", "paused_frontend_error",
                    },
                }, ensure_ascii=False))
            except asyncio.CancelledError:
                release_turn_owner(turn_id, target_ws)
                if not _SERVER_STOP_EVENT.is_set():
                    try:
                        current_record = runtime.store.read_snapshot()
                        await send_stale_authority(
                            pid, turn_id, target_ws,
                            expected_turn_id=current_record.get("turn_id")
                            if isinstance(current_record, dict)
                            else None,
                        )
                    except Exception:
                        pass
                raise
            except Exception as exc:
                release_turn_owner(turn_id, target_ws)
                status = str(getattr(runtime, "status", ""))
                if not status.startswith("paused"):
                    current_record = runtime.store.read_snapshot()
                    await send_stale_authority(
                        pid, turn_id, target_ws,
                        expected_turn_id=(
                            current_record.get("turn_id")
                            if isinstance(current_record, dict)
                            else turn_id
                        ),
                        reason="AI_TURN_RUNTIME_ERROR",
                    )
                    return
                manifest = runtime.store.read_manifest()
                pause_issue = manifest.get("pause_issue")
                safe_error = (
                    str(pause_issue.get("title") or "API 请求失败")
                    if status == "paused_api_error"
                    and isinstance(pause_issue, dict)
                    else str(exc)[:300]
                )
                await target_ws.send(_j.dumps({
                    "type": "ai_error", "pid": pid, "turnId": turn_id,
                    "error": safe_error, "paused": True,
                    "status": status,
                    "recoverable": status in {
                        "paused_api_error", "paused_frontend_error",
                    },
                }, ensure_ascii=False))

        def schedule_ai_turn(
            pid: int,
            turn_id: str,
            claimed_authority_hash: str,
            target_ws: Any,
            authority_frame: Any = None,
            retry_reason: str | None = None,
        ) -> None:
            if turn_connections.get(turn_id) is not target_ws:
                return
            generation = attempt_generations.get(turn_id, 0) + 1
            attempt_generations[turn_id] = generation
            task = asyncio.create_task(
                process_ai_turn(
                    pid, turn_id, claimed_authority_hash, target_ws,
                    authority_frame, generation, retry_reason,
                ),
            )
            ai_attempts[turn_id] = {
                "pid": pid,
                "ws": target_ws,
                "generation": generation,
                "task": task,
            }
            ai_tasks.add(task)
            task.add_done_callback(ai_tasks.discard)

        def schedule_restored_action_recovery(
            recovery: dict[str, Any],
            target_ws: Any,
            connection_key: int,
        ) -> None:
            turn_id = recovery.get("turnId")
            pid = recovery.get("seat")
            transaction = recovery.get("transaction")
            if (
                not isinstance(turn_id, str)
                or not isinstance(pid, int)
                or not isinstance(transaction, dict)
            ):
                raise RuntimeError("malformed restored action recovery state")
            existing_owner = recovery_by_turn.get(turn_id)
            if isinstance(existing_owner, dict):
                # One pending replay turn has exactly one frontend authority
                # owner. Quarantine another restored tab immediately so its
                # initial-anchor retry cannot later submit the stale snapshot
                # and pause the newly advanced game.
                close_task = asyncio.create_task(
                    target_ws.close(
                        code=4009,
                        reason="restored action recovery is owned by another frontend",
                    ),
                    name=f"bg-recovery-conflict-{game_id}-{turn_id}",
                )
                recovery_tasks.add(close_task)
                close_task.add_done_callback(recovery_tasks.discard)
                return
            owner = {
                "status": "dispatching",
                "turnId": turn_id,
                "connectionId": connection_key,
            }
            recovery_by_connection[connection_key] = owner
            recovery_by_turn[turn_id] = owner

            async def recover() -> None:
                try:
                    result = await validate_with_frontend(
                        turn_id, pid, copy.deepcopy(transaction),
                        target_ws=target_ws,
                    )
                    if result.get("status") != "committed":
                        raise RuntimeError(
                            "restored durable action was rejected by authority",
                        )
                    expected_turn_id = result.get("frontendTurnId")
                    if not isinstance(expected_turn_id, str) or not expected_turn_id:
                        expected_turn_id = None
                    expected_hash = result.get("frontendStateHash")
                    if not isinstance(expected_hash, str) or len(expected_hash) != 64:
                        expected_hash = None
                    owner.update({
                        "status": "awaiting_snapshot",
                        "expectedTurnId": expected_turn_id,
                        "expectedStateHash": expected_hash,
                    })
                    await target_ws.send(_j.dumps({
                        "type": "persist_snapshot",
                        "turnId": turn_id,
                        "expectedTurnId": expected_turn_id,
                    }, ensure_ascii=False))
                except Exception as exc:
                    disconnected = (
                        owner.get("status") == "disconnected"
                        or recovery_by_turn.get(turn_id) is not owner
                        or getattr(target_ws, "close_code", None) is not None
                    )
                    recovery_by_connection.pop(connection_key, None)
                    if recovery_by_turn.get(turn_id) is owner:
                        recovery_by_turn.pop(turn_id, None)
                    if disconnected:
                        # The durable pending replay and commit fence remain
                        # authoritative recovery proof. A replacement frontend
                        # may claim the turn and redispatch the exact same
                        # transaction; a transport disconnect is not evidence
                        # of an authority rejection and must not pause the game.
                        return
                    error = (
                        "Restored action recovery failed; game paused: "
                        f"{exc}"
                    )
                    runtime.pause(
                        "paused_snapshot_error", error,
                        turn_id=turn_id, pid=pid,
                    )
                    try:
                        await target_ws.send(_j.dumps({
                            "type": "snapshot_error",
                            "turnId": turn_id,
                            "error": error,
                            "paused": True,
                        }, ensure_ascii=False))
                    except Exception:
                        pass
            task = asyncio.create_task(
                recover(),
                name=f"bg-recover-{game_id}-{turn_id}",
            )
            recovery_tasks.add(task)
            task.add_done_callback(recovery_tasks.discard)

        def authenticate(msg: dict) -> bool:
            nonlocal authenticated, frontend_generation
            if not (
                _session_capability_matches(msg.get("gameId"), game_id)
                and _session_capability_matches(
                    msg.get("sessionCapability"), session_capability,
                )
            ):
                return False
            if not authenticated:
                authenticated = True
                authenticated_frontends.add(connection_id)
                frontend_generation += 1
                runtime.browser_chat_callback = send_browser_chat
                _write_live_event({
                    "type": "frontend_connected", "game_id": game_id,
                    "connection_id": connection_id,
                    "active_connections": len(authenticated_frontends),
                })
            return True

        async def send_browser_chat(event: dict) -> None:
            await ws.send(_j.dumps(event, ensure_ascii=False))

        async def send_retry_ready() -> None:
            retry_ready = runtime.manual_retry_ready()
            if not isinstance(retry_ready, dict):
                return
            await ws.send(_j.dumps({
                "type": "game_retry_ready",
                "gameId": game_id,
                "epoch": retry_ready["epoch"],
                "turnId": retry_ready["identity"].get("decisionId"),
                "pid": retry_ready["pid"],
            }, ensure_ascii=False))

        async def send_frontend_restore() -> None:
            record = runtime.store.read_snapshot()
            state = (
                copy.deepcopy(record.get("state"))
                if isinstance(record, dict) and isinstance(record.get("state"), dict)
                else None
            )
            turn_id = (
                record.get("turn_id")
                if isinstance(record, dict) and isinstance(record.get("turn_id"), str)
                else None
            )
            await ws.send(_j.dumps({
                "type": "frontend_restore",
                "gameId": game_id,
                "turnId": turn_id,
                "state": state,
            }, ensure_ascii=False))

        def message_authentication(msg: dict) -> tuple[bool, bool]:
            game_id_valid = _session_capability_matches(msg.get("gameId"), game_id)
            capability_valid = _session_capability_matches(
                msg.get("sessionCapability"), session_capability,
            )
            return game_id_valid, capability_valid

        try:
            async for raw in ws:
                try:
                    msg = _j.loads(raw)
                except Exception:
                    await ws.close(code=4003, reason="session authentication failed")
                    break
                game_id_valid, capability_valid = message_authentication(msg)
                if not capability_valid:
                    await ws.close(code=4003, reason="session authentication failed")
                    break
                if not game_id_valid:
                    await ws.close(code=4001, reason="gameId mismatch")
                    break
                if msg.get("type") == "frontend_hello":
                    if not authenticate(msg):
                        await ws.close(code=4001, reason="gameId mismatch")
                        break
                    await send_retry_ready()
                    continue
                if not authenticated:
                    await ws.close(code=4002, reason="frontend hello required")
                    break
                if msg.get("type") == "game_retry_probe":
                    await send_retry_ready()
                    continue
                if msg.get("type") == "frontend_sync":
                    await send_frontend_restore()
                    continue
                if msg.get("type") == "state_snapshot":
                    snapshot_payload = msg.get("state", {})
                    snapshot_wrapper = (
                        snapshot_payload.get("wrapper", snapshot_payload.get("st", {}))
                        if isinstance(snapshot_payload, dict)
                        else {}
                    )
                    if not snapshot_wrapper and isinstance(snapshot_payload, dict):
                        snapshot_game = snapshot_payload.get("game", {})
                        snapshot_wrapper = (
                            snapshot_game.get("wrapper", snapshot_game)
                            if isinstance(snapshot_game, dict)
                            else {}
                        )
                    pending_replay = runtime.store.read_pending_replay_turn()
                    pending_turn_id = (
                        str(pending_replay.get("turnId"))
                        if isinstance(pending_replay, dict)
                        and pending_replay.get("turnId") is not None
                        else None
                    )
                    snapshot_decision_id = (
                        snapshot_payload.get("decisionId")
                        if isinstance(snapshot_payload, dict)
                        else None
                    )
                    if not isinstance(snapshot_decision_id, str):
                        snapshot_decision_id = msg.get("turnId")
                    terminal_snapshot = snapshot_wrapper.get("phase") in {
                        "finished", "gameover",
                    }
                    if _snapshot_owned_by_another_connection(
                        turn_connections,
                        pending_turn_id=pending_turn_id,
                        snapshot_turn_id=snapshot_decision_id,
                        target_ws=ws,
                    ):
                        await ws.send(_j.dumps({
                            "type": "snapshot_error",
                            "turnId": snapshot_decision_id,
                            "error": "SNAPSHOT_TURN_ALREADY_OWNED",
                            "paused": False,
                        }, ensure_ascii=False))
                        continue
                    snapshot_viewer_seat = (
                        msg.get("viewerSeat")
                        if isinstance(msg.get("viewerSeat"), int)
                        else snapshot_wrapper.get("currentPlayer")
                    )
                    active_recovery = recovery_by_connection.get(connection_id)
                    if isinstance(active_recovery, dict):
                        recovery_snapshot_error = ""
                        recovery_status = active_recovery.get("status")
                        recovery_turn_id = active_recovery.get("turnId")
                        snapshot_turn_id = snapshot_payload.get("decisionId")
                        if recovery_status == "dispatching":
                            # The exact authority dispatch is already in
                            # flight. A duplicate pre-action snapshot must not
                            # trigger a second dispatch or receive an ack.
                            continue
                        if (
                            recovery_status == "awaiting_snapshot"
                            and snapshot_turn_id == recovery_turn_id
                        ):
                            pending = runtime.store.read_pending_replay_turn()
                            before_hash = (
                                pending.get("beforeHash")
                                if isinstance(pending, dict)
                                else None
                            )
                            incoming_hash = authority_hash(snapshot_payload)
                            if incoming_hash == before_hash:
                                await ws.send(_j.dumps({
                                    "type": "persist_snapshot",
                                    "turnId": recovery_turn_id,
                                    "expectedTurnId": active_recovery.get(
                                        "expectedTurnId",
                                    ),
                                }, ensure_ascii=False))
                                continue
                            expected_hash = active_recovery.get(
                                "expectedStateHash",
                            )
                            terminal_same_decision = (
                                snapshot_wrapper.get("phase")
                                in {"finished", "gameover"}
                            )
                            if not terminal_same_decision:
                                recovery_snapshot_error = (
                                    "same-decision authority changed without "
                                    "reaching a terminal state"
                                )
                            elif (
                                isinstance(expected_hash, str)
                                and incoming_hash != expected_hash
                            ):
                                recovery_snapshot_error = (
                                    "restored action snapshot hash mismatch"
                                )
                        expected_turn_id = active_recovery.get("expectedTurnId")
                        if (
                            isinstance(expected_turn_id, str)
                            and snapshot_turn_id != expected_turn_id
                        ):
                            recovery_snapshot_error = (
                                "restored action snapshot decision mismatch"
                            )
                        expected_hash = active_recovery.get("expectedStateHash")
                        if isinstance(expected_hash, str):
                            if authority_hash(snapshot_payload) != expected_hash:
                                recovery_snapshot_error = (
                                    "restored action snapshot hash mismatch"
                                )
                        if recovery_snapshot_error:
                            runtime.pause(
                                "paused_snapshot_error",
                                recovery_snapshot_error,
                                turn_id=str(snapshot_turn_id or ""),
                            )
                            await ws.send(_j.dumps({
                                "type": "snapshot_error",
                                "turnId": snapshot_turn_id,
                                "error": recovery_snapshot_error,
                                "paused": True,
                            }, ensure_ascii=False))
                            continue
                    if result_only:
                        # The terminal snapshot was validated before startup;
                        # this session is read-only and must not create a
                        # AuthorityWorker or persist a browser echo.
                        await ws.send(_j.dumps({
                            "type": "snapshot_ack", "turnId": msg.get("turnId"),
                        }))
                        continue
                    try:
                        snapshot_recovery = runtime.save_snapshot(
                            str(msg.get("turnId", "")),
                            snapshot_payload,
                            final_result=msg.get("finalResult"),
                            metadata=(
                                {
                                    "mode": mode,
                                    "manual_test": True,
                                    "rules_player_count": len(player_types),
                                    "player_types": list(player_types),
                                    "viewer_seat": (
                                        int(snapshot_viewer_seat)
                                        if isinstance(snapshot_viewer_seat, int)
                                        else None
                                    ),
                                }
                                if manual_test else None
                            ),
                        )
                        if isinstance(snapshot_recovery, dict):
                            schedule_restored_action_recovery(
                                snapshot_recovery, ws, connection_id,
                            )
                            continue
                        if active_recovery is not None:
                            recovery_by_connection.pop(connection_id, None)
                            recovery_turn_id = active_recovery.get("turnId")
                            if (
                                isinstance(recovery_turn_id, str)
                                and recovery_by_turn.get(recovery_turn_id)
                                is active_recovery
                            ):
                                recovery_by_turn.pop(recovery_turn_id, None)
                        for stale_turn_id in runtime.consume_stale_turn_retries():
                            stale_ws = turn_connections.get(stale_turn_id)
                            if stale_ws is None or _SERVER_STOP_EVENT.is_set():
                                continue
                            try:
                                await stale_ws.send(_j.dumps({
                                    "type": "ai_retry", "turnId": stale_turn_id,
                                    "reason": "STALE_AUTHORITY_FRAME",
                                    "paused": False,
                                    "expectedTurnId": msg.get("turnId"),
                                }, ensure_ascii=False))
                                _write_live_event({
                                    "type": "ai_turn_retry", "game_id": game_id,
                                    "turn_id": stale_turn_id,
                                    "reason": "confirmed_snapshot_superseded",
                                })
                            except Exception:
                                pass
                        for owned_turn_id in list(turn_connections):
                            if owned_turn_id != str(msg.get("turnId", "")):
                                owner_ws = turn_connections.get(owned_turn_id)
                                if owner_ws is not None:
                                    release_turn_owner(owned_turn_id, owner_ws)
                        if terminal_snapshot:
                            terminal_turn_id = str(msg.get("turnId", ""))
                            terminal_owner = turn_connections.get(terminal_turn_id)
                            if terminal_owner is not None:
                                release_turn_owner(terminal_turn_id, terminal_owner)
                            else:
                                pending_ai_turns[:] = [
                                    item for item in pending_ai_turns
                                    if item[1] != terminal_turn_id
                                ]
                    except Exception as exc:
                        error = f"Snapshot persistence failed; game paused: {exc}"
                        try:
                            runtime.pause(
                                "paused_snapshot_error", error,
                                turn_id=str(msg.get("turnId", "")),
                            )
                        except Exception:
                            runtime.status = "paused_snapshot_error"
                            _write_live_event({
                                "type": "game_paused", "game_id": game_id,
                                "turn_id": msg.get("turnId"), "error": error,
                                "status": "paused_snapshot_error",
                            })
                        await ws.send(_j.dumps({
                            "type": "snapshot_error", "turnId": msg.get("turnId"),
                            "error": error, "paused": True,
                        }, ensure_ascii=False))
                        continue
                    new_worker = None
                    if manual_test or terminal_snapshot:
                        # A terminal snapshot has no next decision to validate.
                        # Starting a fresh AuthorityWorker here only delays the
                        # acknowledgement and can race browser shutdown.
                        pass
                    else:
                        confirmed_record = runtime.store.read_snapshot()
                        confirmed_state = (
                            confirmed_record.get("state")
                            if isinstance(confirmed_record, dict)
                            else None
                        )
                        if not isinstance(confirmed_state, dict):
                            raise RuntimeError(
                                "confirmed snapshot worker state is unavailable",
                            )
                        try:
                            new_worker = await asyncio.to_thread(
                                AuthorityWorker,
                                definition,
                                confirmed_state,
                                decision_id=str(msg.get("turnId") or ""),
                            )
                        except Exception as exc:
                            _write_live_event({
                                "type": "authority_worker_failure", "game_id": game_id,
                                "turn_id": msg.get("turnId"), "error": str(exc),
                            })
                    async with authority_worker_lock:
                        prior_worker = authority_worker
                        authority_worker = new_worker
                    if prior_worker is not None:
                        try:
                            await asyncio.to_thread(prior_worker.close)
                        except Exception as exc:
                            _write_live_event({
                                "type": "authority_worker_close_error",
                                "game_id": game_id,
                                "turn_id": msg.get("turnId"),
                                "phase": "snapshot_rotation",
                                "error_type": type(exc).__name__,
                            })
                    await ws.send(_j.dumps({"type": "snapshot_ack", "turnId": msg.get("turnId")}))
                    if manual_test:
                        pending_ai_turns.clear()
                    elif authority_worker is not None and pending_ai_turns:
                        queued_turns = list(pending_ai_turns)
                        pending_ai_turns.clear()
                        for queued_pid, queued_turn_id, queued_hash, queued_ws in queued_turns:
                            schedule_ai_turn(
                                queued_pid, queued_turn_id, queued_hash, queued_ws,
                            )
                    elif authority_worker is None and pending_ai_turns:
                        queued_turns = list(pending_ai_turns)
                        pending_ai_turns.clear()
                        for queued_pid, queued_turn_id, _queued_hash, queued_ws in queued_turns:
                            release_turn_owner(queued_turn_id, queued_ws)
                            await queued_ws.send(_j.dumps({
                                "type": "ai_retry", "pid": queued_pid,
                                "turnId": queued_turn_id,
                                "reason": "AUTHORITY_WORKER_UNAVAILABLE",
                                "expectedTurnId": msg.get("turnId"),
                                "paused": False,
                            }, ensure_ascii=False))
                    continue
                if msg.get("type") == "action_validation_result":
                    waiter = validation_waiters.get(str(msg.get("requestId", "")))
                    if waiter and waiter[1] == connection_id and not waiter[0].done():
                        waiter[0].set_result(msg.get("result") or {})
                    continue
                if msg.get('type') == 'game_chat_history':
                    await ws.send(_j.dumps({'type': 'game_chat_history', 'events': getattr(runtime, 'public_chat_history', [])}, ensure_ascii=False))
                    continue
                if msg.get("type") == "game_chat":
                    try:
                        human_pids = [
                            pid for pid, kind in enumerate(player_types)
                            if kind == "human"
                        ]
                        sender_pid = (
                            human_pids[0]
                            if not manual_test and len(human_pids) == 1
                            else int(msg.get("fromPid"))
                        )
                        result = runtime.route_human_chat(
                            sender_pid, msg.get("toPid"),
                            str(msg.get("message", "")),
                            **({'client_id': msg['clientId']} if 'clientId' in msg else {}),
                        )
                    except Exception as exc:
                        await ws.send(_j.dumps({
                            "type": "chat_error", "error": str(exc),
                            **({'clientId': msg['clientId']} if 'clientId' in msg else {}),
                        }, ensure_ascii=False))
                    else:
                        await ws.send(_j.dumps({
                            "type": "chat_ack", "message": result,
                            **({'clientId': msg['clientId']} if 'clientId' in msg else {}),
                        }, ensure_ascii=False))
                    continue
                if msg.get("type") == "game_control":
                    command = str(msg.get("command", ""))
                    if command not in {"rematch", "end"}:
                        await ws.send(_j.dumps({
                            "type": "game_control_error", "error": "invalid game control request",
                        }))
                        continue
                    if runtime.status != "finished":
                        await ws.send(_j.dumps({
                            "type": "game_control_error", "error": "game is not finished",
                        }))
                        continue
                    await ws.send(_j.dumps({
                        "type": "game_control_ack", "command": command,
                        "message": "正在创建新对局" if command == "rematch" else "正在关闭对局",
                    }, ensure_ascii=False))
                    target = rematch_game if command == "rematch" else stop_game
                    threading.Thread(
                        target=target,
                        name=f"bg-browser-{command}",
                        daemon=True,
                    ).start()
                    continue
                if msg.get("type") != "ai_turn":
                    continue
                if result_only:
                    await ws.send(_j.dumps({
                        "type": "ai_error", "pid": msg.get("pid", 0),
                        "turnId": msg.get("turnId"),
                        "error": "result-only session does not accept AI turns",
                        "paused": True,
                    }, ensure_ascii=False))
                    continue

                if manual_test:
                    await ws.send(_j.dumps({
                        "type": "ai_error", "pid": msg.get("pid", 0),
                        "turnId": msg.get("turnId"),
                        "error": "manual-test has no AI turns or provider path",
                        "paused": False,
                    }, ensure_ascii=False))
                    continue

                pid = msg.get("pid", 0)
                state = msg.get("state", {})
                state_wrapper = (
                    state.get("wrapper", state.get("st", {}))
                    if isinstance(state, dict) else {}
                )
                turn_id = str(
                    msg.get("turnId")
                    or f"{state_wrapper.get('turn', 0)}:{pid}"
                )
                if isinstance(pid, int) and not isinstance(pid, bool):
                    if not await await_fenced_attempt(turn_id):
                        await ws.send(_j.dumps({
                            "type": "ai_retry", "pid": pid, "turnId": turn_id,
                            "reason": "AI_ATTEMPT_DRAINING", "paused": False,
                            "expectedTurnId": turn_id,
                        }, ensure_ascii=False))
                        continue
                    retry_reason = (
                        "frontend_disconnected"
                        if reopen_fenced_agent_turn(turn_id, pid)
                        else None
                    )
                else:
                    retry_reason = None
                try:
                    claimed_authority_hash = authority_hash(state)
                    authority_frame = runtime.bind_confirmed_ai_turn(
                        pid, turn_id, state,
                    )
                except AuthorityBindingError as exc:
                    await send_stale_authority(
                        pid, turn_id, ws,
                        expected_turn_id=exc.expected_turn_id,
                    )
                    continue
                if not _reserve_ai_turn_owner(turn_connections, turn_id, ws):
                    await ws.send(_j.dumps({
                        "type": "ai_retry", "pid": pid, "turnId": turn_id,
                        "reason": "AI_TURN_ALREADY_OWNED", "paused": False,
                        "expectedTurnId": turn_id,
                    }, ensure_ascii=False))
                    continue
                # Reserve the route before checking worker readiness.  A
                # second tab cannot replace this owner while the first frame
                # waits for a host worker to start.
                ai_call_count += 1
                if authority_worker is None:
                    # A previous snapshot worker build may have failed.  The
                    # owned process task rebuilds once from this exact confirmed
                    # frame; on failure it releases ownership and returns a
                    # non-pausing retry instead of entering an orphaned queue.
                    schedule_ai_turn(
                        pid, turn_id, claimed_authority_hash, ws,
                        authority_frame, retry_reason,
                    )
                    continue
                schedule_ai_turn(
                    pid, turn_id, claimed_authority_hash, ws,
                    authority_frame, retry_reason,
                )

        except Exception as e:
            if not isinstance(e, ConnectionClosed):
                import traceback; traceback.print_exc()
        finally:
            for owned_turn_id, owner_ws in list(turn_connections.items()):
                if owner_ws is ws:
                    await fence_disconnected_attempt(owned_turn_id, ws)
                    release_turn_owner(owned_turn_id, ws)
            pending_ai_turns[:] = [
                item for item in pending_ai_turns if item[3] is not ws
            ]
            recovery_owner = recovery_by_connection.pop(connection_id, None)
            if isinstance(recovery_owner, dict):
                recovery_owner["status"] = "disconnected"
                recovery_turn_id = recovery_owner.get("turnId")
                if (
                    isinstance(recovery_turn_id, str)
                    and recovery_by_turn.get(recovery_turn_id) is recovery_owner
                ):
                    recovery_by_turn.pop(recovery_turn_id, None)
            for request_id, (future, owner) in list(validation_waiters.items()):
                if owner == connection_id and not future.done():
                    future.set_exception(RuntimeError("authoritative frontend disconnected"))
            if authenticated and runtime.browser_chat_callback is send_browser_chat:
                runtime.browser_chat_callback = None
            if authenticated:
                authenticated_frontends.discard(connection_id)
                frontend_generation += 1
                disconnected_generation = frontend_generation
                await asyncio.sleep(3)
                if (
                    disconnected_generation == frontend_generation
                    and not authenticated_frontends
                ):
                    _write_live_event({
                        "type": "frontend_disconnected", "game_id": game_id,
                        "stopping": _SERVER_STOP_EVENT.is_set(),
                        "close_code": getattr(ws, "close_code", None),
                        "close_reason": getattr(ws, "close_reason", None),
                        "grace_seconds": 3,
                    })

    ws_server = await websockets.serve(
        ws_handler, "127.0.0.1", 7333,
        ping_interval=30, ping_timeout=10,
        origins=["http://localhost:8080", "http://127.0.0.1:8080"],
    )
    try:
        http_server = _HTTP_SERVER_CLASS(("127.0.0.1", 8080), _GameHandler)
    except Exception:
        ws_server.close()
        await ws_server.wait_closed()
        raise
    threading.Thread(target=http_server.serve_forever, daemon=True).start()
    all_ai = all(t == "ai" for t in player_types)
    mode_label = (
        "MANUAL-TEST (all human seats)" if manual_test
        else ("SPECTATOR (AI vs AI)" if all_ai else "Human vs AI")
    )
    print(f"\n  === {definition.title} {mode_label} ===")
    print("  HTTP: http://localhost:8080")
    print("  WS:   ws://localhost:7333")
    if all_ai:
        print(f"  Delay: {ai_delay}ms between AI turns")

    if not result_only:
        store.update_manifest(
            status="active", player_types=player_types, players=player_names,
            http_port=8080, ws_port=7333, ai_delay=ai_delay, error=None,
            mode=mode, manual_test=manual_test, rules_player_count=pc,
            team_name=team_name, teammate_sessions={} if manual_test else None,
            model=None if manual_test else model,
            provider_policy=(
                release_provider_policy.to_settings()
                if not manual_test else None
            ),
            pause_turn_id=None,
            pause_pid=None,
            pause_issue=None,
        )
    _write_live_event({
        "type": "server_ready", "game_id": game_id,
        "engine": definition.id, "title": definition.title,
        "url": "http://localhost:8080",
    })
    _SERVER_READY_EVENT.set()
    try:
        while not _SERVER_STOP_EVENT.is_set():
            await asyncio.sleep(0.25)
    finally:
        # Release the user-facing ports first. Memory/skill maintenance may still
        # need a few seconds, but closing a game must immediately close its UI.
        ws_server.close()
        ws_wait_task = asyncio.create_task(
            ws_server.wait_closed(), name=f"bg-ws-close-{game_id}"
        )
        _, ws_pending = await asyncio.wait({ws_wait_task}, timeout=3.0)
        if ws_pending:
            ws_wait_task.cancel()
            _write_live_event({
                "type": "ws_server_close_timeout",
                "game_id": game_id,
                "timeout_seconds": 3,
            })
        http_shutdown_task = asyncio.create_task(
            asyncio.to_thread(http_server.shutdown),
            name=f"bg-http-shutdown-{game_id}",
        )
        _, http_pending = await asyncio.wait({http_shutdown_task}, timeout=2.0)
        if http_pending:
            http_shutdown_task.cancel()
            _write_live_event({
                "type": "http_server_shutdown_timeout",
                "game_id": game_id,
                "timeout_seconds": 2,
            })
        http_server.server_close()
        authority_close_task = None
        if authority_worker is not None:
            authority_close_task = asyncio.create_task(
                asyncio.to_thread(authority_worker.close),
                name=f"bg-authority-close-{game_id}",
            )
        shutdown_task = asyncio.create_task(
            runtime.stop(), name=f"bg-runtime-stop-{game_id}"
        )
        shutdown_tasks = {shutdown_task}
        if authority_close_task is not None:
            shutdown_tasks.add(authority_close_task)
        _, pending = await asyncio.wait(shutdown_tasks, timeout=5.0)
        if authority_close_task is not None and authority_close_task in pending:
            authority_close_task.cancel()
            _write_live_event({
                "type": "authority_worker_close_timeout",
                "game_id": game_id,
                "timeout_seconds": 5,
            })
        if shutdown_task in pending:
            # Do not use wait_for here: it waits for a cancellation-resistant
            # provider task before returning, which can leave the manifest
            # visibly active after the owned ports have already closed.
            shutdown_task.cancel()
            runtime.status = "stopped"
            if not result_only:
                runtime.store.update_manifest(
                    status="stopped", usage=runtime._usage_summary(),
                )
            _write_live_event({
                "type": "runtime_stop_timeout",
                "game_id": game_id,
                "timeout_seconds": 5,
            })
            _ACTIVE_RUNTIME = None
        else:
            shutdown_task.result()
        _deactivate_game_team(team_name)
        _SERVER_RUNNING = False
        _ACTIVE_RUNTIME = None
        _clear_active_session_capability(session_capability)
        await asyncio.sleep(0.3)

    return (
        f"{definition.title} game ended!\n"
        f"  AI decisions: {ai_call_count}\n"
        f"  Total turns: {turn_count}\n"
        f"  Tool stats: {_j.dumps(tool_stats, ensure_ascii=False)}"
    )


# ====================================================================
# Tool 入口
# ====================================================================

def _bg_tool_call(args: dict) -> str:
    global _START_IN_PROGRESS
    from bglab.games.persistence.store import GameStore
    from bglab.games.skills.loader import get_skillset_version
    import uuid

    from bglab.session.state import session_state

    mode = args.get("mode", "ai_vs_ai")
    if mode == "replay":
        return start_replay(args.get("game_id"))
    engine = str(args.get("engine", "splendor"))
    try:
        definition = _resolve_game_definition(engine)
        engine = definition.id
    except GameRegistryError as exc:
        return f"ERROR: {exc}"
    try:
        player_count = int(args.get("player_count", 2))
    except (TypeError, ValueError):
        return (
            "ERROR: player_count must be an integer from "
            f"{definition.min_players} to {definition.max_players}."
        )
    if not definition.min_players <= player_count <= definition.max_players:
        return (
            f"ERROR: {definition.title} supports {definition.min_players}-"
            f"{definition.max_players} total players."
        )
    if mode not in {"ai_vs_ai", "human_vs_ai", "manual-test"}:
        return f"ERROR: unsupported game mode: {mode}"
    manual_test = mode == "manual-test"
    human_mode = mode == "human_vs_ai"
    try:
        delay = int(args.get("delay", 0 if manual_test else (300 if human_mode else 800)))
    except (TypeError, ValueError):
        return "ERROR: delay must be an integer."
    raw_seed = args.get("seed")
    if raw_seed is None:
        seed = secrets.randbits(32)
    else:
        try:
            seed = int(raw_seed)
        except (TypeError, ValueError):
            return "ERROR: seed must be an integer from 0 to 4294967295."
        if isinstance(raw_seed, bool) or not 0 <= seed <= 0xFFFFFFFF:
            return "ERROR: seed must be an integer from 0 to 4294967295."
    # Never wait for the owned server thread while holding _SERVER_LOCK.  Its
    # finalizer takes the same lock to clear the active capability; joining it
    # under the lock deadlocks until the caller's timeout and prevents an
    # otherwise valid same-session game switch.
    with _SERVER_LOCK:
        if _START_IN_PROGRESS:
            return "ERROR: another BGLab game is still starting."
        active_game_id = _ACTIVE_GAME_ID
        active_server = bool(_SERVER_THREAD and _SERVER_THREAD.is_alive())
    if active_server:
        stopped = _stop_game_server(end_session=False)
        if not stopped.startswith("Game stopped"):
            return (
                f"ERROR: cannot start a new game while game "
                f"{active_game_id or ''} is active: {stopped}"
            )
    with _SERVER_LOCK:
        if _START_IN_PROGRESS:
            return "ERROR: another BGLab game is still starting."
        if _SERVER_THREAD and _SERVER_THREAD.is_alive():
            return (
                f"ERROR: cannot start a new game while game "
                f"{_ACTIVE_GAME_ID or ''} is active."
            )
        _START_IN_PROGRESS = True
        _START_CANCEL_EVENT.clear()
    pt = (
        ["human"] * player_count if manual_test
        else (["human"] + ["ai"] * (player_count - 1)) if human_mode
        else ["ai"] * player_count
    )
    from bglab.session.settings import load_player_nickname
    pn = (
        [f"测试玩家 {i+1}" for i in range(player_count)] if manual_test
        else ([load_player_nickname()] + [f"AI {i}" for i in range(1, player_count)]) if human_mode
        else [f"AI {i+1}" for i in range(player_count)]
    )
    model = None if manual_test else str(args.get("model") or session_state.model or "deepseek-chat")
    game_id = uuid.uuid4().hex[:8]
    store = GameStore(game_id)
    store.write_manifest(
        engine=engine, pc=player_count, names=pn,
        skillset_version=get_skillset_version(engine),
    )
    store.update_manifest(
        player_types=pt,
        game_title=definition.title,
        adapter_protocol=definition.adapter_protocol,
        snapshot_version=definition.snapshot_version,
        status="starting",
        mode=mode,
        manual_test=manual_test,
        rules_player_count=player_count,
        provider="none" if manual_test else "configured",
        ai_enabled=not manual_test,
        model=model,
        seed=seed,
        ai_delay=delay,
        action_profile=definition.action_profile,
        game_package_fingerprint=package_runtime_fingerprint(definition),
    )
    team_name = None
    if manual_test:
        store.update_manifest(
            system_prompt_fingerprint=None,
            team_name=None,
            teammate_sessions={},
        )
    else:
        from bglab.games.prompt import build_game_system_prompt
        store.update_manifest(
            system_prompt_fingerprint=hashlib.sha256(
                build_game_system_prompt(model=model).encode("utf-8")
            ).hexdigest()[:16],
        )
        try:
            team_name = _ensure_game_team(
                game_id, pt, pn, model=model or "deepseek-chat", engine=definition.id,
            )
        except Exception as exc:
            _finish_start()
            store.update_manifest(status="paused_start_error", error=str(exc))
            return f"ERROR: {exc}"
    store.update_manifest(
        team_name=team_name,
        teammate_sessions={
            str(pid): f"game-{game_id}-p{pid}"
            for pid, player_type in enumerate(pt) if player_type == "ai"
        },
    )
    if _START_CANCEL_EVENT.is_set():
        _finish_start()
        _deactivate_game_team(team_name)
        store.update_manifest(
            status="stopped", error="start cancelled before server startup",
        )
        return "Game start cancelled before server startup. Code Agent mode restored."
    try:
        error = _start_server_thread(
            definition, pt, pn, delay, game_id=game_id, model=model, mode=mode,
            session_capability=args.get("_session_capability"),
        )
    except Exception as exc:
        _finish_start()
        _deactivate_game_team(team_name)
        store.update_manifest(status="paused_start_error", error=str(exc))
        return f"ERROR: {exc}"
    if error:
        _finish_start()
        _deactivate_game_team(team_name)
        store.update_manifest(status="paused_start_error", error=error)
        return f"ERROR: {error}"
    health_error = _verify_frontend_health(definition)
    if health_error:
        stopped = _stop_game_server(end_session=False)
        detail = f"{health_error}; cleanup: {stopped}"
        _deactivate_game_team(team_name)
        store.update_manifest(status="paused_start_error", error=detail)
        _write_live_event({
            "type": "game_start_failed", "game_id": game_id,
            "error": health_error, "cleanup": stopped,
        })
        _finish_start()
        return f"ERROR: game startup health check failed: {detail}"
    _finish_start()
    _write_live_event({
        "type": "game_started", "game_id": game_id,
        "engine": definition.id, "title": definition.title,
        "url": "http://localhost:8080",
    })
    # The TUI reports the URL; the user decides when to open the game page.
    return (
        f"{definition.title}已启动（game_id={game_id}，seed={seed}，{player_count}人，"
        f"{'手动测试模式：你控制全部席位' if manual_test else ('1人类+' + str(player_count-1) + 'AI' if human_mode else str(player_count) + 'AI')}）。\n"
        f"游戏页面: {_session_url()}\n"
        "Code Agent 已进入游戏阻塞状态。关闭: /bg stop"
    )


def start_replay(game_id: str | None = None) -> str:
    """Serve one verified completed game, or the local completed-game selector."""
    global _SERVER_THREAD, _SERVER_ERROR, _ACTIVE_GAME_ID
    global _SERVER_RUNNING, _ACTIVE_RUNTIME, _ACTIVE_SESSION_CAPABILITY

    from bglab.games.replay import ReplayAuditError
    from bglab.games.replay_ui import make_replay_handler

    normalized = game_id.strip() if isinstance(game_id, str) else None
    if normalized == "":
        normalized = None
    capability = secrets.token_urlsafe(32)
    with _SERVER_LOCK:
        if _SERVER_THREAD and _SERVER_THREAD.is_alive():
            return (
                f"ERROR: game {_ACTIVE_GAME_ID or ''} is already active. "
                "Use /bg stop first."
            )
        try:
            handler = make_replay_handler(normalized, capability)
        except ReplayAuditError as exc:
            return f"ERROR: {exc}"

        _SERVER_STOP_EVENT.clear()
        _SERVER_READY_EVENT.clear()
        _SERVER_ERROR = None
        _ACTIVE_GAME_ID = normalized
        _ACTIVE_RUNTIME = None
        _ACTIVE_SESSION_CAPABILITY = capability

        def _bg() -> None:
            global _SERVER_THREAD, _SERVER_ERROR, _ACTIVE_GAME_ID
            global _SERVER_RUNNING, _ACTIVE_RUNTIME, _ACTIVE_SESSION_CAPABILITY
            server: ThreadingHTTPServer | None = None
            try:
                server = _HTTP_SERVER_CLASS(("127.0.0.1", 8080), handler)
                server.timeout = 0.25
                _SERVER_RUNNING = True
                _SERVER_READY_EVENT.set()
                while not _SERVER_STOP_EVENT.is_set():
                    server.handle_request()
            except Exception as exc:
                _SERVER_ERROR = str(exc)
                _SERVER_READY_EVENT.set()
            finally:
                if server is not None:
                    server.server_close()
                _SERVER_RUNNING = False
                _ACTIVE_RUNTIME = None
                _SERVER_THREAD = None
                _ACTIVE_GAME_ID = None
                _clear_active_session_capability(capability)
                if _SERVER_ERROR:
                    _GAME_SESSION_DONE_EVENT.set()

        thread = threading.Thread(target=_bg, name="bg-replay", daemon=True)
        _SERVER_THREAD = thread
        thread.start()
    if not _SERVER_READY_EVENT.wait(timeout=8):
        _SERVER_STOP_EVENT.set()
        thread.join(timeout=2)
        return "ERROR: replay server readiness timed out"
    if _SERVER_ERROR:
        thread.join(timeout=2)
        return f"ERROR: {_SERVER_ERROR}"
    path = "/replay?capability=" + quote(capability, safe="")
    if normalized is not None:
        path += "&gameId=" + quote(normalized, safe="")
    return (
        f"历史对局回放已启动"
        f"{f'（game_id={normalized}）' if normalized else ''}。\n"
        f"回放页面: http://localhost:8080{path}\n"
        "这是只读回放，不调用模型；每次点击“下一回合”推进一个完整 AI turn。"
    )


def _start_server_thread(definition: GameDefinition, player_types: list[str],
                         player_names: list[str], ai_delay: int,
                         game_id: str, restore: bool = False,
                         model: str | None = "deepseek-chat",
                         mode: str = "human_vs_ai",
                         result_only: bool = False,
                         result_evidence: dict | None = None,
                         session_capability: str | None = None) -> str | None:
    """Start the game server in a background thread."""
    global _SERVER_THREAD, _SERVER_ERROR, _ACTIVE_GAME_ID
    global _ACTIVE_SESSION_CAPABILITY
    _SERVER_STOP_EVENT.clear()
    _SERVER_READY_EVENT.clear()
    _SERVER_ERROR = None
    _ACTIVE_GAME_ID = game_id

    def _bg():
        global _SERVER_ERROR, _SERVER_THREAD, _ACTIVE_GAME_ID
        global _SERVER_RUNNING, _ACTIVE_RUNTIME, _ACTIVE_SESSION_CAPABILITY
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(
                _run_ai_server(definition, player_types, player_names, ai_delay,
                               game_id=game_id, restore=restore, model=model,
                               mode=mode, result_only=result_only,
                               result_evidence=result_evidence,
                               session_capability=session_capability))
        except Exception as e:
            _SERVER_ERROR = str(e)
            _SERVER_READY_EVENT.set()
            print(f"Game server error: {e}")
        finally:
            if _ACTIVE_RUNTIME is not None:
                try:
                    loop.run_until_complete(_ACTIVE_RUNTIME.stop())
                except Exception:
                    pass
            _ACTIVE_RUNTIME = None
            _SERVER_RUNNING = False
            with _SERVER_LOCK:
                finishing_capability = _ACTIVE_SESSION_CAPABILITY
            _clear_active_session_capability(finishing_capability)
            # A stopped game must not leave fire-and-forget query helpers on
            # the owned event loop. Cancel to quiescence because closing an
            # async generator can itself schedule one final cleanup task.
            for _ in range(3):
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                if not pending:
                    break
                for task in pending:
                    task.cancel()
                try:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True),
                    )
                except Exception:
                    pass
            loop.close()
            _SERVER_THREAD = None
            _ACTIVE_GAME_ID = None
            if _SERVER_ERROR:
                _GAME_SESSION_DONE_EVENT.set()
    t = threading.Thread(target=_bg, name="bg-srv", daemon=True)
    _SERVER_THREAD = t
    t.start()
    if not _SERVER_READY_EVENT.wait(timeout=8):
        _SERVER_STOP_EVENT.set()
        t.join(timeout=2)
        return "game server readiness timed out"
    return _SERVER_ERROR


def resume_game(game_id: str | None = None) -> str:
    from bglab.games.persistence import store as store_module
    from bglab.games.persistence.store import GameStore

    restart_paused = False
    with _SERVER_LOCK:
        active_thread = _SERVER_THREAD
        active_game_id = _ACTIVE_GAME_ID
        active_status = getattr(_ACTIVE_RUNTIME, "status", "") if _ACTIVE_RUNTIME else ""
    if active_thread and active_thread.is_alive():
        if game_id and game_id != active_game_id:
            return f"ERROR: game {active_game_id or ''} is already active."
        if str(active_status).startswith("paused"):
            game_id = active_game_id
            restart_paused = True
        else:
            return (
                f"Game {active_game_id} is already active; frontend available: "
                f"{_session_url()}"
            )
    if game_id:
        game_id = game_id.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", game_id):
            return "ERROR: invalid game_id."
        if not (store_module.GAMES_DIR / game_id / "manifest.json").is_file():
            return f"ERROR: game {game_id} does not exist."
        store = GameStore.from_existing_dir(game_id, store_module.GAMES_DIR / game_id)
    else:
        unfinished = GameStore.unfinished()
        if not unfinished:
            return "ERROR: no unfinished game found."
        if len(unfinished) > 1:
            ids = ", ".join(item.game_id for item in unfinished)
            return (
                "ERROR: resume selection required; multiple unfinished games: "
                f"{ids}. Use /bg resume <game_id>."
            )
        store = unfinished[0]
    try:
        manifest = store.read_manifest()
        snapshot = store.read_snapshot()
    except Exception as exc:
        return f"ERROR: game {store.game_id} persistence is unreadable: {exc}"
    if manifest.get("game_id") != store.game_id:
        return f"ERROR: game {store.game_id} manifest identity mismatch."
    try:
        definition = get_game(str(manifest.get("engine", "")))
    except GameRegistryError as exc:
        return f"ERROR: game {store.game_id} uses an unsupported engine: {exc}"
    is_finished = manifest.get("status") == "finished" or bool(manifest.get("finished_at"))
    if not is_finished:
        try:
            require_resume_package_identity(definition, manifest)
        except (PackageIdentityError, ResumePackageIdentityError) as exc:
            return f"ERROR: game {store.game_id} resume refused: {exc}"
    if restart_paused:
        stopped = _stop_game_server(end_session=False)
        if stopped != "Game stopped. Code Agent mode restored.":
            return f"ERROR: cannot restart paused game: {stopped}"
    if is_finished:
        if manifest.get("status") != "finished" or not manifest.get("finished_at"):
            return f"ERROR: game {store.game_id} has an incomplete finished marker."
        try:
            validated = _validate_finished_result_store(store, definition)
        except (OSError, TypeError, ValueError, RuntimeError) as exc:
            return f"ERROR: game {store.game_id} finished result refused: {exc}"
        player_types = validated["playerTypes"]
        names = validated["players"]
        error = _start_server_thread(
            definition, player_types, names,
            int(manifest.get("ai_delay") or 0), game_id=store.game_id,
            restore=True, model=None,
            mode=str(manifest.get("mode") or "ai_vs_ai"), result_only=True,
            result_evidence=validated,
        )
        if error:
            return f"ERROR: game {store.game_id} result session failed: {error}"
        health_error = _verify_frontend_health(definition)
        if health_error:
            stopped = _stop_game_server(end_session=False)
            return (
                f"ERROR: game {store.game_id} result session health failed: "
                f"{health_error}; cleanup: {stopped}"
            )
        return f"Game {store.game_id} result-only session: http://localhost:8080"
    if not snapshot:
        return f"ERROR: game {store.game_id} has no confirmed frontend snapshot."
    snapshot_state = snapshot.get("state") if isinstance(snapshot, dict) else None
    snapshot_wrapper = snapshot_state.get("st", snapshot_state.get("wrapper", {})) if isinstance(snapshot_state, dict) else {}
    if not isinstance(snapshot_state, dict) or not isinstance(snapshot_wrapper, dict) or "turn" not in snapshot_wrapper:
        return f"ERROR: game {store.game_id} snapshot is invalid."
    snapshot_version = snapshot_state.get(
        "schemaVersion", snapshot_state.get("v", definition.snapshot_version),
    )
    if snapshot_version not in (definition.restore_versions or (definition.snapshot_version,)):
        return (
            f"ERROR: game {store.game_id} snapshot version {snapshot_version} "
            f"is incompatible with {definition.title} version {definition.snapshot_version}."
        )
    player_types = manifest.get("player_types")
    manual_test = bool(manifest.get("manual_test")) or manifest.get("mode") == "manual-test"
    if (
        not isinstance(player_types, list)
        or not definition.min_players <= len(player_types) <= definition.max_players
        or any(item not in {"human", "ai"} for item in player_types)
    ):
        return f"ERROR: game {store.game_id} lineup is invalid."
    if manual_test and any(kind != "human" for kind in player_types):
        return f"ERROR: game {store.game_id} manual-test lineup is invalid."
    names = manifest.get("players") or [f"P{i}" for i in range(len(player_types))]
    if not isinstance(names, list) or len(names) != len(player_types):
        return f"ERROR: game {store.game_id} player names are invalid."
    model = None if manual_test else str(manifest.get("model") or "deepseek-chat")
    team_name = None
    if not manual_test:
        try:
            team_name = _ensure_game_team(
                store.game_id, player_types, names,
                model=model or "deepseek-chat", engine=definition.id,
            )
        except Exception as exc:
            store.update_manifest(status="paused_start_error", error=str(exc))
            return f"ERROR: {exc}"
    store.update_manifest(
        team_name=team_name,
        teammate_sessions={} if manual_test else manifest.get("teammate_sessions"),
        mode="manual-test" if manual_test else manifest.get("mode", "human_vs_ai"),
        manual_test=manual_test,
        rules_player_count=len(player_types),
        provider="none" if manual_test else manifest.get("provider", "configured"),
        ai_enabled=not manual_test,
        model=model,
    )
    error = _start_server_thread(
        definition, player_types, names,
        int(manifest.get("ai_delay", 800)), game_id=store.game_id, restore=True,
        model=model, mode="manual-test" if manual_test else manifest.get("mode", "human_vs_ai"),
    )
    if error:
        store.update_manifest(status="paused_start_error", error=error)
        return f"ERROR: {error}"
    health_error = _verify_frontend_health(definition)
    if health_error:
        stopped = _stop_game_server(end_session=False)
        detail = f"{health_error}; cleanup: {stopped}"
        store.update_manifest(status="paused_start_error", error=detail)
        _write_live_event({
            "type": "game_resume_failed", "game_id": store.game_id,
            "error": health_error, "cleanup": stopped,
        })
        return f"ERROR: game resume health check failed: {detail}"
    store.update_manifest(
        status="active", resumed_at=time.time(), error=None,
        mode="manual-test" if manual_test else manifest.get("mode", "human_vs_ai"),
        manual_test=manual_test,
    )
    _write_live_event({
        "type": "game_resumed", "game_id": store.game_id,
        "engine": definition.id, "title": definition.title,
        "url": "http://localhost:8080",
        "turn_id": store.read_manifest().get("last_confirmed_turn_id"),
    })
    return f"Game {store.game_id} resumed: {_session_url()}"


def retry_game(game_id: str | None = None) -> str:
    """Authorize one exact paused provider retry, then strictly restore it."""
    from bglab.games.persistence import store as store_module
    from bglab.games.runtime import (
        authorize_persisted_manual_retry,
    )
    from bglab.games.adapter_process import AdapterProcess
    from bglab.games.replay import authority_hash

    with _SERVER_LOCK:
        active_id = _ACTIVE_GAME_ID
        active_status = (
            getattr(_ACTIVE_RUNTIME, "status", "") if _ACTIVE_RUNTIME else ""
        )
    requested = game_id.strip() if isinstance(game_id, str) else active_id
    if not requested:
        latest = GameStore.latest_unfinished()
        requested = latest.game_id if latest is not None else None
    if not requested or _GAME_ID_RE.fullmatch(requested) is None:
        return "ERROR: no valid paused game is available for retry."
    if active_id and requested != active_id:
        return f"ERROR: game {active_id} is already active."
    if not (store_module.GAMES_DIR / requested / "manifest.json").is_file():
        return f"ERROR: game {requested} does not exist."
    store = GameStore.from_existing_dir(requested, store_module.GAMES_DIR / requested)
    try:
        manifest = store.read_manifest()
        snapshot_record = store.read_snapshot()
    except Exception:
        return f"ERROR: game {requested} persistence is unreadable."
    if manifest.get("game_id") != requested:
        return f"ERROR: game {requested} manifest identity mismatch."
    if manifest.get("status") != "paused_api_error":
        return f"ERROR: game {requested} is not paused by an API failure."
    if active_id == requested and active_status != "paused_api_error":
        return f"ERROR: game {requested} is not paused by an API failure."
    if store.read_pending_replay_turn() is not None:
        return "ERROR: game has an unconfirmed action; use /bg resume instead."
    pid = manifest.get("pause_pid")
    turn_id = manifest.get("pause_turn_id")
    snapshot = (
        snapshot_record.get("state")
        if isinstance(snapshot_record, dict)
        else None
    )
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or not isinstance(turn_id, str)
        or not turn_id
        or not isinstance(snapshot, dict)
    ):
        return "ERROR: paused game is missing exact retry identity."
    agent_state = store.read_agent_state(pid)
    if not isinstance(agent_state, dict):
        return "ERROR: paused game agent state is missing."
    attempt = agent_state.get("attempt")
    identity = attempt.get("identity") if isinstance(attempt, dict) else None
    if not isinstance(identity, dict) or identity.get("decisionId") != turn_id:
        return "ERROR: paused game retry identity mismatch."
    try:
        definition = get_game(str(manifest.get("engine", "")))
        require_resume_package_identity(definition, manifest)
        player_types = manifest.get("player_types")
        names = manifest.get("players")
        if not isinstance(player_types, list) or not isinstance(names, list):
            raise ValueError("lineup is invalid")
        _release_agent_profiles(manifest, len(player_types), restore=True)
        adapter = AdapterProcess(definition)
        try:
            adapter.restore(snapshot)
            adapter_view = adapter.view(pid)
        finally:
            adapter.close()
        if not isinstance(adapter_view, dict):
            raise ValueError("adapter view is invalid")
        if adapter_view.get("decisionId") != turn_id:
            raise ValueError("confirmed adapter decision does not match retry identity")
        model_state = copy.deepcopy(snapshot)
        model_state["adapterView"] = adapter_view
        expected_state_hash = authority_hash(snapshot)
        expected_identity = {
            "gameId": requested,
            "decisionId": turn_id,
            "seat": pid,
            "stateHash": expected_state_hash,
        }
        if identity != expected_identity:
            raise ValueError("confirmed snapshot does not match retry identity")
        with _SERVER_LOCK:
            active_runtime = (
                _ACTIVE_RUNTIME if _ACTIVE_GAME_ID == requested else None
            )
        if active_runtime is not None:
            agent = active_runtime.agents.get(pid)
            if agent is None:
                raise ValueError("paused seat is not an AI player")
            epoch = agent.authorize_manual_retry(expected_identity)
        else:
            epoch = authorize_persisted_manual_retry(
                store, pid, expected_identity,
            )
        store.update_manifest(
            status="paused_api_error",
            manual_retry_epoch=epoch,
            manual_retry_status="authorized",
            retry_pause_issue=manifest.get("pause_issue"),
        )
    except Exception as exc:
        return f"ERROR: game retry refused: {exc}"
    result = resume_game(requested)
    if result.startswith("ERROR:"):
        try:
            current = store.read_agent_state(pid)
            current_attempt = (
                current.get("attempt") if isinstance(current, dict) else None
            )
            current_manual = (
                current_attempt.get("manualRetry")
                if isinstance(current_attempt, dict)
                else None
            )
            if (
                isinstance(current_manual, dict)
                and current_manual.get("epoch") == epoch
                and current_manual.get("status") == "AUTHORIZED"
            ):
                updated = copy.deepcopy(current)
                updated_attempt = copy.deepcopy(current_attempt)
                updated_manual = copy.deepcopy(current_manual)
                updated_manual["status"] = "CONSUMED"
                updated_attempt["manualRetry"] = updated_manual
                updated["attempt"] = updated_attempt
                store.write_agent_state(pid, updated)
            store.update_manifest(
                status="paused_api_error",
                error=(
                    str(manifest.get("pause_issue", {}).get("title"))
                    if isinstance(manifest.get("pause_issue"), dict)
                    else "API 请求失败"
                ),
                pause_turn_id=turn_id,
                pause_pid=pid,
                pause_issue=manifest.get("pause_issue"),
                manual_retry_status="resume_failed",
            )
        except Exception:
            pass
        return result
    return (
        f"Game {requested} retry authorized for {turn_id}; "
        "the browser will resume from the confirmed snapshot: "
        f"{_session_url()}"
    )


async def _blocking_bg_tool_call(args: dict) -> str:
    """Keep the leader's BgPlay call pending until the session is closed."""
    if _GAME_SESSION_PREPARED_EVENT.is_set():
        _GAME_SESSION_PREPARED_EVENT.clear()
    else:
        _GAME_SESSION_DONE_EVENT.clear()
    if _GAME_SESSION_DONE_EVENT.is_set():
        return ToolCallResult(
            "Game start cancelled before server startup. Code Agent mode restored.",
            metadata={"end_submit": True, "game_session_closed": True},
        )
    started = await asyncio.to_thread(_bg_tool_call, args)
    if started.startswith("Game start cancelled"):
        return ToolCallResult(
            started, metadata={"end_submit": True, "game_session_closed": True},
        )
    if started.startswith("ERROR:"):
        return started
    await asyncio.to_thread(_GAME_SESSION_DONE_EVENT.wait)
    if _SERVER_ERROR:
        return ToolCallResult(
            f"ERROR: game server stopped: {_SERVER_ERROR}",
            is_error=True, metadata={"end_submit": True},
        )
    return ToolCallResult(
        started + "\nGame stopped. Code Agent mode restored.",
        metadata={"end_submit": True, "game_session_closed": True},
    )


BG_PLAY_TOOL = Tool(
    name="BgPlay",
    description="按已注册游戏 manifest 启动可恢复的网页桌游会话。",
    prompt=(
        "当用户想玩已注册网页桌游时调用；使用 manifest 的稳定 engine id。"
        "浏览器中的游戏 Adapter 是权威游戏引擎。\n"
        "开局前确认游戏、模式和人数；信息不足时用 AskUserQuestion 只询问缺失项。"
        "选择列表以本工具的已注册游戏目录为准；别名是同一款游戏，不要分成多个选项。"
        "用户给出具体游戏名和开局参数后，即使游戏名可能不存在，也调用 BgPlay 让它验证；"
        "Do not use Bash or Glob to search for a game. Return a concise ERROR from BgPlay.\n"
        "语义规则：完整请求为‘测试璀璨宝石/测试花砖物语/测试姬路城/测试白城堡’时使用"
        "mode=manual-test；player_count 依游戏支持的人数设置，默认 2；该模式由当前浏览器控制全部人类席位，"
        "不创建 teammate、不创建 AI、不调用 provider/API。要求测试函数、游戏代码或明确不要开游戏时，不调用 BgPlay。"
        "AI打AI默认使用 mode=ai_vs_ai、player_count=2；"
        "明确说‘我要玩某游戏N人局’"
        "则 mode=human_vs_ai, player_count=N（一名人类加N-1名AI）。"
        "游戏名、中文名和别名由 manifest 解析；人数由对应 manifest 校验。"
        "不要猜测未提供的游戏，也不要将本工具的接口默认值当作用户选择。\n"
        "参数:\n"
        "- engine: manifest 中的稳定游戏 id（当前默认 'splendor'）\n"
        "- mode: 'ai_vs_ai'、'human_vs_ai'、'manual-test' 或只读历史回放 'replay'\n"
        "- player_count: 总玩家数\n\n"
        "- game_id: mode=replay 时可指定已完成对局；省略则打开本地历史列表\n\n"
        "启动后 Code Agent 阻塞；使用 /bg stop 关闭，/bg resume 恢复未完成对局。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "engine": {"type": "string", "description": "游戏引擎", "default": "splendor"},
            "mode": {"type": "string", "enum": ["ai_vs_ai", "human_vs_ai", "manual-test", "replay"], "default": "ai_vs_ai"},
            "player_count": {"type": "integer", "minimum": 1, "maximum": 12, "description": "总玩家数；实际范围由游戏 manifest 决定", "default": 2},
            "seed": {"type": "integer", "minimum": 0, "maximum": 4294967295, "description": "可选的可复现对局种子；省略时自动生成"},
            "game_id": {"type": "string", "description": "mode=replay 时的已完成历史对局 ID；省略时打开历史列表"},
        },
        "required": [],
    },
    call=_blocking_bg_tool_call,
    is_read_only=False,
    auto_allow=True,
    plan_allowed=False,
    always_load=True,
)


def build_bg_play_tool() -> Tool:
    """Ground launch clarifications in the installed manifests, including aliases."""
    from dataclasses import replace
    from bglab.games.registry import discover_games, GameRegistryError

    try:
        games = discover_games().values()
        catalog = "\n".join(
            f"- {game.id}: {game.title}；别名 {', '.join(game.aliases)}；"
            f"{game.min_players}–{game.max_players} 人"
            for game in games
        ) or "当前没有已注册游戏。"
    except (OSError, GameRegistryError):
        catalog = "游戏目录暂时不可读；不要编造游戏选项。"
    return replace(BG_PLAY_TOOL, prompt=BG_PLAY_TOOL.prompt + "\n已注册游戏目录：\n" + catalog)
