/* Local human play: no host connection, provider or authority service. */
(function () {
  'use strict';
  const settings = window.BOARD_GAME_LOCAL_CONFIG;
  if (!settings) throw new Error('缺少本地游戏配置');
  const storageKey = `board-game-local/v1/${settings.gameId}`;
  let config = null;
  let active = false;
  let started = false;
  let status;
  let playerCount;
  const frontend = () => window.BGLabFrontend;
  const adapter = () => window.BGLabGameAdapter;
  const say = (text, error = false) => {
    if (!status) return;
    status.textContent = text;
    status.dataset.error = String(error);
  };
  function localConfig(count, seed) {
    if (!Number.isInteger(count) || count < 2 || count > 4) throw new Error('人数必须为 2 至 4');
    if (seed != null && !Number.isInteger(seed)) throw new Error('存档种子格式无效');
    return {playerCount:count, playerTypes:Array(count).fill('human'),
      names:Array.from({length:count}, (_, i) => `玩家 ${i + 1}`), manualTest:true,
      seed:seed ?? crypto.getRandomValues(new Uint32Array(1))[0]};
  }
  function envelope() {
    if (!active) throw new Error('请先开始或继续一局游戏');
    return {format:'board-game-local/v1', gameId:settings.gameId,
      savedAt:new Date().toISOString(), config, snapshot:frontend().snapshot()};
  }
  function persist() {
    if (!active) return false;
    try {
      localStorage.setItem(storageKey, JSON.stringify(envelope()));
      say('已保存本次提交；刷新可继续');
      return true;
    } catch (error) {
      say(`自动保存失败，请导出存档：${error.message}`, true);
      return false;
    }
  }
  function revealGame() {
    const app = document.getElementById('app');
    app.inert = false;
    app.style.removeProperty('display');
  }
  function restore(value) {
    if (!value || value.format !== 'board-game-local/v1' || value.gameId !== settings.gameId
      || !value.snapshot || typeof value.snapshot !== 'object') throw new Error('存档格式或游戏不匹配');
    // Validate in a separate game instance before changing either the UI or saved game.
    const candidate = new (adapter().constructor)();
    candidate.restore(value.snapshot);
    const snapshot = candidate.snapshot();
    const count = snapshot.wrapper?.playerCount ?? snapshot.game?.playerCount;
    const nextConfig = localConfig(count, value.config?.seed);
    revealGame();
    frontend().restore(snapshot, nextConfig);
    config = nextConfig;
    active = true;
    playerCount.value = String(count);
    persist();
  }
  function resumeSaved() {
    const text = localStorage.getItem(storageKey);
    if (!text) throw new Error('尚无本地存档');
    restore(JSON.parse(text));
  }
  function startNew() {
    if (active && !window.confirm('开始新局会替换本地自动存档。需要保留当前局时，请先取消并导出存档。')) return;
    const nextConfig = localConfig(Number(playerCount.value), settings.seed);
    revealGame();
    frontend().start(nextConfig);
    config = nextConfig;
    active = true;
    persist();
  }
  function exportSave() {
    const blob = new Blob([JSON.stringify(envelope(), null, 2)], {type:'application/json'});
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `${settings.gameId}-存档.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
  function guarded(action) {
    return async () => {
      try { await action(); } catch (error) { say(error.message || String(error), true); }
    };
  }
  function boot() {
    if (started) return;
    started = true;
    const bar = document.createElement('section');
    bar.id = 'local-game-controls';
    bar.setAttribute('aria-label', '本地游戏与存档');
    bar.innerHTML = `<strong>本地多人</strong><label>人数 <select aria-label="本地游戏人数"><option>2</option><option>3</option><option>4</option></select></label>
      <button type="button" data-local-action="new">开始新局</button><button type="button" data-local-action="resume">继续存档</button>
      <button type="button" data-local-action="export">导出存档</button><button type="button" data-local-action="import">导入存档</button>
      <input type="file" accept=".json,application/json" hidden><span role="status" aria-live="polite">轮流操作当前玩家，未确认的选择不保存</span>`;
    const style = document.createElement('style');
    style.textContent = `#local-game-controls{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 16px;background:#17222c;color:#f5f3e9;font:14px system-ui;position:relative;z-index:10000}#local-game-controls button,#local-game-controls select{padding:6px 10px;font:inherit}#local-game-controls [role=status]{font-size:12px}#local-game-controls [data-error=true]{color:#ffd2c8}`;
    document.head.append(style);
    document.body.prepend(bar);
    // Some existing UIs render a constructor-created default game. It must
    // stay unavailable until this local session owns its configuration/save.
    const app = document.getElementById('app');
    app.inert = true;
    app.style.display = 'none';
    playerCount = bar.querySelector('select');
    playerCount.value = String(settings.playerCount || 2);
    status = bar.querySelector('[role=status]');
    const fileInput = bar.querySelector('input');
    bar.querySelector('[data-local-action=new]').addEventListener('click', guarded(startNew));
    bar.querySelector('[data-local-action=resume]').addEventListener('click', guarded(resumeSaved));
    bar.querySelector('[data-local-action=export]').addEventListener('click', guarded(exportSave));
    bar.querySelector('[data-local-action=import]').addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', guarded(async () => {
      const file = fileInput.files[0];
      if (!file) return;
      const value = JSON.parse(await file.text());
      restore(value);
      fileInput.value = '';
    }));
    // The existing terminal "new game" control must retain the same local configuration.
    document.addEventListener('click', event => {
      if (event.target.closest('[data-action="new-game"]')) {
        event.preventDefault(); event.stopImmediatePropagation(); guarded(startNew)();
      }
    }, true);
    const loading = document.getElementById('init-loading');
    if (loading) loading.style.display = 'none';
    try {
      if (localStorage.getItem(storageKey)) resumeSaved();
    } catch (error) { say(`存档未载入：${error.message}。原存档已保留。`, true); }
    window.Bridge._ready = true;
  }
  window.Bridge = {
    mode:'local-human', _ready:false,
    init() { if (document.readyState !== 'loading') boot(); return true; },
    persist, validationInFlight:() => false, trace:() => {},
    requestAITurn:async () => { throw new Error('本地多人模式仅支持人类玩家'); },
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once:true});
  else boot();
})();
