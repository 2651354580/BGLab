"""Read-only local HTTP surface for verified historical game replays."""

from __future__ import annotations

import html
import json
import re
import secrets
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from bglab.games.replay import (
    ReplayAuditError,
    load_replay_frame,
    load_replay_manifest,
    load_replay_turn,
    materialize_replay,
    verify_replay_turn,
)
from bglab.games.registry import get_game
from bglab.games.frontend_assets import (
    resolve_shared_ui_asset,
    shared_ui_content_type,
)


def replay_controller_script(game_id: str, manifest: dict, capability: str) -> str:
    """Generate one game-agnostic controller for an original package frontend."""
    config = json.dumps(
        {
            "gameId": game_id,
            "turnCount": int(manifest["turnCount"]),
            "frameCount": int(manifest["frameCount"]),
            "players": list(manifest.get("players") or []),
            "finalResult": manifest.get("finalResult"),
            "capability": capability,
        },
        ensure_ascii=False,
    ).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return r"""
(function(){
  'use strict';
  const cfg=__CONFIG__;
  let current=0, failed=false, busy=false, currentTurn=null;
  const api='/api/replay/'+encodeURIComponent(cfg.gameId);
  const auth='capability='+encodeURIComponent(cfg.capability);
  function node(tag,text){const value=document.createElement(tag);if(text!==undefined)value.textContent=String(text);return value}
  async function getJson(url){const response=await fetch(url+(url.includes('?')?'&':'?')+auth,{cache:'no-store'});const value=await response.json();if(!response.ok){const error=new Error(value.message||'Replay request failed');error.code=value.code||'REPLAY_STORE_CORRUPT';error.details=value.details||{};throw error}return value}
  function playerName(seat){return cfg.players[seat]||('P'+seat)}
  function createUi(){
    const style=node('style');
    style.textContent='body.bglab-replay-active *{pointer-events:none!important}#bglab-replay-toolbar,#bglab-replay-toolbar *,#bglab-score-modal,#bglab-score-modal *{pointer-events:auto!important}#bglab-replay-toolbar{position:fixed;right:16px;top:16px;z-index:2147483600;width:300px;padding:12px;border:1px solid #6d7d9d;border-radius:12px;background:rgba(12,18,31,.94);color:#f5f7ff;font:14px/1.4 system-ui;box-shadow:0 8px 28px #0009}#bglab-replay-toolbar button{margin:7px 4px 0 0;padding:6px 9px}#bglab-replay-error{margin-top:8px;color:#ff9b9b;white-space:pre-wrap}#bglab-score-modal{position:fixed;inset:0;z-index:2147483601;display:flex;align-items:center;justify-content:center;background:#000a}#bglab-score-card{max-height:82vh;overflow:auto;min-width:420px;max-width:760px;padding:22px;border-radius:14px;background:#151d30;color:#f6f8ff;box-shadow:0 18px 55px #000}#bglab-score-card table{width:100%;border-collapse:collapse;margin:12px 0}#bglab-score-card td,#bglab-score-card th{padding:6px;border-bottom:1px solid #39445d;text-align:left}';
    document.head.appendChild(style);
    document.body.classList.add('bglab-replay-active');
    const box=node('section');box.id='bglab-replay-toolbar';box.setAttribute('aria-label','历史对局回放');
    const title=node('strong','历史回放 · '+cfg.gameId);
    const progress=node('div');progress.id='bglab-replay-progress';
    const actor=node('div');actor.id='bglab-replay-actor';
    const controls=node('div');
    const start=node('button','回到开始');start.id='bglab-replay-start';start.onclick=()=>show(0,false);
    const previous=node('button','上一步');previous.id='bglab-replay-previous';previous.onclick=()=>show(Math.max(0,current-1),false);
    const next=node('button','下一回合');next.id='bglab-replay-next';next.onclick=advance;
    const error=node('div');error.id='bglab-replay-error';error.setAttribute('role','alert');
    controls.append(start,previous,next);box.append(title,progress,actor,controls,error);document.body.appendChild(box);
  }
  function setBusy(value){busy=value;for(const id of ['bglab-replay-start','bglab-replay-previous','bglab-replay-next'])document.getElementById(id).disabled=value}
  function lockGameUi(){
    for(const control of document.querySelectorAll('button,input,select,textarea,[role="button"],[tabindex]')){
      if(control.closest('#bglab-replay-toolbar,#bglab-score-modal'))continue;
      control.setAttribute('aria-disabled','true');
      control.setAttribute('tabindex','-1');
      if('disabled' in control)control.disabled=true;
    }
  }
  async function metadata(){
    currentTurn=current>0?await getJson(api+'/turn/'+(current-1)+'?metadata=1'):null;
    document.getElementById('bglab-replay-progress').textContent='第 '+current+' / '+cfg.turnCount+' 回合';
    document.getElementById('bglab-replay-actor').textContent=currentTurn
      ?('已播放：'+playerName(currentTurn.seat)+' · '+currentTurn.turnId)
      :(cfg.turnCount?'尚未播放第一回合':'该对局没有 AI 回合');
    document.getElementById('bglab-replay-start').disabled=busy||current===0;
    document.getElementById('bglab-replay-previous').disabled=busy||current===0;
    document.getElementById('bglab-replay-next').disabled=busy||failed||current>=cfg.turnCount;
  }
  async function restoreFrame(index){
    const frame=await getJson(api+'/frame/'+index);
    const playerTypes=Array(cfg.players.length).fill('replay');
    window.BGLabFrontend.restore(frame.state,{gameId:cfg.gameId,names:cfg.players,playerTypes,viewerSeat:null,aiDelay:0});
    lockGameUi();
  }
  async function show(index,verifyForward){
    if(busy||index<0||index>=cfg.frameCount)return;
    setBusy(true);
    try{
      if(verifyForward&&index===current+1)await getJson(api+'/turn/'+current);
      await restoreFrame(index);
      current=index;
      if(current<cfg.turnCount)document.getElementById('bglab-score-modal')?.remove();
      await metadata();
      if(current===cfg.turnCount)showFinal();
    }catch(error){fail(error)}
    finally{busy=false;await metadata().catch(()=>{});}
  }
  async function advance(){
    if(busy||failed||current>=cfg.turnCount)return;
    await show(current+1,true);
  }
  function fail(error){
    failed=true;
    const details=error.details||{};
    document.getElementById('bglab-replay-error').textContent=(error.code||'REPLAY_STORE_CORRUPT')+' · '+error.message+(details.expected!==undefined?'\nexpected='+details.expected:'')+(details.actual!==undefined?'\nactual='+details.actual:'');
  }
  function showFinal(){
    if(document.getElementById('bglab-score-modal')||!cfg.finalResult)return;
    const modal=node('div');modal.id='bglab-score-modal';modal.setAttribute('role','dialog');modal.setAttribute('aria-modal','true');
    const card=node('section');card.id='bglab-score-card';card.appendChild(node('h2','最终计分'));
    const winners=cfg.finalResult.winners.map(playerName).join('、');
    card.appendChild(node('p','胜者：'+winners));
    for(const player of cfg.finalResult.players){
      card.appendChild(node('h3',playerName(player.seat)+' · '+player.total+' 分'));
      const table=node('table');
      for(const component of player.components){
        const row=node('tr'),label=node('td',component.label),value=node('td',component.value),formula=node('td',component.formula);
        row.append(label,value,formula);table.appendChild(row);
      }
      const total=node('tr');total.append(node('th','总分'),node('th',player.total),node('th','各项之和'));table.appendChild(total);card.appendChild(table);
    }
    if(cfg.finalResult.tieBreakers.length){
      card.appendChild(node('h3','平局裁定'));
      for(const item of cfg.finalResult.tieBreakers)card.appendChild(node('p',item.label+'：'+item.values.join(' / ')+(item.winner===null?'':' → '+playerName(item.winner))));
    }
    const close=node('button','关闭计分');close.onclick=()=>modal.remove();card.appendChild(close);modal.appendChild(card);document.body.appendChild(modal);
  }
  async function boot(){
    if(!window.BGLabFrontend||typeof window.BGLabFrontend.restore!=='function'){setTimeout(boot,50);return}
    createUi();
    try{await restoreFrame(0);await metadata()}catch(error){fail(error)}
  }
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();
})();
""".replace("__CONFIG__", config)


