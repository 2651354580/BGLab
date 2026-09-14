(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.BGLabAzulPresentation = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const COLORS = Object.freeze({black:'黑色', cyan:'青色', blue:'蓝色', yellow:'黄色', red:'红色'});
  const INTERACTION_STATES = new Set([
    'idle', 'source_selectable', 'source_selected', 'target_selectable',
    'effect_pending', 'confirmation_pending', 'resolving', 'waiting_next_decision',
    'finished', 'error',
  ]);
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const playerName = (model, seat) => model?.players?.[seat]?.name || `玩家 ${Number(seat) + 1}`;
  const normalizeKind = kind => String(kind || '').toLowerCase() === 'ai' ? 'ai' : 'human';

  function sourceLabel(step, event) {
    const source = step?.source || event?.source;
    if (source?.kind === 'factory') return `第 ${number(source.index ?? step?.factoryIndex) + 1} 工厂`;
    if (source === 'factory') return `第 ${number(step?.factoryIndex) + 1} 工厂`;
    return '中央区域';
  }

  function destinationLabel(step, event) {
    const destination = event?.destination || step?.destination;
    const kind = destination?.kind || destination;
    if (kind === 'pattern') return `第 ${number(destination?.row ?? step?.row) + 1} 条花纹线`;
    return '地板线';
  }

  function eventSummary(record, event) {
    if (event?.type === 'TilesTaken') {
      const floorDelta = number(record?.outcome?.floorDelta);
      const suffix = floorDelta > 0 ? `；${floorDelta} 枚进入地板线` : '';
      return `从${sourceLabel(null, event)}拿取 ${number(event.count)} 枚${COLORS[event.color] || '花砖'}砖，放入${destinationLabel(null, event)}${suffix}`;
    }
    if (event?.type === 'WallTilePlaced') {
      return `第 ${number(event.row) + 1} 行${COLORS[event.color] || '花砖'}砖贴入墙面，获得 ${number(event.points)} 分`;
    }
    if (event?.type === 'RoundScored') {
      const delta = number(event.delta);
      return `本轮结算 ${delta >= 0 ? '+' : ''}${delta} 分（总分 ${number(event.score)}）`;
    }
    if (event?.type === 'RoundStarted') return `开始第 ${number(event.round)} 轮`;
    if (event?.type === 'FinalBonus') return `终局奖励 +${number(event.bonus)} 分（总分 ${number(event.score)}）`;
    if (event?.type === 'GameFinished') return '赢得本局';
    return '';
  }

  function compactRoundEntries(model, record, turnId) {
    const events = Array.isArray(record?.events) ? record.events : [];
    const wallRows = new Map();
    for (const event of events) {
      if (event?.type !== 'WallTilePlaced' || !Number.isInteger(event.player)) continue;
      const rows = wallRows.get(event.player) || [];
      rows.push(number(event.row) + 1);
      wallRows.set(event.player, rows);
    }
    const children = [];
    const scoredSeats = new Set();
    for (const [eventIndex, event] of events.entries()) {
      if (event?.type === 'RoundScored' && Number.isInteger(event.player) && !scoredSeats.has(event.player)) {
        scoredSeats.add(event.player);
        const rows = wallRows.get(event.player) || [];
        const rowText = rows.length ? `第 ${rows.join('、')} 行贴墙，` : '';
        const delta = number(event.delta);
        children.push({
          id:`azul-${turnId}-round-score-${event.player}-${eventIndex}`,
          turnId,
          actor:{seat:event.player, name:playerName(model, event.player)},
          kind:'score',
          summary:`${playerName(model, event.player)} ${rowText}本轮结算 ${delta >= 0 ? '+' : ''}${delta} 分（总分 ${number(event.score)}）`,
          children:[],
        });
      } else if (event?.type === 'RoundStarted') {
        children.push({
          id:`azul-${turnId}-round-start-${eventIndex}`,
          turnId, actor:null,
          kind:'phase', summary:`开始第 ${number(event.round)} 轮`, children:[],
        });
      } else if (event?.type === 'FinalBonus' && Number.isInteger(event.player)) {
        children.push({
          id:`azul-${turnId}-final-bonus-${event.player}-${eventIndex}`,
          turnId,
          actor:{seat:event.player, name:playerName(model, event.player)},
          kind:'score',
          summary:`${playerName(model, event.player)} 终局奖励 +${number(event.bonus)} 分（总分 ${number(event.score)}）`,
          children:[],
        });
      } else if (event?.type === 'GameFinished') {
        const winnerSeats = Array.isArray(event.winners)
          ? event.winners.filter(seat => Number.isInteger(seat))
          : (Number.isInteger(event.winner) ? [event.winner] : []);
        const shared = winnerSeats.length > 1;
        const winner = shared ? null : (winnerSeats[0] ?? null);
        const names = winnerSeats.map(seat => playerName(model, seat)).join('、');
        children.push({
          id:`azul-${turnId}-finished-${eventIndex}`,
          turnId,
          actor:winner === null ? null : {seat:winner, name:playerName(model, winner)},
          kind:'phase', summary:shared ? `${names} 共享胜利` : (winner === null ? '游戏结束' : `${playerName(model, winner)} 赢得本局`), children:[],
        });
      }
    }
    return children;
  }

  function timelineFromRecords(model, records) {
    const used = new Set();
    const entries = [];
    for (const [recordIndex, record] of (Array.isArray(records) ? records : []).entries()) {
      const turnId = String(record?.turnId || record?.decisionId || `turn-${recordIndex + 1}`);
      const events = Array.isArray(record?.events) ? record.events : [];
      const taken = events.find(event => event?.type === 'TilesTaken');
      const summary = String(record?.text || eventSummary(record, taken)).trim();
      if (!summary) continue;
      const seat = Number.isInteger(record?.actor)
        ? record.actor
        : (Number.isInteger(taken?.player) ? taken.player : null);
      let id = `azul-${turnId}`;
      let suffix = 1;
      while (used.has(id)) id = `azul-${turnId}-${suffix += 1}`;
      used.add(id);
      entries.push({
        id,
        turnId,
        actor:Number.isInteger(seat) ? {seat, name:playerName(model, seat)} : null,
        kind:taken ? 'movement' : 'choice',
        summary,
        children:[],
      });
      entries.push(...compactRoundEntries(model, record, turnId));
    }
    return entries;
  }

  function defaultInteraction(model) {
    if (String(model?.phase) === 'finished') return {
      state:'finished', instruction:'对局已结束', canCancel:false, canSkip:false, canConfirm:false,
    };
    if (model?.viewerSeat == null || model?.viewerSeat !== model?.currentPlayer) return {
      state:'waiting_next_decision', instruction:`等待${playerName(model, model?.currentPlayer)}行动`,
      canCancel:false, canSkip:false, canConfirm:false,
    };
    const draft = Array.isArray(model?.draft) ? model.draft : [];
    const hasSource = draft.some(step => step.op === 'select_source');
    const hasColor = draft.some(step => step.op === 'select_color');
    const hasDestination = draft.some(step => step.op === 'place_tiles');
    if (hasDestination) return {
      state:'confirmation_pending', instruction:'确认本轮花砖放置',
      validatedSteps:['来源已验证', '颜色已验证', '落点已验证'],
      canCancel:true, canSkip:false, canConfirm:true,
    };
    if (hasColor) return {
      state:'target_selectable', instruction:'请选择花纹线或地板线',
      sourceLabel:'已选择花砖来源与颜色', targetLabel:'选择放置位置',
      validatedSteps:['来源已验证', '颜色已验证'], canCancel:true, canSkip:false, canConfirm:false,
    };
    if (hasSource) return {
      state:'source_selected', instruction:'请选择花砖颜色',
      sourceLabel:'已选择来源', canCancel:true, canSkip:false, canConfirm:false,
    };
    return {
      state:'source_selectable', instruction:'请选择一个工坊或中央区域中的花砖',
      canCancel:false, canSkip:false, canConfirm:false,
    };
  }

  function interactionView(model, interaction) {
    const source = interaction && typeof interaction === 'object' ? interaction : defaultInteraction(model);
    const result = {};
    if (INTERACTION_STATES.has(source.state)) result.state = source.state;
    else result.state = defaultInteraction(model).state;
    result.instruction = String(source.instruction || defaultInteraction(model).instruction);
    for (const key of ['sourceLabel', 'targetLabel', 'cancelLabel', 'skipLabel', 'confirmLabel']) {
      if (source[key] != null) result[key] = String(source[key]);
    }
    result.validatedSteps = Array.isArray(source.validatedSteps) ? source.validatedSteps.map(String) : [];
    result.canCancel = Boolean(source.canCancel);
    result.canSkip = Boolean(source.canSkip);
    result.canConfirm = Boolean(source.canConfirm);
    if (source.error && typeof source.error === 'object') {
      result.error = {code:String(source.error.code || 'error'), message:String(source.error.message || '')};
    }
    return result;
  }

  function gameView(model, interaction) {
    const phase = String(model?.phase || 'draft');
    const factories = Array.isArray(model?.factories) ? model.factories.length : 0;
    const center = Array.isArray(model?.center) ? model.center.length : 0;
    const first = model?.firstPlayerTokenAvailable ? '有先手标记' : '无先手标记';
    return {
      id:'azul',
      title:'花砖物语',
      phaseLabel:phase === 'finished' ? '终局结算' : `第 ${number(model?.round)} 轮 · 行动 ${number(model?.turn)}`,
      progressLabel:`工坊 ${factories} 个 · 中央区 ${center} 枚 · ${first}${phase === 'scoring' ? ' · 墙面结算' : ''}`,
      progressItems:[
        {id:'round', label:'轮次', value:number(model?.round)},
        {id:'action', label:'行动', value:number(model?.turn)},
        {id:'factories', label:'工坊', value:factories},
        {id:'center', label:'中央', value:center},
        {id:'first-player', label:'先手', value:model?.firstPlayerTokenAvailable ? '有' : '无'},
      ],
      ...(phase === 'finished' ? {progressPercent:100} : {}),
      primaryInstruction:interaction?.instruction || (phase === 'finished' ? '对局已结束' : '请选择一个工坊或中央区域中的花砖'),
    };
  }

  function fieldsForPlayer(player) {
    const lines = Array.isArray(player?.patternLines) ? player.patternLines : [];
    const patternSummary = Array.from({length:5}, (_, row) => {
      const line = Array.isArray(lines[row]) ? lines[row] : [];
      return `${line.length}/${row + 1}`;
    }).join(' · ');
    const wall = Array.isArray(player?.wall) ? player.wall : [];
    const wallCount = wall.flat().filter(Boolean).length;
    const completeLines = lines.filter((line, row) => Array.isArray(line) && line.length === row + 1).length;
    const floor = Array.isArray(player?.floorLine)
      ? player.floorLine
      : (Array.isArray(player?.floor) ? player.floor : []);
    return [
      {id:'pattern', label:'花纹线', fields:[
        {id:'pattern-summary', label:'填充进度', value:patternSummary},
      ]},
      {id:'board', label:'版图', fields:[
        {id:'wall-count', label:'墙面花砖', value:`${wallCount}/25`},
        {id:'complete-lines', label:'待贴墙行', value:completeLines},
        {id:'floor-count', label:'地板花砖', value:`${floor.length}/7`},
      ]},
    ];
  }

  function finalResult(model) {
    if (String(model?.phase) !== 'finished') return undefined;
    const winnerSeats = Array.isArray(model?.winners) ? model.winners.slice() : (Number.isInteger(model?.winner) ? [model.winner] : []);
    const winnerNames = winnerSeats.map(seat => playerName(model, seat)).join('、');
    const players = [...(model?.players || [])].sort((left, right) => left.seat - right.seat).map(player => ({
      player,
      bonuses:wallBonusFacts(player),
    }));
    return {
      title:'花砖物语 · 终局计分',
      columns:players.map(({player}) => playerName(model, player.seat)),
      winnerSeats,
      summary:winnerSeats.length > 1
        ? `${winnerNames} 共享胜利`
        : (winnerSeats.length ? `${playerName(model, winnerSeats[0])} 获胜` : '对局已结束'),
      rows:[
        {id:'total', label:'总分', values:players.map(({player}) => number(player.score))},
        {id:'completed-rows', label:'完整横行（2分/行）', values:players.map(({bonuses}) => bonuses.rowBonus)},
        {id:'completed-columns', label:'完整竖列（7分/列）', values:players.map(({bonuses}) => bonuses.columnBonus)},
        {id:'completed-color-sets', label:'五同色（10分/色）', values:players.map(({bonuses}) => bonuses.colorSetBonus)},
      ],
    };
  }

  function wallBonusFacts(player) {
    const wall = Array.isArray(player?.wall) ? player.wall : [];
    const completeRows = wall.filter(row => Array.isArray(row) && row.length === 5 && row.every(Boolean)).length;
    const completeColumns = Array.from({length:5}, (_, column) => (
      wall.length === 5 && wall.every(row => Array.isArray(row) && Boolean(row[column]))
    )).filter(Boolean).length;
    const completeColorSets = Object.keys(COLORS).filter(color => (
      wall.flat().filter(tile => tile === color).length === 5
    )).length;
    return {
      rowBonus:completeRows * 2,
      columnBonus:completeColumns * 7,
      colorSetBonus:completeColorSets * 10,
    };
  }

  function buildShellView(model, interaction, records) {
    const safeModel = model || {};
    const activeSeat = Number.isInteger(safeModel.currentPlayer) ? safeModel.currentPlayer : null;
    const result = {
      game:gameView(safeModel, interaction),
      activeSeat,
      players:(safeModel.players || []).map(player => ({
        seat:player.seat,
        name:String(player.name || `P${Number(player.seat) + 1}`),
        kind:normalizeKind(player.kind),
        isViewer:safeModel.viewerSeat === player.seat,
        isActive:activeSeat === player.seat,
        score:Number.isFinite(Number(player.score)) ? Number(player.score) : null,
        gameFields:fieldsForPlayer(player),
      })),
      interaction:interactionView(safeModel, interaction),
      timeline:timelineFromRecords(safeModel, records),
    };
    const final = finalResult(safeModel);
    if (final) result.finalResult = final;
    return result;
  }

  return Object.freeze({COLORS, buildShellView});
});
