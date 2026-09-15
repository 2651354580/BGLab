(function () {
  'use strict';

  const COLORS = ['C','S','E','R','O'];
  const ALL = [...COLORS, 'G'];
  const NAMES = {C:'钻石',S:'蓝宝石',E:'祖母绿',R:'红宝石',O:'黑玛瑙',G:'黄金'};
  const ICONS = {C:'💎',S:'💙',E:'💚',R:'❤️',O:'🖤',G:'🟡'};
  const app = document.getElementById('app');
  const clone = value => JSON.parse(JSON.stringify(value));
  const total = gems => ALL.reduce((sum, color) => sum + (gems[color] || 0), 0);

  const UI = {
    config:null,
    view:null,
    draft:null,
    draftStack:[],
    turnCheckpoint:null,
    pendingChain:null,
    pendingNobleChoices:null,
    irreversiblePending:null,
    discard:{},
    discardRequired:0,
    pausedError:null,
    aiTimer:null,
    aiRunning:false,
    logs:[],
    actionNotice:'',
    actionNoticeTimer:null,
    messageText:'',
    deferredAdapterEvents:[],

    handleAdapterEvents(events) {
      // The adapter enriches its committed record immediately after the core
      // engine emits this synchronous event.  Persist/render on the next
      // microtask so the authority snapshot contains that complete record.
      const pending = [...events];
      Promise.resolve().then(() => {
        if (window.Bridge?.validationInFlight?.()) {
          this.deferredAdapterEvents.push(...pending);
          window.Bridge?.trace?.('adapter_events_deferred', {count:pending.length});
          return;
        }
        this.applyAdapterEvents(pending);
      });
    },

    applyAdapterEvents(events) {
      const beforeId = this.logs[0]?.decisionId;
      const snapshot = this.snapshot();
      this.syncReserveDiscard(snapshot);
      this.logs = this.historyFromSnapshot(snapshot);
      const latest = this.logs[0];
      if (latest && latest.decisionId !== beforeId) this.showActionNotice(latest.text);
      this.render();
      window.Bridge?.trace?.('render', {
        turnId:snapshot.decisionId,
        currentPlayer:snapshot.wrapper.currentPlayer,
        turn:snapshot.wrapper.turn,
      });
      window.Bridge?.persist();
      if (this.currentHuman()) this.turnCheckpoint = this.snapshot();
      this.scheduleAI();
    },

    flushConfirmedAdapterEvents() {
      if (!this.deferredAdapterEvents.length) return false;
      const events = this.deferredAdapterEvents;
      this.deferredAdapterEvents = [];
      this.applyAdapterEvents(events);
      return true;
    },

    start(config) {
      this.config = config;
      window.BGLabGameAdapter.start(config);
      this.unsubscribeAdapter?.();
      this.unsubscribeAdapter = window.BGLabGameAdapter.subscribe((events) => this.handleAdapterEvents(events));
      this.logs = this.historyFromSnapshot(this.snapshot());
      this.actionNotice = '';
      this.messageText = '';
      this.aiRunning = false;
      this.resetDraft();
      this.syncReserveDiscard(this.snapshot());
      this.turnCheckpoint = this.snapshot();
      document.getElementById('init-loading').style.display = 'none';
      this.render();
      this.scheduleAI();
    },

    restore(snapshot, config) {
      this.config = config;
      window.BGLabGameAdapter.restore(snapshot);
      this.unsubscribeAdapter?.();
      this.unsubscribeAdapter = window.BGLabGameAdapter.subscribe((events) => this.handleAdapterEvents(events));
      this.logs = this.historyFromSnapshot(this.snapshot());
      this.actionNotice = '';
      this.messageText = '';
      this.aiRunning = false;
      this.resetDraft();
      this.syncReserveDiscard(this.snapshot());
      this.turnCheckpoint = this.snapshot();
      document.getElementById('init-loading').style.display = 'none';
      this.render();
      this.scheduleAI();
    },

    snapshot() { return window.BGLabGameAdapter.snapshot(); },
    status() {
      const snapshot = this.snapshot();
      return {phase:this.pausedError ? 'paused_api_error' : snapshot.wrapper.phase, ...clone(snapshot.wrapper)};
    },
    pause(message) { this.pausedError = String(message); this.render(); },

    resetDraft() {
      this.draft = {take:{C:0,S:0,E:0,R:0,O:0}, cardId:null, payment:null};
      this.draftStack = [];
      this.pendingChain = null;
      this.pendingNobleChoices = null;
      this.discard = {};
      this.discardRequired = 0;
      this.irreversiblePending = null;
    },

    syncReserveDiscard(snapshot) {
      if (snapshot?.wrapper?.phase !== 'reserve_discard') return;
      const pid = snapshot.wrapper.currentPlayer;
      const player = snapshot.playerstorage?.[pid];
      if (!player) return;
      const required = Math.max(0, total(player) - 10);
      if (!required) return;
      if (!this.discardRequired) {
        this.discard = {};
        this.discardRequired = required;
      }
      if (!this.pendingChain) {
        this.pendingChain = {steps:[{op:'begin', action:'reserve_card'}]};
      }
    },

    viewerSeat() {
      if (this.config?.manualTest) return this.snapshot().wrapper.currentPlayer;
      const seat = this.config?.viewerSeat;
      return Number.isInteger(seat) ? seat : null;
    },

    currentHuman() {
      const wrapper = this.snapshot().wrapper;
      const viewerSeat = this.viewerSeat();
      return !window.BG_REPLAY_MODE && !this.pausedError && viewerSeat === wrapper.currentPlayer && this.config.playerTypes[viewerSeat] === 'human';
    },

    playerName(pid) { return this.config.names[pid] || `P${pid}`; },

    describe(event) {
      const actor = this.playerName(event.player);
      const gems = event.action?.gems || {};
      const gemText = Object.entries(gems).filter(([, count]) => Number(count) > 0).map(([color, count]) => `${NAMES[color] || color} ${count}`).join('、');
      if (event.type === 'take_3' || event.type === 'take_2') return `${actor} 拿取${gemText || '宝石'}。`;
      if (event.type === 'buy_market' || event.type === 'buy_reserved') {
        const card = this.snapshot().carddb?.[event.action?.cardId];
        const cardName = card ? `${NAMES[COLORS[card.type]] || '发展'}色 ${card.points} 分卡` : `卡牌 #${event.action?.cardId}`;
        const payment = Object.entries(event.action?.payment || {}).filter(([, count]) => Number(count) > 0).map(([color, count]) => `${NAMES[color] || color} ${count}`).join('、');
        return `${actor} 购买${cardName}${payment ? `，支付${payment}` : ''}。`;
      }
      if (event.type === 'reserve_deck') return `${actor} 从${event.action?.source || '牌堆'}保留一张卡牌。`;
      if (event.type === 'reserve_market') return `${actor} 保留卡牌 #${event.action?.cardId}。`;
      if (event.type === 'noble_awarded') return `${this.playerName(event.player)} 获得贵族 (+3分)`;
      if (event.type === 'final_round_started') return '触发最后一轮';
      if (event.type === 'game_finished') {
        const winners = Array.isArray(event.winners)
          ? event.winners
          : (Number.isInteger(event.winner) ? [event.winner] : []);
        return winners.length
          ? winners.map(pid => this.playerName(pid)).join('、')
            + (winners.length > 1 ? ' 共享胜利' : ' 获胜')
          : '对局已结束';
      }
      return event.type;
    },

    describeEvents(events) {
      const primary = events.find(event => ['take_3','take_2','buy_market','buy_reserved','reserve_deck','reserve_market'].includes(event.type));
      if (!primary) return events.map(event => this.describe(event)).filter(Boolean).join('；');
      let text = this.describe(primary);
      const noble = events.find(event => event.type === 'noble_awarded');
      if (noble) text = `${text.replace(/。$/, '')}，并获得贵族。`;
      return text;
    },

    historyFromSnapshot(snapshot) {
      const helper = window.BGLabActionHistory;
      if (!helper) return [];
      return helper.records(snapshot, record => {
        const events = record.events.map(event => (
          Number.isInteger(event.player) || !Number.isInteger(record.actor)
            ? event
            : {...event, player:record.actor}
        ));
        return this.describeEvents(events);
      }, 20);
    },

    shellInteraction(snapshot, model, viewerSeat) {
      const phase = snapshot?.wrapper?.phase;
      if (phase === 'finished') return {
        state:'finished', instruction:'对局已结束', canCancel:false, canSkip:false, canConfirm:false,
      };
      if (this.pausedError) return {
        state:'error', instruction:`对局已暂停：${this.pausedError}`,
        error:{code:'paused', message:String(this.pausedError)},
        canCancel:false, canSkip:false, canConfirm:false,
      };
      if (this.aiRunning) return {
        state:'resolving', instruction:`${this.playerName(snapshot.wrapper.currentPlayer)}正在思考并提交行动…`,
        canCancel:false, canSkip:false, canConfirm:false,
      };
      if (!this.currentHuman() || viewerSeat === null) return {
        state:'waiting_next_decision',
        instruction:this.config?.manualTest
          ? `轮到${this.playerName(snapshot.wrapper.currentPlayer)}，请在同一页面操作`
          : `等待${this.playerName(snapshot.wrapper.currentPlayer)}行动`,
        canCancel:false, canSkip:false, canConfirm:false,
      };
      const actionError = this.messageText
        ? {code:'action_rejected', message:this.messageText}
        : undefined;
      if (this.discardRequired) return {
        state:'confirmation_pending',
        instruction:`需弃 ${this.discardRequired}，已选 ${this.discardCount()}`,
        sourceLabel:'宝石弃置',
        validatedSteps:['已准备本次行动', '已选择弃置数量'],
        canCancel:true,
        cancelLabel:'返回本次选择',
        canSkip:false,
        canConfirm:this.discardCount() === this.discardRequired,
        confirmLabel:'确认弃置',
        ...(actionError ? {error:actionError} : {}),
      };
      const selectedGemCount = COLORS.reduce((sum, color) => sum + (this.draft.take[color] || 0), 0);
      const selectedCard = Boolean(this.draft.cardId);
      const legalTake = selectedGemCount > 0 && this.takeIsLegal(snapshot);
      const confirmation = Boolean(this.irreversiblePending || legalTake);
      const selected = selectedCard ? '已选择发展卡' : (selectedGemCount ? '已选择宝石' : '尚未选择来源');
      const state = confirmation ? 'confirmation_pending'
        : (selectedCard || selectedGemCount ? 'source_selected' : 'source_selectable');
      return {
        state,
        instruction:this.actionNotice || (confirmation ? '确认本次行动' : '选择宝石、发展卡或保留卡'),
        sourceLabel:selected,
        validatedSteps:(selectedCard || selectedGemCount) ? ['已验证当前公开选择'] : [],
        canCancel:Boolean(this.irreversiblePending),
        cancelLabel:'取消保留',
        canSkip:false,
        canConfirm:confirmation,
        confirmLabel:this.irreversiblePending ? '确认保留' : '确认拿取',
        ...(actionError ? {error:actionError} : {}),
      };
    },

    renderBoard(model, viewerSeat) {
      return `<section id="game-board" class="splendor-board" data-game-board="splendor">
        <div id="board-main" class="splendor-board-main">
          <div id="cards-area"><div id="cards">
            <div id="row_3" class="spl_cardrow"></div>
            <div id="row_2" class="spl_cardrow"></div>
            <div id="row_1" class="spl_cardrow"></div>
          </div></div>
          <div id="coinsbar" aria-label="银行宝石供应"></div>
          <div id="noblesbar" aria-label="贵族"></div>
        </div>
        <div id="player-area" data-player-seat="${viewerSeat == null ? '' : viewerSeat}">
          <div class="player-box" id="player-gems-section">
            <span class="splendor-personal-badge" aria-hidden="true">宝石</span>
            <div class="section-label">我的宝石 (<span id="gem-total">0</span>/10)</div>
            <div id="player-gems-row"></div>
          </div>
          <div class="player-box-row">
            <div class="player-box player-box-reserve" id="player-reserve-section">
              <span class="splendor-personal-badge" aria-hidden="true">发展卡</span>
              <div class="section-label">预留卡区 (<span id="reserve-count">0</span>/3)</div>
              <div id="player-reserve-gallery"></div>
            </div>
            <div class="player-box player-box-nobles" id="player-nobles-section">
              <span class="splendor-personal-badge" aria-hidden="true">贵族</span>
              <div class="section-label">贵族 (<span id="nobles-count">0</span>)</div>
              <div id="player-nobles-gallery"></div>
            </div>
          </div>
        </div>
      </section>`;
    },

    renderPrimaryActions(snapshot) {
      const enabled = this.currentHuman();
      let buttons = '';
      if (enabled && this.discardRequired) {
        const player = snapshot.playerstorage[snapshot.wrapper.currentPlayer];
        buttons = ALL.filter(color => player[color] || this.draft.take[color]).map(color => {
          const available = player[color] + (this.draft.take[color] || 0);
          const selected = this.discard[color] || 0;
          return `<button type="button" class="bglab-action-button splendor-discard-gem" data-discard-color="${color}" onclick="BGLabFrontend.chooseDiscard('${color}')" aria-label="弃置${NAMES[color]}"><span class="player-token-chip type_${color}" aria-hidden="true"></span><span>${selected}/${available}</span></button>`;
        }).join('');
        return `<div class="splendor-discard-actions">${buttons}<button type="button" class="bglab-action-button" onclick="BGLabFrontend.clearDiscard()">清除</button></div>`;
      }
      if (enabled && this.irreversiblePending) {
        return '<div class="irreversible-notice" role="status">盲抽会揭示新信息，请确认或取消。</div>';
      }
      if (enabled && snapshot.wrapper.phase === 'reserve_discard') {
        buttons += '<button class="bglab-action-button" onclick="BGLabFrontend.prepareOrCommit({steps:[{op:\'begin\',action:\'reserve_card\'}]},0)">确认弃牌</button>';
      }
      if (enabled && this.draft.cardId) {
        const pid = snapshot.wrapper.currentPlayer;
        const card = snapshot.carddb[this.draft.cardId];
        const canBuy = Boolean(card && this.payment(snapshot, pid, card));
        const canReserve = this.draft.source === 'market' && snapshot.playerstorage[pid].storedCards.length < 3;
        buttons += `<button class="bglab-action-button" onclick="BGLabFrontend.confirmBuy()"${canBuy ? '' : ' disabled title="资源不足"'}>购买</button>`;
        if (this.draft.source === 'market') {
          buttons += `<button class="bglab-action-button" onclick="BGLabFrontend.confirmReserve()"${canReserve ? '' : ' disabled title="保留区已满"'}>保留</button>`;
        }
      }
      return buttons ? `<div class="splendor-primary-actions">${buttons}</div>` : '';
    },

    renderRollbackActions() {
      if (!this.currentHuman()) return '';
      if (this.discardRequired) return '';
      const disabled = this.draftStack.length && !this.irreversiblePending ? '' : ' disabled';
      return `<div class="splendor-rollback-actions"><button class="bglab-action-button" onclick="BGLabFrontend.undoDraft()"${disabled}>撤回一步</button><button class="bglab-action-button" onclick="BGLabFrontend.restartTurn()"${disabled}>重新选择</button></div>`;
    },

    renderOverlays(snapshot) {
      let eligible = [];
      if (this.currentHuman()) {
        if (snapshot.wrapper.phase === 'choose_noble') {
          const ids = snapshot.wrapper.pendingNobles.ids;
          eligible = snapshot.gamestorage.nobles.filter(n => ids.includes(n.id));
        } else if (this.pendingNobleChoices) {
          eligible = this.pendingNobleChoices;
        } else if (this.pendingChain?.steps[0]?.action === 'buy_card' && this.draft.source === 'reserved') {
          eligible = this.eligibleNoblesAfterPurchase(snapshot, snapshot.wrapper.currentPlayer, snapshot.carddb[this.draft.cardId]);
        }
      }
      const choices = eligible.map(noble => {
        const cost = COLORS.filter(color => noble.cost[color]).map(color => `${ICONS[color]}${noble.cost[color]}`).join(' ');
        return `<button type="button" onclick="BGLabFrontend.chooseNoble(${noble.id})"><b>贵族 #${noble.id}</b><small>${cost}</small></button>`;
      }).join('');
      return `<div id="noble-overlay" class="overlay${eligible.length ? '' : ' hidden'}"><div class="overlay-box">
        <h3>选择一位贵族来访</h3><div id="noble-choices">${choices}</div>
      </div></div>`;
    },

    showActionNotice(text) {
      if (!text) return;
      this.actionNotice = text;
      clearTimeout(this.actionNoticeTimer);
      this.actionNoticeTimer = setTimeout(() => {
        this.actionNotice = '';
        this.render();
      }, 1000);
    },

    recordAction(text) {
      this.showActionNotice(text);
    },

    score(snapshot, pid) {
      const player = snapshot.playerstorage[pid];
      return player.boughtCards.reduce((sum, item) => sum + (snapshot.carddb[item.id]?.points || 0), 0) + player.boughtNobles.length * 3;
    },

    bonuses(snapshot, pid) {
      const result = {C:0,S:0,E:0,R:0,O:0};
      for (const owned of snapshot.playerstorage[pid].boughtCards) {
        const card = snapshot.carddb[owned.id];
        if (card) result[COLORS[card.type]] += 1;
      }
      return result;
    },

    render() {
      const snapshot = this.snapshot();
      this.syncReserveDiscard(snapshot);
      const wrapper = snapshot.wrapper;
      const viewerSeat = this.viewerSeat();
      this.view = viewerSeat === null ? null : window.BGLabGameAdapter.view(viewerSeat);
      this.model = window.BGLabSplendorViewModel.build(snapshot, this.view, {
        viewerSeat,
        names:this.config?.names,
        playerTypes:this.config?.playerTypes,
      }, this.draft);
      const interaction = this.shellInteraction(snapshot, this.model, viewerSeat);
      const shellView = window.BGLabSplendorPresentation.buildShellView(this.model, interaction, this.logs);
      app.innerHTML = window.BGLabGameShell.renderShell(shellView, {
        boardHtml:this.renderBoard(this.model, viewerSeat),
        actionPrimaryHtml:this.renderPrimaryActions(snapshot),
        actionRollbackHtml:this.renderRollbackActions(),
        overlayHtml:this.renderOverlays(snapshot),
      });
      window.BGLabGameShell.bindActions(app, {
        cancel:() => this.discardRequired ? this.cancelDiscard() : (this.irreversiblePending ? this.cancelIrreversible() : this.restartTurn()),
        confirm:() => this.confirmShellAction(snapshot),
      });
      const topbar = app.querySelector('.bglab-game-topbar');
      if (topbar) topbar.dataset.turnId = snapshot.decisionId;
      this.renderMarket(this.model);
      this.renderSupply(this.model);
      this.renderPlayerArea(this.model, viewerSeat);
      this.renderGameOver(snapshot);
    },

    confirmShellAction(snapshot) {
      if (!this.currentHuman()) return;
      if (this.discardRequired) return this.confirmDiscard();
      if (this.irreversiblePending) return this.confirmIrreversible();
      if (this.takeIsLegal(snapshot)) return this.confirmTake();
    },

    renderPlayers(model) {
      document.getElementById('all-players').innerHTML = model.players.map(player => {
        const tokenTotal = ALL.reduce((sum, color) => sum + player.tokens[color], 0);
        const tokens = ALL.map(color => {
          const value = player.tokens[color];
          return `<span class="player-token-chip type_${color}${value ? '' : ' is-zero'}" aria-label="${NAMES[color]} ${value}"><span class="player-token-count">${value}</span></span>`;
        }).join('');
        const discounts = COLORS.map(color => {
          const value = player.bonuses[color];
          return `<span class="player-discount-badge type_${color}${value ? '' : ' is-zero'}">${NAMES[color]} · 永久 −${value}</span>`;
        }).join('');
        const kind = this.config?.manualTest ? '手动测试' : player.kind;
        return `<div class="player-card bglab-player-card ${player.seat === model.viewerSeat ? 'is-me ' : ''}${player.seat === model.currentPlayer ? 'is-active' : ''}"><div class="player-card-header bglab-player-card-header"><div class="player-card-avatar">${this.config?.manualTest ? '测' : (player.kind === 'AI' ? 'AI' : 'P')}</div><div><div class="player-card-name">${this.escape(player.name)}${player.seat === model.currentPlayer ? ' ◀' : ''}</div><div class="player-card-detail">${kind} · ${player.purchasedCount}卡 · ${player.reservedCount}保留 · ${player.nobles.length}贵族</div></div><div class="player-card-score bglab-player-score">${player.score}分</div></div><div class="player-card-resources bglab-player-resources"><div class="player-token-row" aria-label="持有宝石"><span class="player-resource-label">持有宝石</span>${tokens}<span class="player-token-total">${tokenTotal}/10</span></div><div class="player-discount-row" aria-label="永久折扣"><span class="player-resource-label">永久折扣</span>${discounts}</div></div></div>`;
      }).join('');
    },

    renderMarket(model) {
      for (let level = 3; level >= 1; level -= 1) {
        const depleted = model.decks[level] ? '' : ' spl_depleted';
        const deck = `<button type="button" class="spl_drawpile spl_back_${level}${depleted}" data-deck-level="${level}" onclick="BGLabFrontend.reserveDeck(${level})" aria-label="保留 ${level} 级牌堆"><span class="drawpile-count">${model.decks[level]}</span></button>`;
        document.getElementById(`row_${level}`).innerHTML = deck + model.market[level].map(card => this.cardHtml(card)).join('');
      }
      document.getElementById('noblesbar').innerHTML = model.nobles.map(noble => this.nobleHtml(noble)).join('');
    },

    costHtml(cost, noble = false) {
      const cls = noble ? 'spl_noblecost' : 'spl_cardcost';
      return COLORS.filter(color => cost[color]).map(color => `<div class="${cls} type_${color}"><div class="spl_number spl_number_${cost[color]}"></div><div class="spl_minigem type_${color}"></div></div>`).join('');
    },

    cardHtml(card) {
      const selected = this.draft.cardId === card.id ? ' selected' : '';
      const reservedClass = card.source === 'reserved' ? ' spl_card--reserved' : '';
      return `<button type="button" class="spl_card${reservedClass} spl_img_${card.sprite} type_${card.color} canselect${selected}" data-card-id="${card.id}" data-card-source="${card.source}" onclick="BGLabFrontend.selectCard(${card.id},'${card.source}')" aria-label="${card.level}级${NAMES[card.color]}发展卡 ${card.points}分"><span class="spl_cardheader"><span class="spl_cardheader_gem type_${card.color}"></span></span><span class="spl_card_vp n_${Math.min(card.points, 5)}"></span><span class="spl_cardcosts">${this.costHtml(card.cost)}</span></button>`;
    },

    prefersReducedMotion() {
      return Boolean(window.matchMedia?.('(prefers-reduced-motion: reduce)').matches);
    },

    captureCardMotion(chain) {
      if (this.prefersReducedMotion() || !chain?.steps || typeof document === 'undefined') return null;
      const begin = chain.steps.find(step => step.op === 'begin');
      const selectedCard = chain.steps.find(step => step.op === 'select_card');
      const selectedDeck = chain.steps.find(step => step.op === 'select_deck');
      const action = begin?.action;
      if (action !== 'reserve_card' && action !== 'buy_card') return null;

      let source = null;
      if (selectedDeck) {
        source = document.querySelector(`#row_${selectedDeck.level} .spl_drawpile`);
      } else if (selectedCard) {
        source = document.querySelector(`[data-card-id="${selectedCard.cardId}"][data-card-source="${selectedCard.source}"]`)
          || document.querySelector(`[data-card-id="${selectedCard.cardId}"]`);
      }
      const snapshot = this.snapshot();
      const pid = snapshot.wrapper.currentPlayer;
      const playerCard = document.querySelector(`.bglab-player-card[data-seat="${pid}"]`);
      const target = action === 'reserve_card'
        ? (document.getElementById('player-reserve-gallery') || document.getElementById('player-reserve-section'))
        : (() => {
          const card = snapshot.carddb?.[selectedCard?.cardId];
          const color = Number.isInteger(card?.type) ? COLORS[card.type] : null;
          return color ? playerCard?.querySelector(`[data-field-group-id="economy"] [data-field-id="economy-${color.toLowerCase()}"]`) : null;
        })();
      if (!source || !target) return null;
      const sourceRect = source.getBoundingClientRect();
      const rawTargetRect = target.getBoundingClientRect();
      if (!sourceRect.width || !sourceRect.height || !rawTargetRect.width || !rawTargetRect.height) return null;
      const reservedCount = snapshot.playerstorage?.[pid]?.storedCards?.length || 0;
      const targetRect = action === 'reserve_card' ? {
        left:rawTargetRect.left + Math.min(reservedCount, 2) * 100,
        top:rawTargetRect.top,
        width:Math.min(92, rawTargetRect.width),
        height:Math.min(123, rawTargetRect.height),
      } : rawTargetRect;
      return {
        node:source.cloneNode(true),
        source:{left:sourceRect.left, top:sourceRect.top, width:sourceRect.width, height:sourceRect.height},
        target:{left:targetRect.left, top:targetRect.top, width:targetRect.width, height:targetRect.height},
      };
    },

    playCardMotion(motion) {
      if (!motion || this.prefersReducedMotion() || typeof document === 'undefined' || !document.body) return;
      const moving = motion.node.cloneNode(true);
      moving.classList.add('splendor-moving-card');
      moving.removeAttribute('id');
      moving.removeAttribute('onclick');
      moving.setAttribute('aria-hidden', 'true');
      moving.tabIndex = -1;
      const {source, target} = motion;
      Object.assign(moving.style, {
        left:`${source.left}px`,
        top:`${source.top}px`,
        width:`${source.width}px`,
        height:`${source.height}px`,
      });
      document.body.appendChild(moving);
      if (typeof moving.animate !== 'function') {
        moving.remove();
        return;
      }
      const dx = target.left + target.width / 2 - (source.left + source.width / 2);
      const dy = target.top + target.height / 2 - (source.top + source.height / 2);
      const scaleX = Math.min(1, Math.max(0.18, target.width / source.width));
      const scaleY = Math.min(1, Math.max(0.18, target.height / source.height));
      let animation;
      try {
        animation = moving.animate([
          {transform:'translate3d(0,0,0) scale(1)', opacity:1},
          {transform:`translate3d(${dx}px,${dy}px,0) scale(${scaleX},${scaleY})`, opacity:.72},
        ], {duration:480, easing:'cubic-bezier(.25,.46,.45,.94)', fill:'forwards'});
      } catch (error) {
        moving.remove();
        return;
      }
      let cleaned = false;
      const cleanup = () => {
        if (cleaned) return;
        cleaned = true;
        moving.remove();
      };
      animation.addEventListener?.('finish', cleanup);
      animation.addEventListener?.('cancel', cleanup);
      animation.finished?.then(cleanup, cleanup);
      setTimeout(cleanup, 900);
    },

    nobleHtml(noble, compact = false) {
      const compactStyle = compact ? ' style="width:50px;height:50px"' : '';
      return `<div id="noble_${noble.sprite}" class="spl_noble" title="贵族 #${noble.id}"${compactStyle}><div class="spl_noble_shadow"></div><div class="spl_noblecosts">${this.costHtml(noble.cost || {}, true)}</div></div>`;
    },

    renderSupply(model) {
      document.getElementById('coinsbar').innerHTML = ALL.map(color => {
        const selected = this.draft.take[color] || 0;
        const depleted = Number(model.supply[color]) > 0 ? '' : ' spl_depleted';
        return `<div class="spl_coinpile_contain type_${color}"><button type="button" id="coinpile_${color}" class="spl_coinpile type_${color}${selected ? ' gem-sel' : ''}${depleted}" onclick="BGLabFrontend.toggleGem('${color}')" aria-label="${NAMES[color]} ${model.supply[color]}个"><span class="spl_coinpile_counter">${model.supply[color]}</span></button></div>`;
      }).join('');
    },

    renderPlayerArea(model, pid) {
      if (pid === null) {
        document.getElementById('gem-total').textContent = '—';
        document.getElementById('player-gems-row').textContent = '观战者不显示私有区域';
        document.getElementById('reserve-count').textContent = '—';
        document.getElementById('player-reserve-gallery').innerHTML = '';
        document.getElementById('nobles-count').textContent = '—';
        document.getElementById('player-nobles-gallery').innerHTML = '';
        return;
      }
      const player = model.players?.[pid] || (() => {
        const legacy = model.playerstorage?.[pid];
        if (!legacy) return null;
        const mappedCards = (legacy.storedCards || []).map(owned => {
          const card = model.carddb?.[owned.id] || {};
          return {id:owned.id, level:card.lvl || 1, color:COLORS[card.type] || 'C', points:Number(card.points || 0), cost:card.cost || {}, sprite:((Number(owned.id) - 1) % 5) + 1, source:'reserved'};
        });
        return {
          tokens:Object.fromEntries(ALL.map(color => [color, Number(legacy[color] || 0)])),
          bonuses:Object.fromEntries(COLORS.map(color => [color, 0])),
          reserved:mappedCards,
          reservedCount:mappedCards.length,
          nobles:legacy.boughtNobles || [],
        };
      })();
      if (!player) return;
      const tokenTotal = ALL.reduce((sum, color) => sum + player.tokens[color], 0);
      document.getElementById('gem-total').textContent = tokenTotal;
      document.getElementById('player-gems-row').innerHTML = ALL.map(color => {
        const discount = color === 'G' ? '' : `<span class="player-gem-discount-tile" aria-label="永久折扣 ${player.bonuses[color]}">−${player.bonuses[color]}</span>`;
        return `<div class="player-gem-item type_${color}" data-gem-color="${color}" aria-label="${NAMES[color]} ${player.tokens[color]}${color === 'G' ? '' : `，永久折扣 ${player.bonuses[color]}`}" title="${NAMES[color]}">${discount}<span class="player-gem-token type_${color}"><strong>${player.tokens[color]}</strong></span></div>`;
      }).join('');
      document.getElementById('reserve-count').textContent = player.reservedCount;
      document.getElementById('player-reserve-gallery').innerHTML = player.reserved.map(card => this.cardHtml(card)).join('') || '<span class="empty-copy">暂无保留卡</span>';
      document.getElementById('nobles-count').textContent = player.nobles.length;
      document.getElementById('player-nobles-gallery').innerHTML = player.nobles.map(noble => this.nobleHtml(noble, true)).join('') || '<span class="empty-copy">暂无贵族</span>';
    },

    renderGameOver(snapshot) {
      const overlay = document.getElementById('gameover-overlay');
      if (!overlay) return;
      if (snapshot.wrapper.phase !== 'finished') { overlay.classList.add('hidden'); return; }
      overlay.classList.remove('hidden');
      const winners = Array.isArray(snapshot.wrapper.winners)
        ? snapshot.wrapper.winners
        : (Number.isInteger(snapshot.wrapper.winner) ? [snapshot.wrapper.winner] : []);
      document.getElementById('gameover-text').textContent = winners.length
        ? winners.map(pid => this.playerName(pid)).join('、')
          + (winners.length > 1 ? ' 共享胜利' : ' 获胜')
        : '对局已结束';
      document.getElementById('gameover-scores').innerHTML = snapshot.playerstorage.map((_,pid) => `<div>${this.escape(this.playerName(pid))}: ${this.score(snapshot,pid)}分</div>`).join('');
    },

    toggleGem(color) {
      if (!this.currentHuman() || !COLORS.includes(color)) return;
      const snapshot = this.snapshot();
      if ((snapshot.gamestorage[color] || 0) <= 0) return;
      this.draft.cardId = null;
      this.draft.source = null;
      this.draftStack.push(clone(this.draft));
      const selectedColors = COLORS.filter(c => this.draft.take[c]);
      const availableCount = COLORS.filter(c => (snapshot.gamestorage[c] || 0) > 0).length;
      if (this.draft.take[color] === 1 && selectedColors.length === 1 && snapshot.gamestorage[color] >= 4) this.draft.take[color] = 2;
      else if (this.draft.take[color]) this.draft.take[color] = 0;
      else if (!selectedColors.length) this.draft.take[color] = 1;
      else if (selectedColors.length < Math.min(3, availableCount) && !selectedColors.includes(color)) this.draft.take[color] = 1;
      this.render();
    },

    takeIsLegal(snapshot) {
      const selected = COLORS.filter(color => this.draft.take[color]);
      const available = COLORS.filter(color => (snapshot.gamestorage[color] || 0) > 0);
      const distinct = selected.length === Math.min(3, available.length) && selected.length > 0 && selected.every(color => this.draft.take[color] === 1);
      const sameColourDouble = selected.length === 1 && this.draft.take[selected[0]] === 2 && snapshot.gamestorage[selected[0]] >= 4;
      return distinct || sameColourDouble;
    },

    selectCard(id, source) {
      if (!this.currentHuman()) return;
      this.draftStack.push(clone(this.draft));
      this.draft.take = {C:0,S:0,E:0,R:0,O:0};
      this.draft.cardId = this.draft.cardId === id ? null : id;
      this.draft.source = this.draft.cardId ? source : null;
      this.render();
    },

    confirmTake() {
      if (!this.takeIsLegal(this.snapshot())) return this.message('必须按当前银行的可用颜色完成拿取');
      const steps = [{op:'begin', action:'take_gems'}];
      for (const color of COLORS) if (this.draft.take[color]) steps.push({op:'take_gem', color, count:this.draft.take[color]});
      this.prepareOrCommit({steps}, COLORS.reduce((sum,c)=>sum+this.draft.take[c],0));
    },

    payment(snapshot, pid, card) {
      const player = snapshot.playerstorage[pid];
      const bonuses = this.bonuses(snapshot, pid);
      const result = {C:0,S:0,E:0,R:0,O:0,G:0};
      let gold = 0;
      for (const color of COLORS) {
        const need = Math.max(0, card.cost[color] - bonuses[color]);
        result[color] = Math.min(player[color], need);
        gold += need - result[color];
      }
      if (gold > player.G) return null;
      result.G = gold;
      return result;
    },

    confirmBuy() {
      const snapshot = this.snapshot();
      const pid = snapshot.wrapper.currentPlayer;
      const card = snapshot.carddb[this.draft.cardId];
      const payment = card && this.payment(snapshot, pid, card);
      if (!payment) return this.message('资源不足，无法购买');
      const steps = [{op:'begin',action:'buy_card'},{op:'select_card',source:this.draft.source,cardId:this.draft.cardId}];
      for (const color of ALL) if (payment[color]) steps.push({op:'pay_gem',color,count:payment[color]});
      // Market refill reveals information: commit that boundary first.
      if (this.draft.source === 'market') return this.commit({steps});
      const eligible = this.eligibleNoblesAfterPurchase(snapshot, pid, card);
      if (eligible.length <= 1) return this.commit({steps});
      this.pendingChain = {steps};
      this.render();
    },

    eligibleNoblesAfterPurchase(snapshot, pid, card) {
      const bonuses = this.bonuses(snapshot, pid);
      bonuses[COLORS[card.type]] += 1;
      return snapshot.gamestorage.nobles.filter(noble => COLORS.every(color => bonuses[color] >= noble.cost[color]));
    },

    chooseNoble(nobleId) {
      if (!this.currentHuman()) return;
      const snapshot = this.snapshot();
      const chain = snapshot.wrapper.phase === 'choose_noble'
        ? {steps:[{op:'begin', action:'buy_card'}]} : this.pendingChain;
      if (!chain) return;
      return this.commit({steps:[...chain.steps, {op:'choose_noble', nobleId}]});
    },

    confirmReserve() {
      const snapshot = this.snapshot();
      const steps = [{op:'begin',action:'reserve_card'},{op:'select_card',source:'market',cardId:this.draft.cardId}];
      return this.commit({steps});
    },

    reserveDeck(level) {
      if (!this.currentHuman()) return;
      const snapshot = this.snapshot();
      const pid = snapshot.wrapper.currentPlayer;
      if (snapshot.playerstorage[pid].storedCards.length >= 3) return this.message('最多保留 3 张');
      if (!snapshot.decks[level]?.length) return this.message('牌堆已空');
      this.irreversiblePending = {level, chain:{steps:[
        {op:'begin',action:'reserve_card'},
        {op:'select_deck',level},
      ]}};
      this.render();
    },

    confirmIrreversible() {
      if (!this.irreversiblePending || !this.currentHuman()) return;
      const chain = this.irreversiblePending.chain;
      if (chain.steps.some(step => step.op === 'select_deck')) {
        this.irreversiblePending = null;
        return this.commit(chain);
      }
      const incoming = this.snapshot().gamestorage.G > 0 ? 1 : 0;
      this.irreversiblePending = null;
      this.prepareOrCommit(chain, incoming);
    },

    cancelIrreversible() {
      this.irreversiblePending = null;
      this.render();
    },

    undoDraft() {
      if (!this.currentHuman() || !this.draftStack.length || this.irreversiblePending || this.discardRequired) return;
      this.draft = this.draftStack.pop();
      this.pendingChain = null;
      this.pendingNobleChoices = null;
      this.render();
    },

    restartTurn() {
      if (!this.currentHuman() || !this.turnCheckpoint) return;
      window.BGLabGameAdapter.restore(clone(this.turnCheckpoint));
      this.resetDraft();
      this.turnCheckpoint = this.snapshot();
      this.render();
    },

    prepareOrCommit(chain, incoming) {
      const snapshot = this.snapshot();
      const player = snapshot.playerstorage[snapshot.wrapper.currentPlayer];
      const excess = Math.max(0, total(player) + incoming - 10);
      if (!excess) return this.commit(chain);
      this.pendingChain = chain;
      this.discard = {};
      this.discardRequired = excess;
      this.render();
    },

    chooseDiscard(color) {
      if (!this.discardRequired || !this.currentHuman() || !ALL.includes(color)) return;
      const snapshot = this.snapshot();
      const pid = snapshot.wrapper.currentPlayer;
      const available = snapshot.playerstorage[pid][color] + (this.draft.take[color] || 0);
      if (!available) return;
      const selected = this.discard[color] || 0;
      this.discard[color] = selected >= available ? 0 : selected + 1;
      this.render();
    },

    discardCount() {
      return Object.values(this.discard).reduce((sum, count) => sum + Number(count || 0), 0);
    },

    clearDiscard() {
      if (!this.discardRequired) return;
      this.discard = {};
      this.render();
    },

    cancelDiscard() {
      if (!this.discardRequired) return;
      this.pendingChain = null;
      this.discard = {};
      this.discardRequired = 0;
      this.render();
    },

    confirmDiscard() {
      const required = this.discardRequired;
      if (!required || !this.pendingChain) return;
      const count = this.discardCount();
      if (count !== required) return this.message(`需要恰好弃掉 ${required} 个`);
      const chain = {...this.pendingChain, steps:[...this.pendingChain.steps]};
      for (const color of ALL) if (this.discard[color]) chain.steps.push({op:'discard_gem',color,count:this.discard[color]});
      this.commit(chain);
    },

    commit(chain) {
      const cardMotion = this.captureCardMotion(chain);
      const result = window.BGLabGameAdapter.dispatch(this.view.decisionId, chain);
      if (!result.ok) {
        if (result.code === 'NOBLE_SELECTION_REQUIRED' && result.facts?.eligibleNobles?.length) {
          this.pendingChain = clone(chain);
          this.pendingNobleChoices = clone(result.facts.eligibleNobles);
          this.discardRequired = 0;
          this.render();
          return result;
        }
        this.message(result.message + '；' + result.correction);
        return result;
      }
      this.resetDraft();
      this.turnCheckpoint = null;
      this.render();
      this.playCardMotion(cardMotion);
      this.scheduleAI();
      return result;
    },

    async runCurrentAITurn() {
      const snapshot = this.snapshot();
      const pid = snapshot.wrapper.currentPlayer;
      if (snapshot.wrapper.phase === 'finished' || this.pausedError || this.aiRunning || this.config.playerTypes[pid] !== 'ai') return;
      this.aiRunning = true;
      this.render();
      try {
        const response = await window.Bridge.requestAITurn(pid);
        if (response.retry) return;
        if (!response.transaction) throw new Error('AI 未返回事务；没有执行兜底动作');
        if (!response.adapterCommitted) {
          const result = window.BGLabGameAdapter.dispatch(window.BGLabGameAdapter.view(pid).decisionId, response.transaction);
          if (!result.ok) throw new Error(`${result.code}: ${result.message}; ${result.correction}`);
        }
      } catch (error) { this.pause(error.message); }
      finally {
        this.aiRunning = false;
        this.render();
        if (!this.pausedError) this.scheduleAI();
      }
    },

    scheduleAI() {
      clearTimeout(this.aiTimer);
      const snapshot = this.snapshot();
      const pid = snapshot.wrapper.currentPlayer;
      if (snapshot.wrapper.phase !== 'finished' && !this.pausedError && this.config.playerTypes[pid] === 'ai') {
        this.aiTimer = setTimeout(() => this.runCurrentAITurn(), Number(this.config.aiDelay || 300));
      }
    },

    message(text) {
      this.messageText = String(text);
      const element = document.getElementById('game-msg');
      if (element) element.textContent = text;
      this.render();
      setTimeout(() => { if (this.messageText === String(text)) { this.messageText = ''; this.render(); } }, 4000);
    },
    escape(text) { const node = document.createElement('span'); node.textContent = String(text); return node.innerHTML; },
  };

  window.BGLabFrontend = UI;
  window.App = UI;
})();
