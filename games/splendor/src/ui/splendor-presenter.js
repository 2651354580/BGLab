(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.BGLabSplendorPresentation = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const COLORS = ['C', 'S', 'E', 'R', 'O'];
  const ALL_GEMS = [...COLORS, 'G'];
  const COLOR_NAMES = {
    C: '钻石', S: '蓝宝石', E: '祖母绿', R: '红宝石', O: '玛瑙', G: '黄金',
  };
  const ICONS = {
    C: 'gem-white', S: 'gem-blue', E: 'gem-green', R: 'gem-red', O: 'gem-black', G: 'gold',
  };
  const EVENT_KIND = Object.freeze({
    take_3: 'gain',
    take_2: 'gain',
    buy_market: 'payment',
    buy_reserved: 'payment',
    reserve_deck: 'choice',
    reserve_market: 'choice',
    reserve_discard: 'payment',
    noble_awarded: 'score',
    final_round_started: 'phase',
    game_finished: 'phase',
  });

  const integer = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const actorName = (model, seat, fallback = '玩家') => {
    const player = model?.players?.[seat];
    return player?.name || `${fallback} ${Number(seat) + 1}`;
  };
  const escapeId = value => String(value == null ? '' : value);

  function playerKind(kind) {
    return String(kind || '').toLowerCase() === 'ai' ? 'ai' : 'human';
  }

  function gemText(gems) {
    return Object.entries(gems || {})
      .filter(([, value]) => Number(value) > 0)
      .map(([color, value]) => `${COLOR_NAMES[color] || color} ${value}`)
      .join('、');
  }

  function actionFromRecord(record) {
    const action = record?.action || {};
    if (action.type) return action;
    const eventAction = (record?.events || []).find(event => event?.action?.type)?.action;
    return eventAction || action;
  }

  function eventType(record, event) {
    return event?.type || actionFromRecord(record)?.type || '';
  }

  function eventSummary(record, event, model) {
    const type = eventType(record, event);
    const action = event?.action || actionFromRecord(record) || {};
    if (type === 'take_3' || type === 'take_2') {
      return `拿取${gemText(event?.gems || action.gems) || '宝石'}`;
    }
    if (type === 'buy_market' || type === 'buy_reserved') {
      const source = type === 'buy_reserved' ? '保留卡' : '发展卡';
      const payment = gemText(event?.payment || action.payment);
      return `购买${source}${payment ? `，支付${payment}` : ''}`;
    }
    if (type === 'reserve_deck') {
      const level = action.level || event?.level;
      return `从${level ? `${level} 级` : ''}牌堆保留一张卡牌`;
    }
    if (type === 'reserve_market') {
      return '保留一张公开发展卡';
    }
    if (type === 'reserve_discard') return '完成超额宝石弃置';
    if (type === 'noble_awarded') return '获得贵族（+3 分）';
    if (type === 'final_round_started') return '触发最后一轮';
    if (type === 'game_finished') return '赢得本局';
    return '';
  }

  function timelineFromRecords(model, records) {
    const used = new Set();
    const entries = [];
    for (const [recordIndex, record] of (Array.isArray(records) ? records : []).entries()) {
      const turnId = escapeId(record?.turnId || record?.decisionId || `turn-${recordIndex + 1}`);
      const rawEvents = Array.isArray(record?.events) ? record.events : [];
      const supported = rawEvents
        .map((event, eventIndex) => ({event, eventIndex}))
        .filter(({event}) => Boolean(EVENT_KIND[eventType(record, event)]));
      const facts = supported.length
        ? supported.reverse()
        : (EVENT_KIND[actionFromRecord(record)?.type]
          ? [{event: {type: actionFromRecord(record).type, action: actionFromRecord(record)}, eventIndex: 0}]
          : []);
      for (const {event, eventIndex} of facts) {
        const type = eventType(record, event);
        const summary = eventSummary(record, event, model);
        if (!summary) continue;
        const actorSeat = Number.isInteger(event?.winner)
          ? event.winner
          : (Number.isInteger(event?.player) ? event.player : record?.actor);
        let id = `splendor-${turnId}-${eventIndex}`;
        let suffix = 1;
        while (used.has(id)) id = `splendor-${turnId}-${eventIndex}-${suffix += 1}`;
        used.add(id);
        entries.push({
          id,
          turnId,
          actor: Number.isInteger(actorSeat) ? {seat: actorSeat, name: actorName(model, actorSeat)} : null,
          kind: EVENT_KIND[type],
          summary,
          children: [],
        });
      }
    }
    return entries;
  }

  function buildGame(model, interaction) {
    const phase = String(model?.phase || 'playing');
    const turn = integer(model?.turn);
    const decks = model?.decks || {};
    const deckText = [1, 2, 3].map(level => `${level}级牌库 ${integer(decks[level])}`).join(' · ');
    const endingText = model?.remainingTurns == null ? '' : ` · 终局剩余 ${integer(model.remainingTurns)} 回合`;
    const isFinished = phase === 'finished';
    const progressItems = [1, 2, 3].map(level => ({
      id: `deck-${level}`,
      label: `${level}级`,
      value: integer(decks[level]),
    })).concat({
      id: 'nobles',
      label: '贵族',
      value: Array.isArray(model?.nobles) ? model.nobles.length : 0,
    });
    if (model?.remainingTurns != null) {
      progressItems.push({id: 'final-round', label: '终局剩余', value: integer(model.remainingTurns)});
    }
    return {
      id: 'splendor',
      title: '璀璨宝石',
      phaseLabel: isFinished ? '终局结算' : `第 ${turn + 1} 回合`,
      progressLabel: `${deckText} · 贵族 ${Array.isArray(model?.nobles) ? model.nobles.length : 0} 位${endingText}`,
      progressItems,
      ...(isFinished ? {progressPercent: 100} : {}),
      primaryInstruction: interaction?.instruction || (isFinished ? '对局已结束' : '选择宝石、发展卡或保留卡'),
    };
  }

  function economyFields(player) {
    return ALL_GEMS.map(color => ({
      id: `economy-${color.toLowerCase()}`,
      label: COLOR_NAMES[color],
      value: integer(player?.tokens?.[color]),
      ...(color === 'G' ? {} : {
        secondaryValue: integer(player?.bonuses?.[color]),
      }),
      icon: ICONS[color],
    }));
  }

  function reservedFields(player) {
    const count = player?.reservedCount == null
      ? (Array.isArray(player?.reserved) ? player.reserved.length : 0)
      : integer(player.reservedCount);
    return Array.from({length: Math.max(0, count)}, (_, index) => ({
        id: `reserved-card-${index + 1}`,
        label: `保留卡 ${index + 1}`,
        value: '',
        icon: 'reserved-back',
      }));
  }

  function playerFields(player) {
    return [
      {id: 'economy', label: '宝石与折扣', fields: economyFields(player)},
      {id: 'reserved', label: '保留卡', fields: reservedFields(player)},
    ];
  }

  function defaultInteraction(model) {
    const phase = String(model?.phase || 'playing');
    if (phase === 'finished') return {state: 'finished', instruction: '对局已结束', canCancel: false, canSkip: false, canConfirm: false};
    const draft = model?.draft || {};
    if (draft.cardId || Object.values(draft.take || {}).some(value => Number(value) > 0)) {
      return {
        state: 'confirmation_pending',
        instruction: '确认本次行动',
        sourceLabel: draft.cardId ? '已选择发展卡' : '已选择宝石',
        validatedSteps: ['已验证当前选择'],
        canCancel: true,
        canSkip: false,
        canConfirm: true,
      };
    }
    return {
      state: 'source_selectable',
      instruction: '选择宝石、发展卡或保留卡',
      canCancel: false,
      canSkip: false,
      canConfirm: false,
    };
  }

  function interactionView(model, interaction) {
    const fallback = defaultInteraction(model);
    const source = interaction && typeof interaction === 'object' ? interaction : {};
    const result = {
      state:source.state || fallback.state,
      instruction:source.instruction || fallback.instruction,
      canCancel:source.canCancel == null ? fallback.canCancel : Boolean(source.canCancel),
      canSkip:source.canSkip == null ? fallback.canSkip : Boolean(source.canSkip),
      canConfirm:source.canConfirm == null ? fallback.canConfirm : Boolean(source.canConfirm),
    };
    for (const key of ['sourceLabel', 'targetLabel', 'cancelLabel', 'skipLabel', 'confirmLabel']) {
      if (source[key] != null) result[key] = String(source[key]);
    }
    if (Array.isArray(source.validatedSteps)) result.validatedSteps = source.validatedSteps.map(String);
    if (source.error && typeof source.error === 'object') {
      result.error = {code:String(source.error.code || 'error'), message:String(source.error.message || '')};
    }
    return result;
  }

  function buildFinalResult(model) {
    if (String(model?.phase) !== 'finished') return undefined;
    const players = [...(model?.players || [])].sort((left, right) => left.seat - right.seat);
    const winner = Array.isArray(model?.winners)
      ? model.winners.filter(seat => Number.isInteger(seat))
      : (Number.isInteger(model?.winner) ? [model.winner] : []);
    return {
      title: '璀璨宝石 · 终局计分',
      columns: players.map(player => player.name || `P${Number(player.seat) + 1}`),
      winnerSeats: winner,
      summary: winner.length
        ? winner.map(seat => actorName(model, seat)).join('、')
          + (winner.length > 1 ? ' 共享胜利' : ' 获胜')
        : '对局已结束',
      rows: [
        {id: 'total', label: '总分', values: players.map(player => integer(player.score))},
        {id: 'purchased', label: '发展卡数量', values: players.map(player => integer(player.purchasedCount))},
        {id: 'nobles', label: '贵族数量', values: players.map(player => Array.isArray(player.nobles) ? player.nobles.length : 0)},
      ],
    };
  }

  function buildShellView(model, interaction, records) {
    const safeModel = model || {};
    const currentPlayer = Number.isInteger(safeModel.currentPlayer) ? safeModel.currentPlayer : null;
    return {
      game: buildGame(safeModel, interaction),
      activeSeat: currentPlayer,
      players: (safeModel.players || []).map(player => ({
        seat: player.seat,
        name: String(player.name || `P${Number(player.seat) + 1}`),
        kind: playerKind(player.kind),
        isViewer: safeModel.viewerSeat === player.seat,
        isActive: currentPlayer === player.seat,
        score: Number.isFinite(Number(player.score)) ? Number(player.score) : null,
        gameFields: playerFields(player),
      })),
      interaction: interactionView(safeModel, interaction),
      timeline: timelineFromRecords(safeModel, records),
      ...(buildFinalResult(safeModel) ? {finalResult: buildFinalResult(safeModel)} : {}),
    };
  }

  return Object.freeze({COLORS, ALL_GEMS, EVENT_KIND, buildShellView});
});