def selector_html(capability: str) -> str:
    from bglab.games.persistence import store as store_module

    options: list[tuple[str, str]] = []
    if store_module.GAMES_DIR.is_dir():
        for path in store_module.GAMES_DIR.glob("*/manifest.json"):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if manifest.get("status") == "finished" and manifest.get("finished_at"):
                options.append((
                    path.parent.name,
                    str(manifest.get("game_title") or manifest.get("engine") or path.parent.name),
                ))
    escaped_capability = html.escape(capability, quote=True)
    links = "".join(
        f'<li><a href="/replay?gameId={html.escape(game_id, quote=True)}&amp;capability={escaped_capability}">'
        f'{html.escape(title)} · {html.escape(game_id)}</a></li>'
        for game_id, title in sorted(options, reverse=True)
    )
    return (
        "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
        "<title>BGLab 历史回放</title><body><main><h1>历史对局回放</h1>"
        "<form action='/replay' method='get'><label>gameId "
        "<input name='gameId' required pattern='[A-Za-z0-9_-]{1,128}'></label>"
        f"<input type='hidden' name='capability' value='{escaped_capability}'>"
        "<button>打开回放</button></form><ul>"
        + links
        + "</ul></main></body></html>"
    )


def _version_frontend_assets(content: str, game_id: str) -> str:
    """Prevent one localhost game from reusing another game's same-path asset."""
    token = quote(game_id, safe="")

    def replace(match: re.Match[str]) -> str:
        url = match.group("url")
        if url.startswith(("data:", "http:", "https:", "#", "javascript:")):
            return match.group(0)
        separator = "&" if "?" in url else "?"
        return (
            match.group("prefix")
            + url
            + separator
            + "bglabReplay="
            + token
            + match.group("suffix")
        )

    return re.sub(
        r"(?P<prefix>\b(?:src|href)=[\"'])(?P<url>[^\"']+)(?P<suffix>[\"'])",
        replace,
        content,
        flags=re.IGNORECASE,
    )


