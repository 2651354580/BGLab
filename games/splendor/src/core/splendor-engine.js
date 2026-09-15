(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.BGLabSplendorCore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const COLORS = ['C', 'S', 'E', 'R', 'O'];
  const ALL_GEMS = [...COLORS, 'G'];
  const TYPE_TO_COLOR = ['C', 'S', 'E', 'R', 'O'];
  const RAW_CARDS = { O:[
    '1g1w1r1b,0,1','1g1w1r2b,0,1','2b1r2w,0,1','2g1r,0,1','2w2g,0,1','3g,0,1','3r1B1g,0,1','4b,1,1',
    '2b2g3w,1,2','3g2B3w,1,2','4g2r1b,2,2','5g3r,2,2','5w,2,2','6B,3,2',
    '5g3w3r3b,3,3','6r3B3g,4,3','7r,4,3','7r3B,5,3',
  ], S:[
    '1r1w1B1g,0,1','1w2B,0,1','2g2B,0,1','2g2r1w,0,1','2r1w1B1g,0,1','3B,0,1','3g1r1b,0,1','4r,1,1',
    '1r4B2w,2,2','2g3r2b,1,2','3g3B2b,1,2','5b,2,2','5w3b,2,2','6b,3,2',
    '3r3w5B3g,3,3','3b3B6w,4,3','7w,4,3','7w3b,5,3',
  ], E:[
    '1r1w1B1b,0,1','1r1w2B1b,0,1','2b2r,0,1','2r2B1b,0,1','2w1b,0,1','3b1g1w,0,1','3r,0,1','4B,1,1',
    '2b1B4w,2,2','2g3r3w,1,2','3b2B2w,1,2','5b3g,2,2','5g,2,2','6g,3,2',
    '3r5w3B3b,3,3','6b3g3w,4,3','7b,4,3','7b3g,5,3',
  ], R:[
    '1g1w1B1b,0,1','1g2B2w,0,1','1g2w1B1b,0,1','1r3B1w,0,1','2b1g,0,1','2w2r,0,1','3w,0,1','4w,1,1',
    '2r3B2w,1,2','2r3B3b,1,2','3w5B,2,2','4b2g1w,2,2','5B,2,2','6r,3,2',
    '3g3w3B5b,3,3','6g3r3b,4,3','7g,4,3','7g3r,5,3',
  ], C:[
    '1b1B3w,0,1','1r1b1B1g,0,1','1r1b1B2g,0,1','2b2B,0,1','2g1B2b,0,1','2r1B,0,1','3b,0,1','4g,1,1',
    '2r2B3g,1,2','3b3r2w,1,2','4r2B1g,2,2','5r,2,2','5r3B,2,2','6w,3,2',
    '5r3b3B3g,3,3','3r6B3w,4,3','7B,4,3','3w7B,5,3',
  ]};
  const NOBLES = [
    {id:0,cost:{C:0,S:0,E:4,R:4,O:0}}, {id:1,cost:{C:3,S:0,E:0,R:3,O:3}},
    {id:2,cost:{C:0,S:3,E:3,R:3,O:0}}, {id:3,cost:{C:3,S:3,E:3,R:0,O:0}},
    {id:4,cost:{C:4,S:4,E:0,R:0,O:0}}, {id:5,cost:{C:4,S:0,E:0,R:0,O:4}},
    {id:6,cost:{C:3,S:3,E:0,R:0,O:3}}, {id:7,cost:{C:0,S:0,E:0,R:4,O:4}},
    {id:8,cost:{C:0,S:4,E:4,R:0,O:0}}, {id:9,cost:{C:0,S:0,E:3,R:3,O:3}},
  ];

  const clone = value => JSON.parse(JSON.stringify(value));
  function canonicalJson(value) {
    if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
    if (value && typeof value === 'object') {
      return `{${Object.keys(value).sort().map(key => (
        `${JSON.stringify(key)}:${canonicalJson(value[key])}`
      )).join(',')}}`;
    }
    return JSON.stringify(value);
  }
  const canonicalClone = value => JSON.parse(canonicalJson(value));
  const canonicalEqual = (left, right) => canonicalJson(left) === canonicalJson(right);
  function hasExactKeys(value, keys) {
    return Boolean(value && typeof value === 'object' && !Array.isArray(value)
      && canonicalEqual(Object.keys(value).sort(), [...keys].sort()));
  }
  function canonicalizeActionChain(chain) {
    const value = clone(chain);
    if (!value || typeof value !== 'object' || Array.isArray(value) || !Array.isArray(value.steps)) return value;
    for (const step of value.steps) {
      if (step && typeof step === 'object' && !Array.isArray(step)
        && ['take_gem','pay_gem','discard_gem'].includes(step.op)
        && !Object.hasOwn(step, 'count')) step.count = 1;
    }
    return canonicalClone(value);
  }
  function decodeSetupAdjustment(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid Splendor setup adjustment');
    if (value.kind === 'move_token') {
      if (!hasExactKeys(value, ['kind','color','seat','count'])
        || typeof value.color !== 'string'
        || !Number.isInteger(value.seat)
        || !Number.isInteger(value.count)) throw new Error('invalid Splendor setup adjustment');
      return canonicalClone(value);
    }
    if (value.kind === 'move_card') {
      if (!hasExactKeys(value, ['kind','cardId','destination','seat'])
        || !Number.isInteger(value.cardId)
        || typeof value.destination !== 'string'
        || !Number.isInteger(value.seat)) throw new Error('invalid Splendor setup adjustment');
      return canonicalClone(value);
    }
    throw new Error('invalid Splendor setup adjustment');
  }
  function requireExactObject(value, keys, label) {
    if (!hasExactKeys(value, keys)) throw new Error(`invalid Splendor ${label}`);
    return value;
  }
  function decodeExactGemStore(value, trailingKeys, label) {
    requireExactObject(value, [...ALL_GEMS, ...trailingKeys], label);
    const decoded = {};
    for (const color of ALL_GEMS) {
      if (!Number.isInteger(value[color])) throw new Error(`invalid Splendor ${label}`);
      decoded[color] = value[color];
    }
    return decoded;
  }
  function decodeExactNoble(value, label) {
    requireExactObject(value, ['id','cost'], label);
    requireExactObject(value.cost, COLORS, `${label} cost`);
    if (!Number.isInteger(value.id)
      || COLORS.some(color => !Number.isInteger(value.cost[color]))) {
      throw new Error(`invalid Splendor ${label}`);
    }
    return {id:value.id, cost:Object.fromEntries(COLORS.map(color => [color, value.cost[color]]))};
  }
  function decodeV7Authority(snapshot) {
    requireExactObject(snapshot.decks, ['1','2','3'], 'decks');
    requireExactObject(snapshot._decks, ['1','2','3'], 'deck aliases');
    if (!canonicalEqual(snapshot.decks, snapshot._decks)
      || [1,2,3].some(level => !Array.isArray(snapshot.decks[level])
        || snapshot.decks[level].some(id => !Number.isInteger(id)))) {
      throw new Error('invalid Splendor decks');
    }

    const game = snapshot.gamestorage;
    const gameGems = decodeExactGemStore(game, ['cards','nobles','drawcounts'], 'bank');
    if (!Array.isArray(game.cards) || !Array.isArray(game.nobles)
      || !Array.isArray(game.drawcounts) || game.drawcounts.length !== 4
      || game.drawcounts.some(value => !Number.isInteger(value))) {
      throw new Error('invalid Splendor board');
    }
    const cards = game.cards.map(card => {
      requireExactObject(card, ['id','location'], 'market card');
      if (!Number.isInteger(card.id) || typeof card.location !== 'string') {
        throw new Error('invalid Splendor market card');
      }
      return {id:card.id, location:card.location};
    });
    const nobles = game.nobles.map(noble => decodeExactNoble(noble, 'noble'));

    if (!Array.isArray(snapshot.playerstorage)) throw new Error('invalid Splendor players');
    const playerstorage = snapshot.playerstorage.map(player => {
      const gems = decodeExactGemStore(
        player,
        ['boughtCards','storedCards','boughtNobles'],
        'player state',
      );
      if (!Array.isArray(player.boughtCards) || !Array.isArray(player.storedCards)
        || !Array.isArray(player.boughtNobles)) throw new Error('invalid Splendor player state');
      const cardRef = card => {
        requireExactObject(card, ['id'], 'player card');
        if (!Number.isInteger(card.id)) throw new Error('invalid Splendor player card');
        return {id:card.id};
      };
      return {
        ...gems,
        boughtCards:player.boughtCards.map(cardRef),
        storedCards:player.storedCards.map(cardRef),
        boughtNobles:player.boughtNobles.map(noble => decodeExactNoble(noble, 'owned noble')),
      };
    });

    const wrapper = snapshot.wrapper;
    const baseWrapperKeys = [
      'playerCount','currentPlayer','phase','turn','remainingTurns',
      'winner','winners','decisionSeq',
    ];
    const hasPendingReserve = Object.hasOwn(wrapper || {}, 'pendingReserve');
    const hasPendingNobles = Object.hasOwn(wrapper || {}, 'pendingNobles');
    if (hasPendingReserve && hasPendingNobles) throw new Error('invalid Splendor continuation');
    requireExactObject(wrapper, [
      ...baseWrapperKeys,
      ...(hasPendingReserve ? ['pendingReserve'] : []),
      ...(hasPendingNobles ? ['pendingNobles'] : []),
    ], 'wrapper');
    if (!Number.isInteger(wrapper.playerCount)
      || !Number.isInteger(wrapper.currentPlayer)
      || typeof wrapper.phase !== 'string'
      || !Number.isInteger(wrapper.turn)
      || !(wrapper.remainingTurns === null || Number.isInteger(wrapper.remainingTurns))
      || !(wrapper.winner === null || Number.isInteger(wrapper.winner))
      || !Array.isArray(wrapper.winners)
      || wrapper.winners.some(seat => !Number.isInteger(seat))
      || !Number.isInteger(wrapper.decisionSeq)) {
      throw new Error('invalid Splendor wrapper');
    }
    const decodedWrapper = {
      playerCount:wrapper.playerCount,
      currentPlayer:wrapper.currentPlayer,
      phase:wrapper.phase,
      turn:wrapper.turn,
      remainingTurns:wrapper.remainingTurns,
      winner:wrapper.winner,
      winners:clone(wrapper.winners),
      decisionSeq:wrapper.decisionSeq,
    };
    if (hasPendingReserve) {
      requireExactObject(wrapper.pendingReserve, ['resumePhase'], 'reserve continuation');
      if (typeof wrapper.pendingReserve.resumePhase !== 'string') throw new Error('invalid Splendor reserve continuation');
      decodedWrapper.pendingReserve = {resumePhase:wrapper.pendingReserve.resumePhase};
    }
    if (hasPendingNobles) {
      requireExactObject(wrapper.pendingNobles, ['resumePhase','ids'], 'noble continuation');
      if (typeof wrapper.pendingNobles.resumePhase !== 'string'
        || !Array.isArray(wrapper.pendingNobles.ids)
        || wrapper.pendingNobles.ids.some(id => !Number.isInteger(id))) {
        throw new Error('invalid Splendor noble continuation');
      }
      decodedWrapper.pendingNobles = {
        resumePhase:wrapper.pendingNobles.resumePhase,
        ids:clone(wrapper.pendingNobles.ids),
      };
    }
    if (!Number.isInteger(snapshot.rngState) || snapshot.rngState < 1 || snapshot.rngState > 0xffffffff
      || typeof snapshot.decisionId !== 'string'
      || snapshot.decisionId !== `${wrapper.turn}:${wrapper.currentPlayer}:${wrapper.decisionSeq}`
      || !snapshot.committed || typeof snapshot.committed !== 'object' || Array.isArray(snapshot.committed)) {
      throw new Error('invalid Splendor snapshot authority');
    }
    return {
      schemaVersion:7,
      rngState:snapshot.rngState,
      decks:clone(snapshot.decks),
      gamestorage:{
        ...gameGems,
        cards,
        nobles,
        drawcounts:clone(game.drawcounts),
      },
      playerstorage,
      wrapper:decodedWrapper,
      committed:{},
    };
  }
  const emptyGems = () => ({C:0,S:0,E:0,R:0,O:0,G:0});
  const gemCounts = value => Object.fromEntries(ALL_GEMS.map(color => [color, Number(value?.[color] || 0)]));
  const gemTotal = value => ALL_GEMS.reduce((sum, color) => sum + (value[color] || 0), 0);

  function parseCard(code) {
    const parts = code.split(',');
    const cost = {C:0,S:0,E:0,R:0,O:0};
    const names = {w:'C',b:'S',g:'E',r:'R',B:'O'};
    const re = /(\d+)([wbgrB])/g;
    let match;
    while ((match = re.exec(parts[0]))) cost[names[match[2]]] = Number(match[1]);
    return {cost, points:Number(parts[1]), lvl:Number(parts[2])};
  }

  function makeCardDb() {
    const result = {};
    let id = 1;
    for (const [color, cards] of Object.entries(RAW_CARDS)) {
      for (const encoded of cards) {
        const parsed = parseCard(encoded);
        result[id] = {id, lvl:parsed.lvl, type:COLORS.indexOf(color), cost:parsed.cost, points:parsed.points};
        id += 1;
      }
    }
    return result;
  }

  function previousRandomState(value) {
    const undoLeft = (input, shift) => {
      let result = input >>> 0;
      for (let width = shift; width < 32; width += shift) result = (input ^ (result << shift)) >>> 0;
      return result;
    };
    const undoRight = (input, shift) => {
      let result = input >>> 0;
      for (let width = shift; width < 32; width += shift) result = (input ^ (result >>> shift)) >>> 0;
      return result;
    };
    return undoLeft(undoRight(undoLeft(value >>> 0, 5), 17), 13);
  }

  function seedFromSetupRngState(rngState) {
    let seed = rngState >>> 0;
    for (let draw = 0; draw < 98; draw += 1) seed = previousRandomState(seed);
    return seed || 1;
  }

  function chainFromCommitted(record) {
    const result = record?.result;
    if (result?.transaction?.steps) return canonicalClone(result.transaction);
    const action = result?.action;
    if (!action || typeof action.type !== 'string') throw new Error('invalid Splendor replay history');
    const positiveSteps = (op, values) => ALL_GEMS.flatMap(color => (
      Number(values?.[color] || 0) > 0
        ? [{op, color, count:Number(values[color])}]
        : []
    ));
    if (action.type === 'take_2' || action.type === 'take_3') return {steps:[
      {op:'begin', action:'take_gems'},
      ...positiveSteps('take_gem', action.gems),
      ...positiveSteps('discard_gem', action.discard),
    ]};
    if (action.type === 'buy_market' || action.type === 'buy_reserved') {
      const noble = (result.events || []).find(event => event.type === 'noble_awarded');
      return {steps:[
        {op:'begin', action:'buy_card'},
        {op:'select_card', source:action.type === 'buy_market' ? 'market' : 'reserved', cardId:Number(action.cardId)},
        ...positiveSteps('pay_gem', action.payment),
        ...(noble ? [{op:'choose_noble', nobleId:Number(noble.nobleId)}] : []),
      ]};
    }
    if (action.type === 'reserve_market' || action.type === 'reserve_deck') {
      const target = action.type === 'reserve_market'
        ? {op:'select_card', source:'market', cardId:Number(action.cardId)}
        : {op:'select_deck', level:Number(String(action.source).split('_')[1])};
      return {steps:[
        {op:'begin', action:'reserve_card'}, target,
        ...positiveSteps('discard_gem', action.discard),
      ]};
    }
    if (action.type === 'reserve_discard') return {steps:[
      {op:'begin', action:'reserve_card'},
      ...positiveSteps('discard_gem', action.discard),
    ]};
    if (action.type === 'choose_noble') return {steps:[
      {op:'begin', action:'buy_card'},
      {op:'choose_noble', nobleId:Number(action.nobleId)},
    ]};
    throw new Error('invalid Splendor replay history');
  }

  class RuleReject extends Error {
    constructor(failedStep, validatedPrefix, code, message, correction, facts) {
      super(message);
      this.failedStep = failedStep;
      this.validatedPrefix = validatedPrefix;
      this.code = code;
      this.correction = correction;
      this.facts = facts;
    }
  }

  class SplendorEngine {
    constructor() {
      this.listeners = new Set();
      this.carddb = makeCardDb();
      this.state = null;
    }

    _random() {
      let x = this.state.rngState >>> 0;
      x ^= x << 13; x ^= x >>> 17; x ^= x << 5;
      this.state.rngState = x >>> 0;
      return this.state.rngState / 4294967296;
    }

    _shuffle(items) {
      for (let i = items.length - 1; i > 0; i -= 1) {
        const j = Math.floor(this._random() * (i + 1));
        [items[i], items[j]] = [items[j], items[i]];
      }
      return items;
    }

    start(config = {}) {
      const playerCount = Number(config.playerCount ?? 2);
      const soloBenchmark = config.soloBenchmark === true;
      if (!Number.isInteger(playerCount) || playerCount < (soloBenchmark ? 1 : 2) || playerCount > 4) throw new Error('playerCount must be 2..4 outside solo benchmark');
      const seed = (Number(config.seed ?? Date.now()) >>> 0) || 1;
      this.state = {
        schemaVersion: 7,
        rngState: seed,
        decks: {1:[],2:[],3:[]},
        gamestorage: null,
        playerstorage: [],
        wrapper: {
          playerCount,
          currentPlayer: 0,
          phase: 'playing',
          turn: 0,
          remainingTurns: null,
          winner: null,
          winners: [],
          decisionSeq: 0,
        },
        committed: {},
        history: {
          schemaVersion:1,
          setup:{playerCount, seed, soloBenchmark},
          setupAdjustments:[],
          actions:[],
        },
      };
      const ids = Object.keys(this.carddb).map(Number);
      this._shuffle(ids);
      for (const id of ids) this.state.decks[this.carddb[id].lvl].push(id);
      const gemCount = {1:4,2:4,3:5,4:7}[playerCount];
      this.state.gamestorage = {...emptyGems(), C:gemCount,S:gemCount,E:gemCount,R:gemCount,O:gemCount,G:5,cards:[],nobles:[],drawcounts:[0,0,0,0]};
      for (let level = 1; level <= 3; level += 1) {
        for (let index = 0; index < 4; index += 1) this.state.gamestorage.cards.push({id:this.state.decks[level].pop(), location:`market_${level}`});
        this.state.gamestorage.drawcounts[level] = this.state.decks[level].length;
      }
      this.state.gamestorage.nobles = this._shuffle(clone(NOBLES)).slice(0, playerCount + 1);
      this.state.playerstorage = Array.from({length:playerCount}, () => ({...emptyGems(), boughtCards:[], storedCards:[], boughtNobles:[]}));
      this._emit([{type:'game_started', playerCount, seed}]);
      return this.snapshot();
    }

    decisionId() {
      const w = this.state.wrapper;
      return `${w.turn}:${w.currentPlayer}:${w.decisionSeq}`;
    }

    snapshot() {
      if (!this.state) throw new Error('game not started');
      return clone({...this.state, carddb:this.carddb, _decks:this.state.decks, decisionId:this.decisionId()});
    }

    fork() {
      if (!this.state) throw new Error('game not started');
      const fork = new SplendorEngine();
      fork.carddb = clone(this.carddb);
      fork.state = clone(this.state);
      return fork;
    }

    canonicalSnapshotJson() { return canonicalJson(this.snapshot()); }

    applySetupAdjustments(adjustments) {
      if (!Array.isArray(adjustments) || this.state.history.actions.length) throw new Error('invalid Splendor setup adjustments');
      const decoded = adjustments.map(decodeSetupAdjustment);
      const draft = clone(this.state);
      this._applySetupAdjustments(draft, decoded);
      draft.history.setupAdjustments.push(...decoded);
      this._validateRestoredState(draft, this.carddb);
      this.state = draft;
      return this.snapshot();
    }

    _applySetupAdjustments(state, adjustments) {
      for (const adjustment of adjustments) {
        if (adjustment?.kind === 'move_token') {
          const color = adjustment.color;
          const seat = adjustment.seat;
          const count = adjustment.count;
          if (!ALL_GEMS.includes(color) || !Number.isInteger(seat) || !state.playerstorage[seat]
            || !Number.isInteger(count) || count <= 0 || state.gamestorage[color] < count) {
            throw new Error('invalid Splendor setup token transfer');
          }
          state.gamestorage[color] -= count;
          state.playerstorage[seat][color] += count;
          if (gemTotal(state.playerstorage[seat]) > 10) throw new Error('invalid Splendor setup token transfer');
          continue;
        }
        if (adjustment?.kind === 'move_card') {
          const cardId = adjustment.cardId;
          const seat = adjustment.seat;
          const destination = adjustment.destination;
          if (!this.carddb[cardId] || !Number.isInteger(seat) || !state.playerstorage[seat]
            || !['bought','reserved'].includes(destination)) throw new Error('invalid Splendor setup card transfer');
          let removed = 0;
          for (const level of [1,2,3]) {
            const before = state.decks[level].length;
            state.decks[level] = state.decks[level].filter(id => id !== cardId);
            removed += before - state.decks[level].length;
          }
          const marketBefore = state.gamestorage.cards.length;
          state.gamestorage.cards = state.gamestorage.cards.filter(card => card.id !== cardId);
          removed += marketBefore - state.gamestorage.cards.length;
          for (const player of state.playerstorage) {
            for (const field of ['boughtCards','storedCards']) {
              const before = player[field].length;
              player[field] = player[field].filter(card => card.id !== cardId);
              removed += before - player[field].length;
            }
          }
          if (removed !== 1) throw new Error('invalid Splendor setup card transfer');
          const target = destination === 'bought' ? 'boughtCards' : 'storedCards';
          state.playerstorage[seat][target].push({id:cardId});
          if (state.playerstorage[seat].storedCards.length > 3) throw new Error('invalid Splendor setup card transfer');
          this._refillMarkets(state);
          continue;
        }
        throw new Error('invalid Splendor setup adjustment');
      }
    }

    restore(snapshot) {
      if (!snapshot || !snapshot.gamestorage || !snapshot.playerstorage) throw new Error('invalid Splendor snapshot');
      const sourceVersion = snapshot.schemaVersion;
      if (!Number.isInteger(sourceVersion)) throw new Error('unsupported Splendor snapshot version');
      if (![5, 6, 7].includes(sourceVersion)) throw new Error('unsupported Splendor snapshot version');
      if (sourceVersion === 7 && !Object.hasOwn(snapshot, 'history')) throw new Error('invalid Splendor replay history');
      if (sourceVersion === 7 && !hasExactKeys(snapshot, [
        'schemaVersion','rngState','decks','gamestorage','playerstorage','wrapper',
        'committed','history','carddb','_decks','decisionId',
      ])) throw new Error('invalid Splendor snapshot keys');
      if (snapshot.decks && snapshot._decks && !canonicalEqual(snapshot.decks, snapshot._decks)) {
        throw new Error('invalid Splendor deck aliases');
      }
      const rawDecks = snapshot.decks || snapshot._decks;
      if (!rawDecks || typeof rawDecks !== 'object') throw new Error('invalid Splendor snapshot');
      const wrapper = snapshot.wrapper || {
        playerCount:snapshot.playerstorage.length,
        currentPlayer:0, phase:'playing', turn:0, remainingTurns:null, winner:null, winners:[], decisionSeq:0,
      };
      const nextCarddb = makeCardDb();
      if (snapshot.carddb && !canonicalEqual(snapshot.carddb, nextCarddb)) {
        throw new Error('invalid Splendor card database');
      }
      const game = snapshot.gamestorage;
      const players = snapshot.playerstorage;
      const nextState = sourceVersion === 7
        ? {
          ...decodeV7Authority(snapshot),
          history:this._restoreHistory(snapshot, sourceVersion),
        }
        : {
          schemaVersion:7,
          rngState:Number(snapshot.rngState || 1) >>> 0,
          decks:clone(rawDecks),
          gamestorage:{
            ...gemCounts(game),
            cards:(game.cards || []).map(card => ({
              id:Number(card.id),
              location:String(card.location),
            })),
            nobles:(game.nobles || []).map(noble => {
              const id = Number(noble.id);
              return clone(NOBLES.find(candidate => candidate.id === id) || {
                id,
                cost:Object.fromEntries(COLORS.map(color => [
                  color, Number(noble.cost?.[color] || 0),
                ])),
              });
            }),
            drawcounts:clone(game.drawcounts || [0,0,0,0]),
          },
          playerstorage:players.map(player => ({
            ...gemCounts(player),
            boughtCards:(player.boughtCards || []).map(card => ({id:Number(card.id)})),
            storedCards:(player.storedCards || []).map(card => ({id:Number(card.id)})),
            boughtNobles:(player.boughtNobles || []).map(noble => {
              const id = Number(noble.id);
              return clone(NOBLES.find(candidate => candidate.id === id) || {
                id,
                cost:Object.fromEntries(COLORS.map(color => [
                  color, Number(noble.cost?.[color] || 0),
                ])),
              });
            }),
          })),
          wrapper:{
            playerCount:Number(wrapper.playerCount ?? players.length),
            currentPlayer:Number(wrapper.currentPlayer ?? 0),
            phase:String(wrapper.phase || "playing"),
            turn:Number(wrapper.turn || 0),
            remainingTurns:wrapper.remainingTurns === null
              || wrapper.remainingTurns === undefined
              ? null : Number(wrapper.remainingTurns),
            winner:wrapper.winner === null || wrapper.winner === undefined
              ? null : Number(wrapper.winner),
            winners:Array.isArray(wrapper.winners)
              ? wrapper.winners.map(Number)
              : (wrapper.winner === null || wrapper.winner === undefined ? [] : [Number(wrapper.winner)]),
            decisionSeq:Number(wrapper.decisionSeq || 0),
            ...(wrapper.pendingReserve
              ? {pendingReserve:{resumePhase:String(wrapper.pendingReserve.resumePhase || "playing")}}
              : {}),
            ...(wrapper.pendingNobles
              ? {pendingNobles:{
                resumePhase:String(wrapper.pendingNobles.resumePhase || "playing"),
                ids:(wrapper.pendingNobles.ids || []).map(Number),
              }}
              : {}),
          },
          committed:{},
          history:this._restoreHistory(snapshot, sourceVersion),
        };
      this._validateRestoredState(nextState, nextCarddb);
      nextState.committed = this._validateReplay(nextState, snapshot.committed || {}, sourceVersion);
      this.carddb = nextCarddb;
      this.state = nextState;
      return this.snapshot();
    }

    _validateRestoredState(state, carddb) {
      if (state.schemaVersion !== 7) throw new Error('invalid Splendor snapshot version');
      if (!carddb || typeof carddb !== 'object' || Object.keys(carddb).length !== 90) throw new Error('invalid Splendor card database');
      for (const [id, card] of Object.entries(carddb)) {
        if (!card || Number(id) !== Number(card.id) || ![1,2,3].includes(card.lvl) || !Number.isInteger(card.type) || card.type < 0 || card.type >= COLORS.length || !Number.isFinite(card.points)) throw new Error('invalid Splendor card database');
        if (!card.cost || COLORS.some(color => !Number.isInteger(card.cost[color]) || card.cost[color] < 0)) throw new Error('invalid Splendor card database');
      }
      const count = state.wrapper.playerCount;
      if (!Number.isInteger(count) || count < 1 || count > 4 || state.playerstorage.length !== count) throw new Error('invalid Splendor player count');
      if (!Number.isInteger(state.wrapper.currentPlayer) || state.wrapper.currentPlayer < 0 || state.wrapper.currentPlayer >= count) throw new Error('invalid Splendor current player');
      if (!['playing','last_round','finished','reserve_discard','choose_noble'].includes(state.wrapper.phase)) throw new Error('invalid Splendor phase');
      if (![1,2,3].every(level => Array.isArray(state.decks[level]))) throw new Error('invalid Splendor decks');
      const validCounter = value => Number.isInteger(value) && value >= 0 && value <= 20;
      if (ALL_GEMS.some(color => !validCounter(state.gamestorage[color]))) throw new Error('invalid Splendor bank');
      if (!Array.isArray(state.gamestorage.cards) || !Array.isArray(state.gamestorage.nobles) || !Array.isArray(state.gamestorage.drawcounts)) throw new Error('invalid Splendor board');
      if (state.gamestorage.cards.some(card => !carddb[card.id] || !/^market_[123]$/.test(card.location))) throw new Error('invalid Splendor market');
      for (const player of state.playerstorage) {
        if (ALL_GEMS.some(color => !validCounter(player[color]))) throw new Error('invalid Splendor player gems');
        if (!Array.isArray(player.boughtCards) || !Array.isArray(player.storedCards) || !Array.isArray(player.boughtNobles) || player.storedCards.length > 3) throw new Error('invalid Splendor player state');
        if ([...player.boughtCards, ...player.storedCards].some(card => !carddb[card.id])) throw new Error('invalid Splendor player cards');
        const ownedIds = [...player.boughtCards, ...player.storedCards].map(card => card.id);
        if (new Set(ownedIds).size !== ownedIds.length) throw new Error('duplicate Splendor player card');
      }
      const cardZones = [];
      for (const level of [1,2,3]) {
        if (state.decks[level].some(id => !Number.isInteger(id) || carddb[id]?.lvl !== level)) throw new Error('invalid Splendor deck card');
        cardZones.push(...state.decks[level]);
        if (Number(state.gamestorage.drawcounts[level]) !== state.decks[level].length) throw new Error('invalid Splendor draw count');
      }
      for (const card of state.gamestorage.cards) {
        if (carddb[card.id].lvl !== Number(card.location.slice(-1))) throw new Error('invalid Splendor market card');
        cardZones.push(card.id);
      }
      for (const player of state.playerstorage) cardZones.push(
        ...player.boughtCards.map(card => card.id),
        ...player.storedCards.map(card => card.id),
      );
      const expectedCardIds = Object.keys(carddb).map(Number).sort((a,b) => a-b);
      if (cardZones.length !== expectedCardIds.length
        || new Set(cardZones).size !== expectedCardIds.length
        || !canonicalEqual([...cardZones].sort((a,b) => a-b), expectedCardIds)) {
        throw new Error('invalid Splendor card partition');
      }
      const supply = count === 4 ? 7 : count === 3 ? 5 : 4;
      for (const color of COLORS) {
        const total = state.gamestorage[color]
          + state.playerstorage.reduce((sum, player) => sum + player[color], 0);
        if (total !== supply) throw new Error('invalid Splendor token conservation');
      }
      const goldTotal = state.gamestorage.G
        + state.playerstorage.reduce((sum, player) => sum + player.G, 0);
      if (goldTotal !== 5) throw new Error('invalid Splendor token conservation');
      const canonicalNobles = new Map(NOBLES.map(noble => [noble.id, noble]));
      const nobles = [
        ...state.gamestorage.nobles,
        ...state.playerstorage.flatMap(player => player.boughtNobles),
      ];
      if (nobles.length !== count + 1 || new Set(nobles.map(noble => noble.id)).size !== nobles.length
        || nobles.some(noble => !canonicalNobles.has(noble.id)
          || !canonicalEqual(noble, canonicalNobles.get(noble.id)))) {
        throw new Error('invalid Splendor noble partition');
      }
      const reservePending = state.wrapper.pendingReserve;
      const noblePending = state.wrapper.pendingNobles;
      if ((state.wrapper.phase === 'reserve_discard') !== Boolean(reservePending)) throw new Error('invalid Splendor reserve continuation');
      if ((state.wrapper.phase === 'choose_noble') !== Boolean(noblePending)) throw new Error('invalid Splendor noble continuation');
      const continuation = reservePending || noblePending;
      if (continuation && !['playing','last_round'].includes(continuation.resumePhase)) throw new Error('invalid Splendor continuation phase');
      if (reservePending && gemTotal(state.playerstorage[state.wrapper.currentPlayer]) <= 10) throw new Error('invalid Splendor reserve continuation');
      if (noblePending) {
        if (!Array.isArray(noblePending.ids) || !noblePending.ids.length
          || new Set(noblePending.ids).size !== noblePending.ids.length
          || noblePending.ids.some(id => !state.gamestorage.nobles.some(noble => noble.id === id))) {
          throw new Error('invalid Splendor noble continuation');
        }
        const bonuses = this.bonuses(state, state.wrapper.currentPlayer);
        const eligible = state.gamestorage.nobles
          .filter(noble => COLORS.every(color => bonuses[color] >= noble.cost[color]))
          .map(noble => noble.id).sort((a,b) => a-b);
        if (!canonicalEqual([...noblePending.ids].sort((a,b) => a-b), eligible)) throw new Error('invalid Splendor noble continuation');
      }
      const basePhase = continuation?.resumePhase || state.wrapper.phase;
      if (basePhase === 'playing' && state.wrapper.remainingTurns !== null) throw new Error('invalid Splendor remaining turns');
      if (basePhase === 'last_round' && (!Number.isInteger(state.wrapper.remainingTurns) || state.wrapper.remainingTurns <= 0)) throw new Error('invalid Splendor remaining turns');
      const expectedWinners = this._winnerSeats(state);
      if (state.wrapper.phase === 'finished') {
        const expectedWinner = expectedWinners.length === 1 ? expectedWinners[0] : null;
        if (state.wrapper.remainingTurns !== 0 || state.wrapper.winner !== expectedWinner
          || !canonicalEqual(state.wrapper.winners, expectedWinners)) throw new Error('invalid Splendor terminal result');
      } else if (state.wrapper.winner !== null || state.wrapper.winners.length !== 0) {
        throw new Error('invalid Splendor terminal result');
      }
      if (!Number.isInteger(state.wrapper.turn) || state.wrapper.turn < 0
        || !Number.isInteger(state.wrapper.decisionSeq) || state.wrapper.decisionSeq < 0) {
        throw new Error('invalid Splendor turn identity');
      }
    }

    _restoreHistory(snapshot, sourceVersion) {
      const raw = snapshot.history;
      if (sourceVersion === 7 && !raw) throw new Error('invalid Splendor replay history');
      if (raw) {
        if (!hasExactKeys(raw, ['schemaVersion','setup','setupAdjustments','actions'])
          || raw.schemaVersion !== 1
          || !hasExactKeys(raw.setup, ['playerCount','seed','soloBenchmark'])
          || !Number.isInteger(raw.setup.playerCount)
          || !Number.isInteger(raw.setup.seed) || raw.setup.seed < 1 || raw.setup.seed > 0xffffffff
          || typeof raw.setup.soloBenchmark !== 'boolean'
          || !Array.isArray(raw.actions)
          || !Array.isArray(raw.setupAdjustments)) throw new Error('invalid Splendor replay history');
        const setup = {
          playerCount:raw.setup.playerCount,
          seed:raw.setup.seed,
          soloBenchmark:raw.setup.soloBenchmark,
        };
        const actions = raw.actions.map(entry => {
          if (!hasExactKeys(entry, ['decisionId','chain'])
            || typeof entry.decisionId !== 'string' || !entry.decisionId
            || !hasExactKeys(entry.chain, ['steps'])
            || !Array.isArray(entry.chain.steps)) throw new Error('invalid Splendor replay history');
          return {decisionId:entry.decisionId, chain:canonicalizeActionChain(entry.chain)};
        });
        return {
          schemaVersion:1,
          setup,
          setupAdjustments:raw.setupAdjustments.map(decodeSetupAdjustment),
          actions,
        };
      }
      const entries = Object.entries(snapshot.committed || {}).map(([decisionId, record]) => {
        const parts = decisionId.split(':').map(Number);
        if (parts.length !== 3 || parts.some(value => !Number.isInteger(value))) throw new Error('invalid Splendor replay history');
        const legacyChain = chainFromCommitted(record);
        const chain = canonicalizeActionChain(legacyChain);
        let fingerprintChain;
        try {
          fingerprintChain = JSON.parse(record?.fingerprint);
        } catch (_error) {
          throw new Error('invalid Splendor replay fingerprint');
        }
        if (!canonicalEqual(canonicalizeActionChain(fingerprintChain), chain)) throw new Error('invalid Splendor replay fingerprint');
        return {decisionId, sequence:parts[2], chain};
      }).sort((left, right) => left.sequence - right.sequence);
      if (entries.some((entry, index) => entry.sequence !== index)) throw new Error('invalid Splendor replay history');
      return {
        schemaVersion:1,
        setup:{
          playerCount:Number(snapshot.wrapper?.playerCount ?? snapshot.playerstorage.length),
          seed:seedFromSetupRngState(Number(snapshot.rngState || 1) >>> 0),
          soloBenchmark:Number(snapshot.wrapper?.playerCount ?? snapshot.playerstorage.length) === 1,
        },
        setupAdjustments:[],
        actions:entries.map(({decisionId, chain}) => ({decisionId, chain:canonicalClone(chain)})),
      };
    }

    _validateReplay(state, rawCommitted, sourceVersion) {
      const replay = new SplendorEngine();
      replay.start(state.history.setup);
      if (state.history.setupAdjustments.length) replay.applySetupAdjustments(state.history.setupAdjustments);
      for (const entry of state.history.actions) {
        if (entry.decisionId !== replay.decisionId()) throw new Error('invalid Splendor replay identity');
        const result = replay.dispatch(entry.decisionId, entry.chain);
        if (!result.ok) throw new Error('invalid Splendor replay action');
      }
      const authority = value => {
        const copy = clone(value);
        delete copy.committed;
        delete copy.history;
        return copy;
      };
      if (!canonicalEqual(authority(state), authority(replay.state))) throw new Error('invalid Splendor replay state');
      const expectedIds = state.history.actions.map(entry => entry.decisionId);
      if (!canonicalEqual(Object.keys(rawCommitted).sort(), [...expectedIds].sort())) throw new Error('invalid Splendor replay commits');
      const normalized = {};
      for (const entry of state.history.actions) {
        const raw = rawCommitted[entry.decisionId];
        const expected = replay.state.committed[entry.decisionId];
        if (sourceVersion === 7) {
          if (!hasExactKeys(raw, ['fingerprint','result'])
            || typeof raw.fingerprint !== 'string'
            || !raw.result || typeof raw.result !== 'object' || Array.isArray(raw.result)) {
            throw new Error('invalid Splendor replay commits');
          }
          const expectedKeys = Object.keys(expected.result);
          const optionalKeys = [
            ...(Object.hasOwn(raw.result, 'transaction') ? ['transaction'] : []),
            ...(Object.hasOwn(raw.result, 'outcome') ? ['outcome'] : []),
          ];
          if (!hasExactKeys(raw.result, [...expectedKeys, ...optionalKeys])
            || (Object.hasOwn(raw.result, 'outcome')
              && (!raw.result.outcome || typeof raw.result.outcome !== 'object'
                || Array.isArray(raw.result.outcome)))) {
            throw new Error('invalid Splendor replay commits');
          }
          const authorityResult = clone(raw.result);
          delete authorityResult.transaction;
          delete authorityResult.outcome;
          if (!canonicalEqual(authorityResult, expected.result)) {
            throw new Error('invalid Splendor replay commits');
          }
        }
        if (!raw || !raw.result || !canonicalEqual(raw.result.action, expected.result.action)
          || !canonicalEqual(raw.result.events, expected.result.events)
          || raw.result.boundaryReason !== expected.result.boundaryReason) {
          throw new Error('invalid Splendor replay commits');
        }
        if (sourceVersion === 7 && raw.fingerprint !== canonicalJson(entry.chain)) throw new Error('invalid Splendor replay fingerprint');
        if (raw.result.transaction && !canonicalEqual(canonicalizeActionChain(raw.result.transaction), entry.chain)) throw new Error('invalid Splendor replay transaction');
        const result = clone(expected.result);
        if (raw.result.transaction) result.transaction = canonicalClone(entry.chain);
        normalized[entry.decisionId] = {fingerprint:canonicalJson(entry.chain), result};
      }
      return normalized;
    }

    subscribe(listener) {
      this.listeners.add(listener);
      return () => this.listeners.delete(listener);
    }

    _emit(events) {
      for (const listener of this.listeners) listener(clone(events), this.snapshot());
    }

    bonuses(state, pid) {
      const result = {C:0,S:0,E:0,R:0,O:0};
      for (const owned of state.playerstorage[pid].boughtCards) {
        const card = this.carddb[owned.id];
        if (card) result[TYPE_TO_COLOR[card.type]] += 1;
      }
      return result;
    }

    score(state, pid) {
      const player = state.playerstorage[pid];
      return player.boughtCards.reduce((sum, owned) => sum + (this.carddb[owned.id]?.points || 0), 0) + player.boughtNobles.length * 3;
    }

    finalResult() {
      const state = this.state;
      if (state.wrapper.phase !== 'finished') return null;
      const players = state.playerstorage.map((player, seat) => {
        const cards = player.boughtCards.reduce(
          (sum, owned) => sum + (this.carddb[owned.id]?.points || 0),
          0,
        );
        const nobles = player.boughtNobles.length * 3;
        return {
          seat,
          total:cards + nobles,
          components:[
            {id:'cards', label:'Development cards', value:cards, formula:'sum of purchased card printed points'},
            {id:'nobles', label:'Nobles', value:nobles, formula:`3 points × ${player.boughtNobles.length} acquired nobles`},
          ],
        };
      });
      const scores = players.map(player => player.total);
      const bestScore = Math.max(...scores);
      const scoreLeaders = players.filter(player => player.total === bestScore).map(player => player.seat);
      const tieBreakers = [{
        id:'total-score',
        label:'Total score',
        values:scores,
        winner:scoreLeaders.length === 1 ? scoreLeaders[0] : null,
      }];
      let winners = scoreLeaders;
      if (scoreLeaders.length > 1) {
        const cardCounts = state.playerstorage.map(player => player.boughtCards.length);
        const fewestCards = Math.min(...scoreLeaders.map(seat => cardCounts[seat]));
        const cardLeaders = scoreLeaders.filter(seat => cardCounts[seat] === fewestCards);
        tieBreakers.push({
          id:'fewest-development-cards',
          label:'Fewest purchased development cards',
          values:cardCounts,
          winner:cardLeaders.length === 1 ? cardLeaders[0] : null,
        });
        winners = cardLeaders;
      }
      const winner = winners.length === 1 ? winners[0] : null;
      return {
        schemaVersion:1,
        winner,
        winners,
        tieBreakers,
        players,
      };
    }

    view(seat) {
      if (!this.state) throw new Error('game not started');
      const pid = Number(seat);
      if (!Number.isInteger(pid) || pid < 0 || pid >= this.state.wrapper.playerCount) throw new Error('invalid seat');
      const player = this.state.playerstorage[pid];
      const publicPlayers = this.state.playerstorage.map(item => {
        const visible = clone(item);
        visible.reservedCount = visible.storedCards.length;
        visible.storedCards = [];
        return visible;
      });
      return {
        decisionId:this.decisionId(),
        turnGroupId:`turn:${this.state.wrapper.turn}:seat:${this.state.wrapper.currentPlayer}`,
        currentPlayer:this.state.wrapper.currentPlayer,
        phase:this.state.wrapper.phase,
        ended:this.state.wrapper.phase === 'finished',
        seat:pid,
        publicState:{
          gamestorage:clone(this.state.gamestorage),
          playerstorage:publicPlayers,
          carddb:clone(this.carddb),
          wrapper:clone(this.state.wrapper),
        },
        privateState:{storedCards:clone(player.storedCards)},
        actionFamilies:['take_gems','buy_card','reserve_card'],
        constraints:{
          maxGems:10,
          reserveLimit:3,
          colors:clone(COLORS),
          handTotal:gemTotal(player),
          supply:COLORS.reduce((out, color) => ({...out, [color]:this.state.gamestorage[color]}), {}),
        },
      };
    }

    dispatch(decisionId, chain) {
      const requested = String(decisionId || '');
      const canonicalChain = canonicalizeActionChain(chain || null);
      const fingerprint = canonicalJson(canonicalChain);
      const prior = this.state.committed[requested];
      if (prior) {
        if (prior.fingerprint === fingerprint) return clone({...prior.result, duplicate:true});
        return this._rejected(requested, 0, [], 'DECISION_ALREADY_COMMITTED', '该决策已提交了不同动作', '不要再次行动；等待新的 decisionId');
      }
      if (this.state.wrapper.phase === 'finished') {
        return this._rejected(requested, 0, [], 'GAME_FINISHED', '游戏已结束，不能再提交行动', '开始新对局后再行动');
      }
      if (requested !== this.decisionId()) return this._rejected(requested, 0, [], 'STALE_DECISION', 'decisionId 已过期', `使用当前 decisionId ${this.decisionId()} 重新构造完整行动链`);
      const draft = clone(this.state);
      const events = [];
      try {
        const action = this._applyChain(draft, chain, events);
        const boundaryReason = action.boundaryReason;
        delete action.boundaryReason;
        if (!boundaryReason) this._finishTurn(draft, events);
        draft.wrapper.decisionSeq += 1;
        draft.history.actions.push({decisionId:requested, chain:canonicalChain});
        this.state = draft;
        const result = {ok:true, decisionId:requested, action, events:clone(events), snapshotVersion:7, ...(boundaryReason ? {boundaryReason} : {})};
        this.state.committed[requested] = {fingerprint, result:clone(result)};
        this._emit(events);
        return result;
      } catch (error) {
        if (!(error instanceof RuleReject)) throw error;
        const player = this.state.playerstorage[this.state.wrapper.currentPlayer];
        const facts = {
          ...(error.facts || {}),
          ...(error.code === 'GEM_UNAVAILABLE' && error.facts?.requested
            ? {
              bankCount:this.state.gamestorage[error.facts.requested.color] || 0,
              availableTakeColors:COLORS.filter(color => (this.state.gamestorage[color] || 0) > 0),
            }
            : {}),
          authoritativeTransactionState:{
            bank:gemCounts(this.state.gamestorage),
            hand:gemCounts(player),
            handTotal:gemTotal(player),
          },
        };
        return this._rejected(requested, error.failedStep, error.validatedPrefix, error.code, error.message, error.correction, facts);
      }
    }

    _rejected(decisionId, failedStep, validatedPrefix, code, message, correction, facts) {
      return {ok:false, decisionId, failedStep, validatedPrefix:clone(validatedPrefix), code, message, correction, ...(facts ? {facts:clone(facts)} : {})};
    }

    _reject(index, steps, code, message, correction, facts) {
      const safeSteps = Array.isArray(steps) ? steps : [];
      throw new RuleReject(index, clone(safeSteps.slice(0, index)), code, message, correction, facts);
    }

    _positive(step, index, steps) {
      const count = step.count ?? 1;
      if (!Number.isInteger(count) || count <= 0) this._reject(index, steps, 'INVALID_COUNT', '数量必须是正整数', '修改 count 后重新提交完整行动链');
      return count;
    }

    _discard(draft, player, step, index, steps) {
      if (!ALL_GEMS.includes(step.color)) this._reject(index, steps, 'INVALID_DISCARD_STEP', '弃牌颜色无效', '选择自己持有的宝石颜色');
      const count = this._positive(step, index, steps);
      if ((player[step.color] || 0) < count) this._reject(index, steps, 'INSUFFICIENT_GEMS', `没有足够的 ${step.color} 可弃`, '减少数量或改弃已有颜色，然后重新提交完整链');
      player[step.color] -= count;
      draft.gamestorage[step.color] += count;
      return count;
    }

    _applyChain(draft, chain, events) {
      const steps = chain && chain.steps;
      if (!Array.isArray(steps) || !steps.length || steps[0]?.op !== 'begin') this._reject(0, steps || [], 'EXPECTED_BEGIN', '第一步必须是 begin', '以 begin 步骤开始并重新提交完整行动链');
      const malformedIndex = steps.findIndex(step => !step || typeof step !== 'object' || Array.isArray(step));
      if (malformedIndex >= 0) this._reject(malformedIndex, steps, 'INVALID_STEP', '行动步骤必须是对象', '删除空步骤或改为合法的动作对象');
      const family = steps[0].action;
      if (!['take_gems','buy_card','reserve_card'].includes(family)) this._reject(0, steps, 'UNKNOWN_ACTION', '未知行动', '使用 view.actionFamilies 中的动作族');
      const pid = draft.wrapper.currentPlayer;
      const player = draft.playerstorage[pid];
      const initialTotal = gemTotal(player);
      // A noble visit ends any main action, not only a purchase. Keep this
      // shared choice outside the token/reservation step grammars.
      const trailingNoble = steps.at(-1)?.op === 'choose_noble'
        ? {...steps.at(-1), index:steps.length - 1} : null;
      const actionSteps = trailingNoble ? steps.slice(0, -1) : steps;
      let canonical;

      if (draft.wrapper.phase === 'choose_noble') {
        if (family !== 'buy_card') this._reject(0, steps, 'EXPECTED_NOBLE_CHOICE', '必须完成贵族选择', '以 begin(buy_card) 和 choose_noble 完成当前决策');
        return this._applyPendingNobleChoice(draft, player, steps, events);
      } else if (draft.wrapper.phase === 'reserve_discard') {
        if (family !== 'reserve_card') this._reject(0, steps, 'EXPECTED_RESERVE_DISCARD', '必须完成保留后的弃牌', '以 begin(reserve_card) 和 discard_gem 完成当前决策');
        canonical = this._applyReserveDiscard(draft, player, actionSteps);
      } else if (family === 'take_gems') canonical = this._applyTake(draft, player, actionSteps, initialTotal);
      else if (family === 'buy_card') canonical = this._applyBuy(draft, player, steps);
      else canonical = this._applyReserve(draft, player, actionSteps, initialTotal);

      if (gemTotal(player) > 10 && canonical.boundaryReason !== 'new_information') this._reject(steps.length, steps, 'HAND_LIMIT_EXCEEDED', '提交后手牌超过 10', '在链末尾加入恰好足量的 discard_gem');
      const nobleChoice = canonical.nobleChoice || trailingNoble;
      delete canonical.nobleChoice;
      events.push({type:canonical.type, player:pid, action:clone(canonical)});
      if (canonical.boundaryReason) {
        if (nobleChoice) this._reject(nobleChoice.index, steps, 'NEW_INFORMATION_BOUNDARY', '保留后的弃牌尚未完成，不能提前结算贵族', '先提交保留；在后续弃牌链末尾选择符合条件的贵族');
        return canonical;
      }
      if (this._resolveNobleVisit(draft, steps, nobleChoice, events, canonical.type === 'buy_market')) canonical.boundaryReason = 'new_information';
      return canonical;
    }

    _applyTake(draft, player, steps, initialTotal) {
      const taken = {C:0,S:0,E:0,R:0,O:0};
      const discarded = emptyGems();
      let discardTotal = 0;
      let discarding = false;
      for (let index = 1; index < steps.length; index += 1) {
        const step = steps[index];
        if (step.op === 'take_gem' && !discarding) {
          if (!COLORS.includes(step.color)) this._reject(index, steps, 'INVALID_GEM_STEP', '取宝石颜色无效', '使用 C/S/E/R/O');
          const count = this._positive(step, index, steps);
          if (draft.gamestorage[step.color] < count) this._reject(index, steps, 'GEM_UNAVAILABLE', '供应不足', '选择供应中足量的颜色', {requested:{color:step.color, count}, bankCount:draft.gamestorage[step.color]});
          draft.gamestorage[step.color] -= count;
          player[step.color] += count;
          taken[step.color] += count;
        } else if (step.op === 'discard_gem') {
          discarding = true;
          const count = this._discard(draft, player, step, index, steps);
          discarded[step.color] += count;
          discardTotal += count;
        } else this._reject(index, steps, 'OUT_OF_ORDER_OPERATION', '只能先取后弃', '将所有 take_gem 放在 discard_gem 之前');
      }
      const colors = COLORS.filter(color => taken[color] > 0);
      const availableDistinct = COLORS.filter(color => this.state.gamestorage[color] > 0);
      const requiredDistinctCount = Math.min(3, availableDistinct.length);
      const distinctTake = colors.length === requiredDistinctCount && requiredDistinctCount > 0 && colors.every(color => taken[color] === 1);
      const sameColourDouble = colors.length === 1 && taken[colors[0]] === 2 && this.state.gamestorage[colors[0]] >= 4;
      if (!distinctTake && !sameColourDouble) this._reject(steps.length, steps, 'ILLEGAL_GEM_COMBINATION', '必须从每种可拿颜色各取 1（最多三种），或同色取 2', '修正取宝石步骤并重新提交完整链', {takeCounts:clone(taken), availableDistinct, requiredDistinctCount, requiredShape:'all_available_distinct_up_to_three_or_one_colour_two'});
      const needed = Math.max(0, initialTotal + Object.values(taken).reduce((a,b)=>a+b,0) - 10);
      if (discardTotal !== needed) this._reject(steps.length, steps, discardTotal < needed ? 'HAND_LIMIT_EXCEEDED' : 'UNNECESSARY_DISCARD', `必须恰好弃掉 ${needed} 个`, '调整 discard_gem 总数后重新提交完整链', {requiredDiscardCount:needed, submittedDiscardCount:discardTotal});
      return {type:sameColourDouble ? 'take_2' : 'take_3', gems:taken, discard:discarded};
    }

    _applyBuy(draft, player, steps) {
      const selected = steps[1];
      if (!selected || selected.op !== 'select_card' || !['market','reserved'].includes(selected.source)) this._reject(1, steps, 'EXPECTED_CARD_SELECTION', '必须选择市场或保留卡', '第二步使用 select_card');
      const marketIndex = draft.gamestorage.cards.findIndex(item => item.id === selected.cardId);
      const reserveIndex = player.storedCards.findIndex(item => item.id === selected.cardId);
      if ((selected.source === 'market' && marketIndex < 0) || (selected.source === 'reserved' && reserveIndex < 0)) this._reject(1, steps, 'CARD_UNAVAILABLE', '卡牌不在声明位置', '重新查询当前市场或保留区');
      const card = this.carddb[selected.cardId];
      if (!card) this._reject(1, steps, 'CARD_UNAVAILABLE', '找不到卡牌定义', '重新查询当前卡牌');
      const bonuses = this.bonuses(draft, draft.wrapper.currentPlayer);
      const required = {};
      for (const color of COLORS) required[color] = Math.max(0, (card.cost[color] || 0) - bonuses[color]);
      const payment = emptyGems();
      const paymentFacts = () => ({
        cardId:card.id,
        remainingDiscountedCost:clone(required),
        submittedPayment:clone(payment),
      });
      const nobleChoice = steps.at(-1)?.op === 'choose_noble'
        ? {...steps.at(-1), index:steps.length - 1}
        : null;
      const paymentEnd = nobleChoice ? steps.length - 1 : steps.length;
      for (let index = 2; index < paymentEnd; index += 1) {
        const step = steps[index];
        if (step.op !== 'pay_gem' || !ALL_GEMS.includes(step.color)) this._reject(index, steps, 'INVALID_PAYMENT_STEP', '支付步骤无效', '只使用 pay_gem(color,count)');
        const count = this._positive(step, index, steps);
        if (payment[step.color] + count > (player[step.color] || 0)) this._reject(index, steps, 'INSUFFICIENT_GEMS', '支付宝石不足', '按当前手牌调整支付');
        payment[step.color] += count;
      }
      let shortfall = 0;
      for (const color of COLORS) {
        if (payment[color] > required[color]) this._reject(steps.length, steps, 'INVALID_PAYMENT', '彩色宝石超付', '支付折扣后的准确费用', paymentFacts());
        shortfall += required[color] - payment[color];
      }
      if (payment.G !== shortfall) this._reject(steps.length, steps, 'INVALID_PAYMENT', '黄金与彩色支付不等于费用', '用黄金恰好补足彩色宝石缺口', paymentFacts());
      for (const color of ALL_GEMS) { player[color] -= payment[color]; draft.gamestorage[color] += payment[color]; }
      player.boughtCards.push({id:selected.cardId});
      if (selected.source === 'market') {
        draft.gamestorage.cards.splice(marketIndex, 1);
        this._refillMarkets(draft);
      }
      else player.storedCards.splice(reserveIndex, 1);
      return {type:selected.source === 'market' ? 'buy_market' : 'buy_reserved', cardId:selected.cardId, payment, nobleChoice};
    }

    _applyReserve(draft, player, steps, initialTotal) {
      if (player.storedCards.length >= 3) this._reject(1, steps, 'RESERVE_LIMIT', '最多保留三张', '选择其他动作');
      const target = steps[1];
      if (!target) this._reject(1, steps, 'EXPECTED_RESERVE_TARGET', '必须选择卡牌或牌堆', '第二步使用 select_card 或 select_deck');
      let cardId;
      let source;
      if (target.op === 'select_card' && target.source === 'market') {
        const index = draft.gamestorage.cards.findIndex(item => item.id === target.cardId);
        if (index < 0) this._reject(1, steps, 'CARD_UNAVAILABLE', '市场无此卡', '重新查询当前市场');
        cardId = target.cardId;
        source = 'market';
        draft.gamestorage.cards.splice(index, 1);
        this._refillMarkets(draft);
      } else if (target.op === 'select_deck' && [1,2,3].includes(target.level) && draft.decks[target.level]?.length) {
        cardId = draft.decks[target.level].pop();
        source = `deck_${target.level}`;
        draft.gamestorage.drawcounts[target.level] = draft.decks[target.level].length;
      } else this._reject(1, steps, 'DECK_UNAVAILABLE', '保留目标不可用', '选择仍存在的市场牌或非空牌堆');
      player.storedCards.push({id:cardId});
      const gainedGold = draft.gamestorage.G > 0;
      if (gainedGold) { draft.gamestorage.G -= 1; player.G += 1; }
      const needed = Math.max(0, initialTotal + (gainedGold ? 1 : 0) - 10);
      if (needed > 0) {
        if (steps.length !== 2) this._reject(2, steps, 'NEW_INFORMATION_BOUNDARY', '补出的市场牌或盲抽牌是新信息，必须先确认保留', '先提交保留目标；再在新的 decisionId 中选择弃牌');
        draft.wrapper.pendingReserve = {resumePhase:draft.wrapper.phase};
        draft.wrapper.phase = 'reserve_discard';
        return {type:source === 'market' ? 'reserve_market' : 'reserve_deck', cardId, source, gainedGold, discard:emptyGems(), boundaryReason:'new_information'};
      }
      const discarded = emptyGems();
      let discardTotal = 0;
      for (let index = 2; index < steps.length; index += 1) {
        if (steps[index].op !== 'discard_gem') this._reject(index, steps, 'OUT_OF_ORDER_OPERATION', '保留后只能弃牌', '将弃牌放在保留目标之后');
        const count = this._discard(draft, player, steps[index], index, steps);
        discarded[steps[index].color] += count;
        discardTotal += count;
      }
      if (discardTotal !== needed) this._reject(steps.length, steps, discardTotal < needed ? 'HAND_LIMIT_EXCEEDED' : 'UNNECESSARY_DISCARD', `必须恰好弃掉 ${needed} 个`, '调整 discard_gem 总数后重新提交完整链', {requiredDiscardCount:needed, submittedDiscardCount:discardTotal});
      return {type:source === 'market' ? 'reserve_market' : 'reserve_deck', cardId, source, gainedGold, discard:discarded};
    }

    _applyReserveDiscard(draft, player, steps) {
      const discarded = emptyGems();
      let discardTotal = 0;
      for (let index = 1; index < steps.length; index += 1) {
        if (steps[index].op !== 'discard_gem') this._reject(index, steps, 'OUT_OF_ORDER_OPERATION', '保留揭示后只能弃牌', '只使用 discard_gem 完成当前决策');
        const count = this._discard(draft, player, steps[index], index, steps);
        discarded[steps[index].color] += count;
        discardTotal += count;
      }
      const needed = Math.max(0, gemTotal(player) + discardTotal - 10);
      if (discardTotal !== needed) this._reject(steps.length, steps, discardTotal < needed ? 'HAND_LIMIT_EXCEEDED' : 'UNNECESSARY_DISCARD', `必须恰好弃掉 ${needed} 个`, '调整 discard_gem 总数后重新提交完整链', {requiredDiscardCount:needed, submittedDiscardCount:discardTotal});
      draft.wrapper.phase = draft.wrapper.pendingReserve?.resumePhase || 'playing';
      delete draft.wrapper.pendingReserve;
      return {type:'reserve_discard', discard:discarded};
    }

    _applyPendingNobleChoice(draft, player, steps, events) {
      const pending = draft.wrapper.pendingNobles;
      const choice = steps[1];
      if (!pending || steps.length !== 2 || choice?.op !== 'choose_noble') {
        this._reject(1, steps, 'EXPECTED_NOBLE_CHOICE', '必须选择一位符合条件的贵族', '第二步使用 choose_noble(nobleId)');
      }
      const noble = draft.gamestorage.nobles.find(item => item.id === choice.nobleId && pending.ids.includes(item.id));
      if (!noble) this._reject(1, steps, 'NOBLE_NOT_ELIGIBLE', '所选贵族当前不符合来访条件', '从 pendingNobles.ids 中选择一位', {eligibleNobles:clone(pending.ids)});
      draft.gamestorage.nobles = draft.gamestorage.nobles.filter(item => item.id !== noble.id);
      player.boughtNobles.push(noble);
      draft.wrapper.phase = pending.resumePhase;
      delete draft.wrapper.pendingNobles;
      events.push({type:'noble_awarded', player:draft.wrapper.currentPlayer, nobleId:noble.id});
      return {type:'choose_noble', nobleId:noble.id};
    }

    _resolveNobleVisit(draft, steps, choice, events, marketRevealed = false) {
      const pid = draft.wrapper.currentPlayer;
      const bonuses = this.bonuses(draft, pid);
      const eligible = draft.gamestorage.nobles.filter(noble => COLORS.every(color => bonuses[color] >= noble.cost[color]));
      const eligibleFacts = eligible.map(noble => ({id:noble.id, cost:clone(noble.cost)}));
      if (eligible.length > 1 && marketRevealed) {
        if (choice) this._reject(choice.index, steps, 'NEW_INFORMATION_BOUNDARY', '市场补牌后才能选择贵族', '先提交购买；再使用新的 decisionId 选择贵族', {eligibleNobles:eligibleFacts});
        draft.wrapper.pendingNobles = {resumePhase:draft.wrapper.phase, ids:eligible.map(noble => noble.id)};
        draft.wrapper.phase = 'choose_noble';
        return true;
      }
      if (eligible.length > 1 && !choice) {
        this._reject(steps.length, steps, 'NOBLE_SELECTION_REQUIRED', '同时满足多位贵族，必须选择其中一位', '在当前行动链末尾加入 choose_noble(nobleId)，并从 eligibleNobles 中选择', {eligibleNobles:eligibleFacts});
      }
      if (choice && eligible.length <= 1) {
        this._reject(choice.index, steps, 'UNEXPECTED_NOBLE_SELECTION', '当前行动不需要选择贵族', '仅在同时满足多位贵族时加入 choose_noble', {eligibleNobles:eligibleFacts});
      }
      const noble = choice ? eligible.find(item => item.id === choice.nobleId) : eligible[0];
      if (choice && !noble) {
        this._reject(choice.index, steps, 'NOBLE_NOT_ELIGIBLE', '所选贵族当前不符合来访条件', '从 eligibleNobles 中选择一位，并重新提交完整行动链', {eligibleNobles:eligibleFacts, selectedNobleId:choice.nobleId});
      }
      if (!noble) return;
      draft.gamestorage.nobles = draft.gamestorage.nobles.filter(item => item.id !== noble.id);
      draft.playerstorage[pid].boughtNobles.push(noble);
      events.push({type:'noble_awarded', player:pid, nobleId:noble.id});
      return false;
    }

    _refillMarkets(draft) {
      for (let level = 1; level <= 3; level += 1) {
        while (draft.gamestorage.cards.filter(card => card.location === `market_${level}`).length < 4 && draft.decks[level].length) {
          const id = draft.decks[level].pop();
          draft.gamestorage.cards.push({id, location:`market_${level}`});
        }
        draft.gamestorage.drawcounts[level] = draft.decks[level].length;
      }
    }

    _finishTurn(draft, events) {
      const pid = draft.wrapper.currentPlayer;
      let startedFinalRound = false;
      this._refillMarkets(draft);
      if (this.score(draft, pid) >= 15 && draft.wrapper.remainingTurns === null) {
        startedFinalRound = true;
        draft.wrapper.remainingTurns = Math.max(0, draft.wrapper.playerCount - pid - 1);
        draft.wrapper.phase = 'last_round';
        events.push({type:'final_round_started', player:pid});
        if (draft.wrapper.remainingTurns === 0) {
          this._finishGame(draft, events);
          return;
        }
      }
      if (draft.wrapper.remainingTurns !== null && !startedFinalRound) {
        draft.wrapper.remainingTurns -= 1;
        if (draft.wrapper.remainingTurns <= 0) {
          this._finishGame(draft, events);
          return;
        }
      }
      draft.wrapper.currentPlayer = (pid + 1) % draft.wrapper.playerCount;
      draft.wrapper.turn += 1;
    }

    _finishGame(draft, events) {
      draft.wrapper.phase = 'finished';
      draft.wrapper.winners = this._winnerSeats(draft);
      draft.wrapper.winner = draft.wrapper.winners.length === 1
        ? draft.wrapper.winners[0] : null;
      events.push({
        type:'game_finished',
        winner:draft.wrapper.winner,
        winners:clone(draft.wrapper.winners),
      });
    }

    _winnerSeats(state) {
      const scores = state.playerstorage.map((_player, pid) => this.score(state, pid));
      const bestScore = Math.max(...scores);
      const scoreLeaders = scores
        .map((score, pid) => ({score, pid}))
        .filter(item => item.score === bestScore)
        .map(item => item.pid);
      const fewestCards = Math.min(...scoreLeaders.map(pid => state.playerstorage[pid].boughtCards.length));
      return scoreLeaders.filter(pid => state.playerstorage[pid].boughtCards.length === fewestCards);
    }

    _winner(state) {
      const winners = this._winnerSeats(state);
      return winners.length === 1 ? winners[0] : null;
    }
  }

  return {SplendorEngine, COLORS, ALL_GEMS, NOBLES, makeCardDb};
});
