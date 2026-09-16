(function () {
  'use strict';

  const app = document.getElementById('app');
  const adapter = window.BGLabGameAdapter;
  const core = window.BGLabAzulCore;
  const viewModel = window.BGLabAzulViewModel;
  const geometry = window.BGLabAzulLayoutGeometry;
  const tileClass = {
    'first-player':'tile-c0', black:'tile-c1', cyan:'tile-c2',
    blue:'tile-c3', yellow:'tile-c4', red:'tile-c5',
  };
  const colorName = {black:'黑色', cyan:'青色', blue:'蓝色', yellow:'黄色', red:'红色'};
  const wallPattern = [
    ['blue', 'yellow', 'red', 'black', 'cyan'],
    ['cyan', 'blue', 'yellow', 'red', 'black'],
    ['black', 'cyan', 'blue', 'yellow', 'red'],
    ['red', 'black', 'cyan', 'blue', 'yellow'],
    ['yellow', 'red', 'black', 'cyan', 'blue'],
  ];
  const wallTint = {black:'#231f20', cyan:'#1dcad3', blue:'#0083ad', yellow:'#ffbf3c', red:'#f5333f'};
  const playerColors = ['#1976d2', '#d32f2f', '#388e3c', '#f57c00'];
  const CENTER_ITEM_LAYOUTS = Object.freeze({
    1:Object.freeze([Object.freeze({x:40,y:20})]),
    2:Object.freeze([Object.freeze({x:20,y:20}), Object.freeze({x:59,y:20})]),
    3:Object.freeze([Object.freeze({x:1,y:20}), Object.freeze({x:40,y:20}), Object.freeze({x:79,y:20})]),
    4:Object.freeze([
      Object.freeze({x:20,y:1}), Object.freeze({x:59,y:1}),
      Object.freeze({x:20,y:40}), Object.freeze({x:59,y:40}),
    ]),
    5:Object.freeze([
      Object.freeze({x:1,y:1}), Object.freeze({x:40,y:1}), Object.freeze({x:79,y:1}),
      Object.freeze({x:20,y:40}), Object.freeze({x:59,y:40}),
    ]),
    6:Object.freeze([
      Object.freeze({x:1,y:1}), Object.freeze({x:40,y:1}), Object.freeze({x:79,y:1}),
      Object.freeze({x:1,y:40}), Object.freeze({x:40,y:40}), Object.freeze({x:79,y:40}),
    ]),
  });

  let playerTypes = ['human', 'human'];
  let playerNames = [];
  let manualTest = false;
  let viewerSeat = 0;
  let draft = [];
  let draftStack = [];
  let pendingSteps = null;
  let turnCheckpoint = null;
  let recentActions = [];
  let actionNotice = '';
  let actionNoticeTimer = null;
  let busy = false;
  let paused = null;
  let aiRequestId = null;
  let lastError = '';
  let resizeObserver = null;

  function state() { return adapter.snapshot().game; }
  function escape(value) {
    return String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[character]));
  }
  function currentModel() {
    if (manualTest) viewerSeat = state().currentPlayer;
    const snapshot = adapter.snapshot();
    return viewModel.build(snapshot, snapshot.adapterView, {viewerSeat, names:playerNames, playerTypes}, draft);
  }
  function tile(color, className = 'bg-tile', attributes = '') {
    return `<div class="${className} ${tileClass[color] || ''}" ${attributes}></div>`;
  }
  function isHumanTurn(game) {
    return !window.BG_REPLAY_MODE && hasHumanControls() && game.phase === 'draft' && playerTypes[game.currentPlayer] === 'human' && !busy && !paused;
  }
  function hasHumanControls() {
    return !window.BG_REPLAY_MODE && viewerSeat !== null;
  }
  function sourceStep() { return draft.find(step => step.op === 'select_source'); }
  function colorStep() { return draft.find(step => step.op === 'select_color'); }
  function destinationStep(steps = pendingSteps) {
    return (steps || []).find(step => step.op === 'place_tiles') || null;
  }
  function pendingDestinationMatches(destination, row) {
    const pending = destinationStep();
    if (!pending || pending.destination !== destination) return false;
    return destination !== 'pattern' || Number(pending.row) === Number(row);
  }

  function renderFactories(model) {
    const center = geometry.FACTORY_STAGE.width / 2;
    const factoryPositions = geometry.factoryPositions(model.factories.length);
    let html = '';
    model.factories.forEach((factory, factoryIndex) => {
      const {x, y, scale} = factoryPositions[factoryIndex];
      const selected = sourceStep()?.source === 'factory' && sourceStep()?.factoryIndex === factoryIndex;
      const positions = [
        {x:5, y:5, rotate:-8}, {x:75, y:5, rotate:5},
        {x:5, y:75, rotate:12}, {x:75, y:75, rotate:-5},
      ];
      const tiles = factory.tiles.map((entry, index) => {
        const position = positions[index];
        const selectable = isHumanTurn(state());
        return `<button type="button" class="factory-tile ${tileClass[entry.color] || ''}${selectable ? ' is-selectable' : ''}" `
          + `style="left:${position.x}px;top:${position.y}px;transform:rotate(${position.rotate}deg)" `
          + `data-action="choose-source" data-source="factory" data-factory="${factoryIndex}" data-color="${entry.color}" `
          + `aria-label="工坊 ${factoryIndex + 1} 的${colorName[entry.color]}花砖"></button>`;
      }).join('');
      html += `<div class="factory-wrap" aria-label="工坊 ${factoryIndex + 1}" style="left:${x}px;top:${y}px;transform:scale(${scale});transform-origin:top left"><div class="factory-disc${selected ? ' is-selected' : ''}">${tiles}</div></div>`;
    });

    const groups = model.center.reduce((result, entry) => {
      result[entry.color] = (result[entry.color] || 0) + 1;
      return result;
    }, {});
    const centerItems = [];
    if (model.firstPlayerTokenAvailable) {
      const markerClass = 'center-first-player-token';
      centerItems.push({kind:'marker', className:markerClass});
    }
    Object.entries(groups).forEach(([color, count]) => {
      centerItems.push({kind:'tiles', color, count});
    });
    const layout = CENTER_ITEM_LAYOUTS[centerItems.length] || [];
    const centerTiles = centerItems.map((item, index) => {
      const position = layout[index];
      if (!position) return '';
      const style = `left:${position.x}px;top:${position.y}px`;
      if (item.kind === 'marker') {
        return `<span class="center-layout-item ${item.className}" style="${style}" title="先手标记会进入地板线" aria-label="先手标记"></span>`;
      }
      return `<button class="center-layout-item center-tile-group" type="button" style="${style}" data-action="choose-source" data-source="center" data-color="${item.color}" data-count="${item.count}" aria-label="中央的${colorName[item.color]}花砖">${tile(item.color, 'center-tile')}${item.count > 1 ? `<span>${item.count}</span>` : ''}</button>`;
    }).join('');
    html += `<div class="center-pool" style="left:${center - 65}px;top:${center - 55}px"><strong>中央区域</strong><div class="center-tiles">${centerItems.length ? centerTiles : '<small>空</small>'}</div></div>`;
    return html;
  }

  function renderPatternLines(model, player) {
    const config = model.calibration.pattern;
    const pitchX = config.tileSize + config.gapX;
    const pitchY = config.tileSize + config.gapY;
    const currentGame = state();
    const selectedColor = colorStep()?.color;
    return `<div class="bg-pattern-lines-area">${player.patternLines.map((line, row) => {
      const capacity = row + 1;
      const rightCenter = geometry.BOARD.patternRightCenters[row] + config.x;
      const left = rightCenter - (capacity - 1) * pitchX - config.tileSize / 2;
      const top = geometry.BOARD.patternRowCenters[0] + config.y + row * pitchY - config.tileSize / 2;
      const canPlace = player.seat === currentGame.currentPlayer && isHumanTurn(currentGame) && selectedColor && core.canPlacePattern(currentGame, player.seat, row, selectedColor);
      const pending = player.seat === currentGame.currentPlayer && pendingDestinationMatches('pattern', row);
      const slots = Array.from({length:capacity}, (_, index) => index < line.length
        ? tile(line[index], 'bg-pattern-tile', `style="width:${config.tileSize}px;height:${config.tileSize}px"`)
        : `<span class="bg-pattern-slot" style="width:${config.tileSize}px;height:${config.tileSize}px"></span>`).join('');
      return `<button type="button" class="bg-pattern-row${canPlace ? ' is-selectable' : ''}${pending ? ' is-pending' : ''}" style="left:${left}px;top:${top}px;gap:${config.gapX}px" ${canPlace ? `data-action="choose-destination" data-destination="pattern" data-row="${row}"` : 'disabled'} aria-label="第 ${row + 1} 条花纹线">${slots}</button>`;
    }).join('')}</div>`;
  }

  function renderWall(model, player) {
    const config = model.calibration.wall;
    const pitchX = config.tileSize + config.gapX;
    const pitchY = config.tileSize + config.gapY;
    return `<div class="bg-wall-area">${player.wall.flatMap((row, rowIndex) => row.map((placed, columnIndex) => {
      const expected = wallPattern[rowIndex][columnIndex];
      const left = geometry.BOARD.wallFirstCenter.x + config.x + columnIndex * pitchX - config.tileSize / 2;
      const top = geometry.BOARD.wallFirstCenter.y + config.y + rowIndex * pitchY - config.tileSize / 2;
      return placed
        ? tile(placed, 'bg-wall-tile', `style="left:${left}px;top:${top}px;width:${config.tileSize}px;height:${config.tileSize}px"`)
        : `<span class="bg-wall-cell" style="left:${left}px;top:${top}px;width:${config.tileSize}px;height:${config.tileSize}px;background:${wallTint[expected]}"></span>`;
    })).join('')}</div>`;
  }

  function renderFloor(model, player) {
    const config = model.calibration.floor;
    const pitchX = config.tileSize + config.gapX;
    const verticalAdjustment = config.gapY - viewModel.CALIBRATION.floor.gapY;
    const currentGame = state();
    const selectable = player.seat === currentGame.currentPlayer && isHumanTurn(currentGame) && Boolean(colorStep());
    const pending = player.seat === currentGame.currentPlayer && pendingDestinationMatches('floor');
    const slots = Array.from({length:7}, (_, index) => {
      const left = geometry.BOARD.floorFirstCenter.x + config.x + index * pitchX - config.tileSize / 2;
      const top = geometry.BOARD.floorFirstCenter.y + config.y + verticalAdjustment - config.tileSize / 2;
      const color = player.floorLine[index];
      return color
        ? `<span class="bg-floor-tile ${tileClass[color] || ''}" style="left:${left}px;top:${top}px;width:${config.tileSize}px;height:${config.tileSize}px"></span>`
        : `<span class="bg-floor-slot" style="left:${left}px;top:${top}px;width:${config.tileSize}px;height:${config.tileSize}px"></span>`;
    }).join('');
    return `<button type="button" class="bg-floor-line${selectable ? ' is-selectable' : ''}${pending ? ' is-pending' : ''}" ${selectable ? 'data-action="choose-destination" data-destination="floor"' : 'disabled'} aria-label="地板线">${slots}</button>`;
  }

  function renderPlayers(model) {
    const visiblePlayers = model.players.some(player => player.seat === model.viewerSeat)
      ? [...model.players].sort((left, right) => (
        Number(right.seat === model.viewerSeat) - Number(left.seat === model.viewerSeat)
      ))
      : model.players;
    return visiblePlayers.map(player => {
      const active = player.seat === model.currentPlayer;
      return `<article class="bg-player-board-wrapper${active ? ' is-current' : ''}" data-seat="${player.seat}" data-viewer-board="${player.seat === model.viewerSeat}" style="--player-color:${playerColors[player.seat]}">
        <div class="bg-board-viewport"><div class="bg-player-table" style="background-image:url('${player.boardAsset}')">${renderPatternLines(model, player)}${renderWall(model, player)}${renderFloor(model, player)}</div></div>
      </article>`;
    }).join('');
  }

  function shellInteraction(model) {
    if (model.phase === 'finished') return {
      state:'finished', instruction:'对局已结束', canCancel:false, canSkip:false, canConfirm:false,
    };
    if (paused) return {
      state:'error', instruction:`对局已暂停：${paused}`,
      error:{code:'paused', message:String(paused)}, canCancel:false, canSkip:false, canConfirm:false,
    };
    if (busy || !isHumanTurn(state())) return {
      state:busy ? 'resolving' : 'waiting_next_decision',
      instruction:busy ? 'AI 正在思考并提交行动…' : `等待 ${model.players[model.currentPlayer]?.name || '当前玩家'} 行动`,
      canCancel:false, canSkip:false, canConfirm:false,
    };
    if (pendingSteps) return {
      state:'confirmation_pending', instruction:'落点已预览，请确认本回合',
      sourceLabel:'来源与颜色已验证', targetLabel:'落点已验证',
      validatedSteps:['来源已验证', '颜色已验证', '落点已验证'],
      canCancel:false, canSkip:false, canConfirm:true, confirmLabel:'确认放置',
    };
    if (colorStep()) return {
      state:'target_selectable', instruction:`已选择${colorName[colorStep().color]}花砖，请选择花纹线或地板线`,
      sourceLabel:'来源与颜色已验证', targetLabel:'选择放置位置',
      validatedSteps:['来源已验证', '颜色已验证'], canCancel:false, canSkip:false, canConfirm:false,
    };
    if (sourceStep()) return {
      state:'source_selected', instruction:'请选择花砖颜色',
      sourceLabel:'已选择工坊或中央区域', validatedSteps:['来源已验证'],
      canCancel:false, canSkip:false, canConfirm:false,
    };
    return {
      state:'source_selectable', instruction:'请选择一个工坊或中央区域中的花砖',
      canCancel:false, canSkip:false, canConfirm:false,
    };
  }

  function renderBoard(model) {
    const legacyRoundLabel = `花砖物语 · 第 ${model.round} 轮 · 行动 ${model.turn}`;
    return `<section id="game-main" data-round-label="${escape(legacyRoundLabel)}">
      <div id="factories-wrap"><div id="factories-area">${renderFactories(model)}</div></div>
      <div id="all-players">${renderPlayers(model)}</div>
    </section>`;
  }

  function renderRollbackActions() {
    if (!hasHumanControls()) return '';
    const disabled = (draft.length || pendingSteps) && isHumanTurn(state()) ? '' : ' disabled';
    return `<div class="azul-rollback-actions"><button class="bglab-action-button" type="button" data-action="undo"${disabled}>撤回一步</button><button class="bglab-action-button" type="button" data-action="undo" data-restart-turn="true"${disabled}>重新选择</button></div>`;
  }

  function renderSidebar(model) {
    return `<aside class="bg-player-info-panel bglab-player-sidebar" aria-label="玩家得分与资源">${model.players.map(player => {
      const wallCount = player.wall.flat().filter(Boolean).length;
      const completeLines = player.patternLines.filter((line, row) => line.length === row + 1).length;
      return `<section class="bg-player-info-item bglab-player-card${player.seat === model.currentPlayer ? ' is-current' : ''}" style="--player-color:${playerColors[player.seat]}">
        <header class="bg-player-info-header bglab-player-card-header"><span class="bg-player-avatar">${manualTest ? '测' : (player.kind === 'AI' ? 'AI' : '人')}</span><strong>${escape(player.name)}</strong><small>${manualTest ? '手动测试' : player.kind}</small><b class="bg-player-info-score bglab-player-score">${player.score} 分</b></header>
        <div class="bg-player-info-stats bglab-player-resources"><span>墙面 <b>${wallCount}/25</b></span><span>地板 <b>${player.floorLine.length}/7</b></span><span>满行 <b>${completeLines}</b></span></div>
      </section>`;
    }).join('')}<section id="recent-actions" class="bglab-recent-actions" aria-live="polite"><h3>行动记录</h3>${recentActions.length ? recentActions.slice(0, 20).map(record => `<p data-turn-id="${escape(record.turnId || '')}"><b>${record.turn == null ? '' : `第${record.turn}回合 `}</b>${escape(record.text || record)}</p>`).join('') : '<p>等待第一条完整行动</p>'}</section></aside>`;
  }

  function renderFinal(model) {
    const result = adapter.finalResult();
    if (!result) return '';
    const winners = result.winners.map(seat => model.players[seat]?.name || `P${seat + 1}`).join('、');
    return `<div class="gameover-overlay"><section><h2>${escape(winners)} 获胜</h2>${result.players.map(player => {
      const name = model.players[player.seat]?.name || `P${player.seat + 1}`;
      return `<p><strong>${escape(name)}：${player.total} 分</strong><small>${player.components.map(item => `${escape(item.label)} ${item.value}`).join(' · ')}</small></p>`;
    }).join('')}</section></div>`;
  }

  function hint(model) {
    if (busy) return 'AI 正在思考并提交行动…';
    if (lastError) return lastError;
    if (paused) return `对局已暂停：${paused}`;
    if (model.phase === 'finished') return '对局已结束';
    if (!isHumanTurn(state())) return `等待 ${escape(model.players[model.currentPlayer].name)} 行动`;
    if (pendingSteps) return '落点已预览。确认后才会原子提交本回合，撤销不会改变游戏状态。';
    if (colorStep()) return `已选择${colorName[colorStep().color]}花砖，请选择花纹线或地板线。`;
    return '请选择一个工坊或中央区域中的花砖。';
  }

  function render() {
    const model = currentModel();
    const interaction = shellInteraction(model);
    const shellView = window.BGLabAzulPresentation.buildShellView(model, interaction, recentActions);
    app.innerHTML = window.BGLabGameShell.renderShell(shellView, {
      boardHtml:renderBoard(model),
      actionRollbackHtml:renderRollbackActions(),
      overlayHtml:'',
    });
    window.BGLabGameShell.bindActions(app, {
      cancel:restartTurn,
      confirm,
    });
    const root = app.querySelector('.bglab-game-shell');
    if (root) root.dataset.turnId = adapter.snapshot().decisionId;
    observeStages();
    requestAnimationFrame(fitStages);
  }

  function observeStages() {
    if (resizeObserver) resizeObserver.disconnect();
    if (!('ResizeObserver' in window)) return;
    resizeObserver = new ResizeObserver(fitStages);
    const factories = document.getElementById('factories-wrap');
    if (factories) resizeObserver.observe(factories);
    document.querySelectorAll('.bg-board-viewport').forEach(element => resizeObserver.observe(element));
  }

  function fitStages() {
    const factoryViewport = document.getElementById('factories-wrap');
    const factoryStage = document.getElementById('factories-area');
    if (factoryViewport && factoryStage) {
      const fit = geometry.viewportFor(factoryViewport.clientWidth, geometry.FACTORY_STAGE);
      factoryViewport.style.height = `${fit.height}px`;
      factoryStage.style.transform = `scale(${fit.scale})`;
    }
    document.querySelectorAll('.bg-board-viewport').forEach(viewport => {
      const stage = viewport.querySelector('.bg-player-table');
      const fit = geometry.viewportFor(viewport.clientWidth, geometry.BOARD);
      viewport.style.height = `${fit.height}px`;
      stage.style.transform = `scale(${fit.scale})`;
    });
  }

  function preview(steps) {
    const result = adapter.validateTransaction(adapter.decisionId(), {steps});
    if (!result.ok) {
      lastError = `${result.message || '当前选择不可用'}${result.correction ? `；${result.correction}` : ''}`;
      return false;
    }
    lastError = '';
    return true;
  }

  function chooseSource(source, factoryIndex, color) {
    if (!isHumanTurn(state())) return;
    const steps = [
      {op:'begin', action:'turn'},
      {op:'select_source', source, ...(source === 'factory' ? {factoryIndex} : {})},
      {op:'select_color', color},
    ];
    if (!preview(steps)) return render();
    if (!turnCheckpoint) turnCheckpoint = adapter.snapshot();
    draftStack.push({draft:draft.map(step => ({...step})), pendingSteps:pendingSteps ? pendingSteps.map(step => ({...step})) : null});
    draft = steps;
    pendingSteps = null;
    render();
  }

  function chooseDestination(destination, row) {
    if (!isHumanTurn(state()) || !colorStep()) return;
    if (pendingDestinationMatches(destination, row)) return confirm();
    const steps = [...draft, {op:'place_tiles', destination, ...(destination === 'pattern' ? {row} : {})}];
    if (!preview(steps)) return render();
    draftStack.push({draft:draft.map(step => ({...step})), pendingSteps:pendingSteps ? pendingSteps.map(step => ({...step})) : null});
    pendingSteps = steps;
    render();
  }

  function captureRect(element) {
    if (!element) return null;
    const rect = element.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    return {left:rect.left, top:rect.top, width:rect.width, height:rect.height};
  }

  function capturePlacementMotion(steps) {
    if (!steps || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return [];
    const source = steps.find(step => step.op === 'select_source');
    const selected = steps.find(step => step.op === 'select_color');
    const placement = destinationStep(steps);
    if (!source || !selected || !placement) return [];

    const activeBoard = document.querySelector('.bg-player-board-wrapper.is-current');
    const destinationRow = placement.destination === 'pattern'
      ? activeBoard?.querySelector(`.bg-pattern-row[data-row="${Number(placement.row)}"]`)
      : null;
    const destination = destinationRow?.querySelector('.bg-pattern-slot') || destinationRow
      || activeBoard?.querySelector('.bg-floor-slot')
      || activeBoard?.querySelector('.bg-floor-tile:last-child');
    const destinationRect = captureRect(destination);
    const centerRect = captureRect(document.querySelector('.center-pool'));
    if (!destinationRect) return [];

    const motions = [];
    if (source.source === 'factory') {
      const factoryTiles = document.querySelectorAll(`.factory-tile[data-factory="${Number(source.factoryIndex)}"]`);
      const selectedTiles = Array.from(factoryTiles).filter(element => element.dataset.color === String(selected.color));
      const selectedTotal = selectedTiles.length;
      let selectedIndex = 0;
      factoryTiles.forEach(element => {
        const color = element.dataset.color;
        const chosen = color === String(selected.color);
        const target = chosen ? destinationRect : centerRect;
        const origin = captureRect(element);
        if (!origin || !target) return;
        motions.push({color, origin, target, order:chosen ? selectedIndex++ : 0, total:chosen ? selectedTotal : 1});
      });
    } else {
      const group = document.querySelector(`.center-tile-group[data-color="${String(selected.color)}"]`);
      const origin = captureRect(group?.querySelector('.center-tile'));
      const count = Math.max(1, Number(group?.dataset.count || 1));
      if (origin) {
        for (let order = 0; order < count; order += 1) {
          motions.push({color:String(selected.color), origin, target:destinationRect, order, total:count});
        }
      }
      const firstPlayerToken = document.querySelector('.center-first-player-token');
      const floor = activeBoard?.querySelector('.bg-floor-slot')
        || activeBoard?.querySelector('.bg-floor-tile:last-child');
      const tokenOrigin = captureRect(firstPlayerToken);
      const floorRect = captureRect(floor);
      if (tokenOrigin && floorRect) motions.push({color:'0', origin:tokenOrigin, target:floorRect, order:0, total:1});
    }
    return motions;
  }

  function playPlacementMotion(motions) {
    motions.forEach((motion, index) => {
      const {color, origin, target, order, total} = motion;
      const clone = document.createElement('span');
      clone.className = `azul-moving-tile ${tileClass[color]}`;
      clone.setAttribute('aria-hidden', 'true');
      Object.assign(clone.style, {
        left:`${origin.left}px`, top:`${origin.top}px`,
        width:`${origin.width}px`, height:`${origin.height}px`,
      });
      document.body.appendChild(clone);
      const spread = (order - (total - 1) / 2) * Math.min(9, target.width / Math.max(total, 1));
      const x = target.left + target.width / 2 - origin.left - origin.width / 2 + spread;
      const y = target.top + target.height / 2 - origin.top - origin.height / 2;
      const animation = clone.animate([
        {transform:'translate3d(0,0,0) scale(1)', opacity:1},
        {transform:`translate3d(${x * .55}px,${y * .55 - 16}px,0) scale(1.08)`, opacity:1, offset:.56},
        {transform:`translate3d(${x}px,${y}px,0) scale(.94)`, opacity:.96},
      ], {duration:440 + Math.min(index, 5) * 28, easing:'cubic-bezier(.2,.82,.24,1)', fill:'forwards'});
      if (animation?.finished) animation.finished.catch(() => {}).finally(() => clone.remove());
      else setTimeout(() => clone.remove(), 650);
    });
  }

  function formatAction(action, events, before, after, fallbackName) {
    const taken = (events || []).find(event => event.type === 'TilesTaken') || {};
    const sourceStep = (action?.steps || []).find(step => step.op === 'select_source');
    const colorStep = (action?.steps || []).find(step => step.op === 'select_color');
    const source = sourceStep?.source === 'factory'
      ? `第 ${Number(sourceStep.factoryIndex) + 1} 工厂`
      : '中央区域';
    const color = colorName[colorStep?.color || taken.color] || '花砖';
    const count = Number(taken.count || 0);
    const destination = taken.destination?.kind === 'pattern'
      ? `第 ${Number(taken.destination.row) + 1} 条花纹线`
      : '地板线';
    const beforeGame = before?.game || {};
    const afterGame = after?.game || {};
    const pid = Number.isInteger(taken.player) ? taken.player : beforeGame.currentPlayer;
    const floorBefore = beforeGame.players?.[pid]?.floor?.length || 0;
    const floorAfter = afterGame.players?.[pid]?.floor?.length || floorBefore;
    const floorDelta = Math.max(0, floorAfter - floorBefore);
    const actor = fallbackName || playerNames[pid] || `玩家 ${Number(pid) + 1}`;
    return `${actor} 从${source}拿取 ${count} 枚${color}砖，放入${destination}${floorDelta ? `；${floorDelta} 枚进入地板` : ''}。`;
  }

  function formatHistoryRecord(record) {
    const action = record.action || {};
    const events = record.events || [];
    const taken = events.find(event => event.type === 'TilesTaken') || {};
    const sourceStep = (action.steps || []).find(step => step.op === 'select_source');
    const colorStep = (action.steps || []).find(step => step.op === 'select_color');
    const source = sourceStep?.source === 'factory'
      ? `第 ${Number(sourceStep.factoryIndex) + 1} 工厂`
      : '中央区域';
    const color = colorName[colorStep?.color || taken.color] || '花砖';
    const count = Number(taken.count || 0);
    const destination = taken.destination?.kind === 'pattern'
      ? `第 ${Number(taken.destination.row) + 1} 条花纹线`
      : '地板线';
    const actor = playerNames[record.actor ?? taken.player] || `玩家 ${(record.actor ?? taken.player ?? 0) + 1}`;
    return `${actor} 从${source}拿取 ${count} 枚${color}砖，放入${destination}。`;
  }

  function historyFromSnapshot(snapshot) {
    const helper = window.BGLabActionHistory;
    return helper ? helper.records(snapshot, formatHistoryRecord, 20) : [];
  }

  function showActionNotice(text) {
    if (!text) return;
    actionNotice = text;
    clearTimeout(actionNoticeTimer);
    actionNoticeTimer = setTimeout(() => {
      actionNotice = '';
      render();
    }, 1200);
  }

  function recordAction(text) {
    showActionNotice(text);
  }

  function undo() {
    if (draftStack.length) {
      const previous = draftStack.pop();
      draft = previous.draft;
      pendingSteps = previous.pendingSteps;
    } else if (pendingSteps) pendingSteps = null;
    else if (draft.length) draft = [];
    lastError = '';
    render();
  }

  function restartTurn() {
    if (window.BG_REPLAY_MODE) return;
    if (turnCheckpoint) adapter.restore(JSON.parse(JSON.stringify(turnCheckpoint)));
    draft = [];
    draftStack = [];
    pendingSteps = null;
    turnCheckpoint = adapter.snapshot();
    lastError = '';
    render();
  }

  function confirm() {
    if (!pendingSteps || busy) return;
    const before = adapter.snapshot();
    const placementMotion = capturePlacementMotion(pendingSteps);
    const result = adapter.dispatch(adapter.decisionId(), {steps:pendingSteps});
    if (!result.ok) {
      lastError = `步骤 ${Number(result.failedStep) + 1}：${result.message || result.code}${result.correction ? `；${result.correction}` : ''}`;
      pendingSteps = null;
      return render();
    }
    draft = [];
    draftStack = [];
    pendingSteps = null;
    turnCheckpoint = null;
    lastError = '';
    const committedSnapshot = adapter.snapshot();
    recentActions = historyFromSnapshot(committedSnapshot);
    recordAction(recentActions[0]?.text || formatAction(result.action, result.events, before, committedSnapshot, playerNames[before.game.currentPlayer]));
    Bridge.persist();
    render();
    playPlacementMotion(placementMotion);
    setTimeout(requestAI, 0);
  }

  async function requestAI() {
    const game = state();
    if (paused || game.phase === 'finished' || playerTypes[game.currentPlayer] !== 'ai') return;
    const decisionId = adapter.decisionId();
    if (aiRequestId === decisionId) return;
    aiRequestId = decisionId;
    busy = true;
    render();
    try {
      const before = adapter.snapshot();
      const response = await Bridge.requestAITurn(game.currentPlayer);
      if (!response.retry) {
        const committedSnapshot = adapter.snapshot();
        recentActions = historyFromSnapshot(committedSnapshot);
        recordAction(recentActions[0]?.text || formatAction(response.transaction || response.action || response.canonicalAction, response.effects, before, committedSnapshot, playerNames[game.currentPlayer]));
      }
      draft = [];
      draftStack = [];
      pendingSteps = null;
      turnCheckpoint = adapter.snapshot();
      Bridge.persist();
    } catch (error) {
      paused = error.message || String(error);
    } finally {
      busy = false;
      aiRequestId = null;
      render();
      if (!paused) setTimeout(requestAI, 0);
    }
  }

  function randomSeed() { return crypto.getRandomValues(new Uint32Array(1))[0]; }
  function normalizeConfig(config, playerCount) {
    manualTest = Boolean(config.manualTest || config.mode === 'manual-test');
    playerTypes = Array.isArray(config.playerTypes) ? config.playerTypes.slice() : Array(playerCount).fill('human');
    playerNames = Array.isArray(config.names) ? config.names.slice() : [];
    viewerSeat = Number.isInteger(config.viewerSeat) ? config.viewerSeat : Math.max(0, playerTypes.indexOf('human'));
  }
  function start(config = {}) {
    const playerCount = Number(config.playerCount || config.playerTypes?.length || 2);
    normalizeConfig(config, playerCount);
    paused = null; busy = false; draft = []; draftStack = []; pendingSteps = null; turnCheckpoint = null; lastError = ''; recentActions = []; actionNotice = '';
    adapter.start({playerCount, names:playerNames, seed:config.seed ?? randomSeed()});
    recentActions = historyFromSnapshot(adapter.snapshot());
    turnCheckpoint = adapter.snapshot();
    if (window.BG_GAME_ID) Bridge.init();
    render();
    setTimeout(requestAI, 0);
  }
  function restore(snapshot, config = {}) {
    const playerCount = Number(snapshot?.wrapper?.playerCount || snapshot?.game?.playerCount || 2);
    normalizeConfig(config, playerCount);
    paused = null; busy = false; draft = []; draftStack = []; pendingSteps = null; turnCheckpoint = null; lastError = ''; recentActions = []; actionNotice = '';
    adapter.restore(snapshot);
    recentActions = historyFromSnapshot(adapter.snapshot());
    turnCheckpoint = adapter.snapshot();
    if (window.BG_GAME_ID) Bridge.init();
    render();
    setTimeout(requestAI, 0);
  }

  function traceDestinationInteraction(kind, target) {
    const destination = target?.dataset?.destination || null;
    const row = destination === 'pattern' ? Number(target.dataset.row) : null;
    const trace = {
      kind,
      target: target?.dataset?.action || target?.tagName || null,
      destination,
      row,
      pendingSteps: pendingSteps ? pendingSteps.map(step => ({...step})) : null,
      draft: draft.map(step => ({...step})),
    };
    (window.__BglabAzulInteractionTrace ||= []).push(trace);
    window.Bridge?.trace?.('azul_' + kind, trace);
  }

  document.addEventListener('click', event => {
    const target = event.target.closest('[data-action]');
    if (!target) return;
    if (target.dataset.action === 'choose-destination') {
      if (target.disabled || busy || !colorStep()) return;
      traceDestinationInteraction('click', target);
      const destination = target.dataset.destination;
      const row = destination === 'pattern' ? Number(target.dataset.row) : undefined;
      chooseDestination(destination, row);
      return;
    }
    const action = target.dataset.action;
    if (action === 'choose-source') chooseSource(target.dataset.source, Number(target.dataset.factory), target.dataset.color);
    if (action === 'confirm') confirm();
    if (action === 'undo') {
      if (target.dataset.restartTurn === 'true') {
        restartTurn();
      } else {
        undo();
      }
    }
    if (action === 'new-game') start({playerCount:state().playerCount, playerTypes, names:playerNames, manualTest, viewerSeat});
  });
  window.addEventListener('resize', fitStages);
  window.AzulUI = {chooseSource, chooseDestination, confirm, undo, restartTurn, start, restore};
  window.BGLabFrontend = {
    start,
    restore,
    bridgeReady() { if (paused === 'Bridge not connected') { paused = null; busy = false; render(); setTimeout(requestAI, 0); } },
    pause(message) { paused = message; busy = false; render(); },
    snapshot() { return adapter.snapshot(); },
    status() { return {...adapter.snapshot().wrapper, paused}; },
  };
  render();
})();