def make_replay_handler(initial_game_id: str | None, capability: str):
    """Create one local-only handler with no model or live-runtime dependency."""
    if not isinstance(capability, str) or not capability:
        raise ValueError("replay capability is required")
    state: dict[str, object] = {
        "gameId": None,
        "manifest": None,
        "definition": None,
    }

    def select(game_id: str) -> None:
        manifest = materialize_replay(game_id)
        state.update(
            gameId=game_id,
            manifest=manifest,
            definition=get_game(str(manifest["engine"])),
        )

    if initial_game_id is not None:
        select(initial_game_id)

    class ReplayHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(Path.cwd()), **kwargs)

        def log_message(self, *_args):
            return

        def end_headers(self) -> None:
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            super().end_headers()

        def _send_bytes(
            self,
            status: int,
            body: bytes,
            content_type: str,
            *,
            no_store: bool = False,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            if no_store:
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: dict) -> None:
            self._send_bytes(
                status,
                json.dumps(value, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
                no_store=True,
            )

        def _error(self, error: ReplayAuditError) -> None:
            self._json(409, {
                "code": error.code,
                "message": str(error),
                "details": error.details,
            })

        def _authenticate(self, parsed) -> bool:
            protected = parsed.path in {"/", "/replay"} or parsed.path.startswith(
                "/api/replay",
            )
            if not protected:
                return True
            supplied = parse_qs(
                parsed.query, keep_blank_values=True,
            ).get("capability", [None])[0]
            try:
                valid = isinstance(supplied, str) and secrets.compare_digest(
                    supplied, capability,
                )
            except (TypeError, UnicodeEncodeError):
                valid = False
            if valid:
                return True
            if parsed.path.startswith("/api/replay"):
                self._json(403, {
                    "code": "FORBIDDEN",
                    "message": "invalid replay capability",
                })
            else:
                self.send_error(403, "invalid replay capability")
            return False

        def do_POST(self):
            if not self._authenticate(urlparse(self.path)):
                return
            self._json(405, {"code": "METHOD_NOT_ALLOWED", "message": "replay is read-only"})

        do_PUT = do_POST
        do_DELETE = do_POST
        do_PATCH = do_POST

        def do_GET(self):
            parsed = urlparse(self.path)
            if not self._authenticate(parsed):
                return
            parts = [part for part in parsed.path.split("/") if part]
            try:
                if len(parts) >= 4 and parts[:2] == ["api", "replay"]:
                    game_id = parts[2]
                    if game_id != state["gameId"]:
                        raise ReplayAuditError(
                            "REPLAY_NOT_FOUND",
                            "requested replay is not the active historical game",
                        )
                    kind = parts[3]
                    if kind == "manifest" and len(parts) == 4:
                        self._json(200, load_replay_manifest(game_id))
                        return
                    if len(parts) != 5:
                        raise ReplayAuditError("REPLAY_STORE_CORRUPT", "invalid replay endpoint")
                    try:
                        index = int(parts[4])
                    except ValueError as exc:
                        raise ReplayAuditError("REPLAY_STORE_CORRUPT", "invalid replay index") from exc
                    if kind == "frame":
                        self._json(200, load_replay_frame(game_id, index))
                        return
                    if kind == "turn":
                        metadata_only = parse_qs(parsed.query).get("metadata") == ["1"]
                        self._json(
                            200,
                            load_replay_turn(game_id, index)
                            if metadata_only else verify_replay_turn(game_id, index),
                        )
                        return
                    raise ReplayAuditError("REPLAY_STORE_CORRUPT", "invalid replay endpoint")
                if parsed.path in {"/", "/replay"}:
                    requested = parse_qs(parsed.query).get("gameId", [None])[0]
                    if requested:
                        select(requested)
                    if state["gameId"] is None:
                        self._send_bytes(
                            200,
                            selector_html(capability).encode("utf-8"),
                            "text/html; charset=utf-8",
                            no_store=True,
                        )
                        return
                    definition = state["definition"]
                    manifest = state["manifest"]
                    assert definition is not None and isinstance(manifest, dict)
                    content = definition.frontend_entry.read_text(encoding="utf-8")
                    bootstrap = (
                        "<script>window.BG_REPLAY_MODE=true;"
                        f"window.BG_REPLAY_GAME_ID={json.dumps(state['gameId'])};"
                        "window.BG_GAME_ID=null;</script>"
                    )
                    content = content.replace("<head>", "<head>" + bootstrap, 1)
                    content = _version_frontend_assets(
                        content,
                        str(state["gameId"]),
                    )
                    controller = replay_controller_script(
                        str(state["gameId"]), manifest, capability,
                    )
                    content = content.replace(
                        "</body>",
                        "<script>" + controller + "</script></body>",
                        1,
                    )
                    self._send_bytes(
                        200,
                        content.encode("utf-8"),
                        "text/html; charset=utf-8",
                        no_store=True,
                    )
                    return
                definition = state["definition"]
                if definition is None:
                    self.send_error(404)
                    return
                if parsed.path.startswith("/__bglab_shared/"):
                    shared_name = parsed.path[len("/__bglab_shared/"):]
                    shared_path = resolve_shared_ui_asset(
                        definition.root, shared_name,
                    )
                    if shared_path is None:
                        self.send_error(404, "shared UI asset not found")
                        return
                    self._send_bytes(
                        200,
                        shared_path.read_bytes(),
                        shared_ui_content_type(shared_name),
                    )
                    return
                self.directory = str(definition.frontend_entry.parent)
                super().do_GET()
            except ReplayAuditError as error:
                self._error(error)

    return ReplayHandler
