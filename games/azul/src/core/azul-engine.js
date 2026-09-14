(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.BGLabAzulCore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const COLORS = ['black', 'cyan', 'blue', 'yellow', 'red'];
  const FLOOR_PENALTIES = [-1, -1, -2, -2, -2, -3, -3];
  const WALL_PATTERN = [
    ['blue', 'yellow', 'red', 'black', 'cyan'],
    ['cyan', 'blue', 'yellow', 'red', 'black'],
    ['black', 'cyan', 'blue', 'yellow', 'red'],
    ['red', 'black', 'cyan', 'blue', 'yellow'],
    ['yellow', 'red', 'black', 'cyan', 'blue'],
  ];

  function clone(value) { return structuredClone(value); }
  function nextRandom(state) {
    let value = state.rngState >>> 0 || 1;
    value ^= value << 13; value ^= value >>> 17; value ^= value << 5;
    state.rngState = value >>> 0;
    return state.rngState / 0x100000000;
  }
  function shuffle(state, values) {
    for (let index = values.length - 1; index > 0; index -= 1) {
      const other = Math.floor(nextRandom(state) * (index + 1));
      [values[index], values[other]] = [values[other], values[index]];
    }
  }
  function makePlayer(id, name) {
    return {
      id, name, score: 0,
      patternLines: Array.from({length: 5}, () => []),
      wall: Array.from({length: 5}, () => Array(5).fill(null)),
      floor: [],
      hasFirstPlayerMarker: false,
    };
  }
  function drawTile(state) {
    if (!state.bag.length && state.lid.length) {
      state.bag = state.lid.splice(0);
      shuffle(state, state.bag);
    }
    return state.bag.pop();
  }
  function fillFactories(state) {
    state.factories = Array.from({length: state.playerCount * 2 + 1}, () => []);
    for (const factory of state.factories) {
      while (factory.length < 4) {
        const tile = drawTile(state);
        if (tile === undefined) break;
        factory.push(tile);
      }
    }
  }
  function createGame(config = {}) {
    const playerCount = Number(config.playerCount ?? 2);
    if (!Number.isInteger(playerCount) || playerCount < 2 || playerCount > 4) throw new Error('Azul requires 2-4 players.');
    const names = Array.isArray(config.names) ? config.names : [];
    const seed = Number(config.seed ?? 1) >>> 0;
    const normalizedNames = Array.from(
      {length:playerCount},
      (_, id) => String(names[id] || `玩家 ${id + 1}`),
    );
    const state = {
      schemaVersion: 1,
      config:{playerCount, seed, names:clone(normalizedNames)},
      seed,
      rngState: seed || 1,
      playerCount,
      turn: 1,
      round: 1,
      currentPlayer: 0,
      phase: 'draft',
      bag: COLORS.flatMap(color => Array(20).fill(color)),
      lid: [], factories: [], center: [],
      firstPlayerTokenAvailable: true,
      nextFirstPlayer: 0,
      players: normalizedNames.map((name, id) => makePlayer(id, name)),
      winner: null, winners: [],
      actionHistory: [],
    };
    shuffle(state, state.bag);
    fillFactories(state);
    return state;
  }
  function sourceTiles(state, source) {
    if (!source || !['factory', 'center'].includes(source.kind)) return [];
    if (source.kind === 'center') return state.center;
    return Number.isInteger(source.index) && source.index >= 0 && source.index < state.factories.length
      ? state.factories[source.index] : [];
  }
  function canPlacePattern(state, pid, row, color) {
    if (!Number.isInteger(row) || row < 0 || row > 4) return false;
    const player = state.players[pid];
    const line = player.patternLines[row];
    if (line.length >= row + 1 || (line.length && line[0] !== color)) return false;
    return player.wall[row][WALL_PATTERN[row].indexOf(color)] === null;
  }
  function legalActions(state) {
    if (state.phase !== 'draft') return [];
    const sources = state.factories.map((_, index) => ({kind: 'factory', index})).concat({kind: 'center'});
    const actions = [];
    for (const source of sources) {
      const colors = [...new Set(sourceTiles(state, source))];
      for (const color of colors) {
        for (let row = 0; row < 5; row += 1) {
          if (canPlacePattern(state, state.currentPlayer, row, color)) actions.push({source: clone(source), color, destination: {kind: 'pattern', row}});
        }
        actions.push({source: clone(source), color, destination: {kind: 'floor'}});
      }
    }
    return actions;
  }
  function canonicalJson(value) {
    if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
    if (value && typeof value === 'object') return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(',')}}`;
    return JSON.stringify(value);
  }
  function actionsEqual(left, right) { return canonicalJson(left) === canonicalJson(right); }
  function placeFloor(state, player, tiles) {
    for (const tile of tiles) {
      if (player.floor.length < 7) player.floor.push(tile);
      else if (tile !== 'first-player') state.lid.push(tile);
    }
  }
  function placementScore(player, row, column) {
    let horizontal = 1, vertical = 1;
    for (let col = column - 1; col >= 0 && player.wall[row][col] !== null; col -= 1) horizontal += 1;
    for (let col = column + 1; col < 5 && player.wall[row][col] !== null; col += 1) horizontal += 1;
    for (let line = row - 1; line >= 0 && player.wall[line][column] !== null; line -= 1) vertical += 1;
    for (let line = row + 1; line < 5 && player.wall[line][column] !== null; line += 1) vertical += 1;
    if (horizontal === 1 && vertical === 1) return 1;
    if (horizontal === 1) return vertical;
    if (vertical === 1) return horizontal;
    return horizontal + vertical;
  }
  function completeRows(player) { return player.wall.filter(row => row.every(tile => tile !== null)).length; }
  function finalBonus(player) {
    const breakdown = finalBonusBreakdown(player);
    return breakdown.rows + breakdown.columns + breakdown.colorSets;
  }
  function finalBonusBreakdown(player) {
    const completedRows = completeRows(player);
    let completedColumns = 0;
    for (let col = 0; col < 5; col += 1) {
      if (player.wall.every(row => row[col] !== null)) completedColumns += 1;
    }
    const completedColorSets = COLORS.filter(
      color => player.wall.flat().filter(tile => tile === color).length === 5,
    ).length;
    return {
      completedRows,
      completedColumns,
      completedColorSets,
      rows:completedRows * 2,
      columns:completedColumns * 7,
      colorSets:completedColorSets * 10,
    };
  }
  function finalResult(state) {
    if (state.phase !== 'finished') return null;
    const details = state.players.map(player => {
      const bonus = finalBonusBreakdown(player);
      const finalBonuses = bonus.rows + bonus.columns + bonus.colorSets;
      return {
        seat:player.id,
        total:player.score,
        completedRows:bonus.completedRows,
        components:[
          {id:'round-scoring', label:'Round scoring', value:player.score - finalBonuses, formula:'accumulated wall placement points and floor penalties before final bonuses'},
          {id:'completed-rows', label:'Completed rows', value:bonus.rows, formula:`2 points × ${bonus.completedRows} completed horizontal rows`},
          {id:'completed-columns', label:'Completed columns', value:bonus.columns, formula:`7 points × ${bonus.completedColumns} completed vertical columns`},
          {id:'completed-color-sets', label:'Completed color sets', value:bonus.colorSets, formula:`10 points × ${bonus.completedColorSets} completed color sets`},
        ],
      };
    });
    const bestScore = Math.max(...details.map(player => player.total));
    const scoreLeaders = details.filter(player => player.total === bestScore);
    const bestRows = Math.max(...scoreLeaders.map(player => player.completedRows));
    const winners = scoreLeaders.filter(player => player.completedRows === bestRows).map(player => player.seat);
    return {
      schemaVersion:1,
      winner:winners.length === 1 ? winners[0] : null,
      winners,
      tieBreakers:[
        {
          id:'total-score',
          label:'Total score',
          values:details.map(player => player.total),
          winner:scoreLeaders.length === 1 ? scoreLeaders[0].seat : null,
        },
        {
          id:'completed-rows',
          label:'Most completed horizontal rows',
          values:details.map(player => player.completedRows),
          winner:scoreLeaders.length > 1 && winners.length === 1 ? winners[0] : null,
        },
      ],
      players:details.map(({seat, total, components}) => ({seat, total, components})),
    };
  }
  function resolveRound(state, events) {
    for (const player of state.players) {
      let delta = 0;
      for (let row = 0; row < 5; row += 1) {
        const line = player.patternLines[row];
        if (line.length !== row + 1) continue;
        const color = line[0];
        const column = WALL_PATTERN[row].indexOf(color);
        player.wall[row][column] = color;
        const points = placementScore(player, row, column);
        delta += points;
        state.lid.push(...line.slice(1));
        player.patternLines[row] = [];
        events.push({type: 'WallTilePlaced', player: player.id, row, column, color, points});
      }
      player.floor.forEach((_, index) => { delta += FLOOR_PENALTIES[index]; });
      state.lid.push(...player.floor.filter(tile => tile !== 'first-player'));
      player.floor = [];
      const scoreBefore = player.score;
      player.score = Math.max(0, scoreBefore + delta);
      events.push({type: 'RoundScored', player: player.id, delta: player.score - scoreBefore, score: player.score});
    }
    if (state.players.some(player => completeRows(player) > 0)) {
      for (const player of state.players) {
        const bonus = finalBonus(player);
        player.score += bonus;
        events.push({type: 'FinalBonus', player: player.id, bonus, score: player.score});
      }
      state.phase = 'finished';
      const ranked = [...state.players].sort((a, b) => b.score - a.score || completeRows(b) - completeRows(a) || a.id - b.id);
      const bestScore = ranked[0].score, bestRows = completeRows(ranked[0]);
      state.winners = ranked.filter(player => player.score === bestScore && completeRows(player) === bestRows).map(player => player.id);
      state.winner = state.winners.length === 1 ? state.winners[0] : null;
      events.push({type: 'GameFinished', winner: state.winner, winners: clone(state.winners)});
      return;
    }
    state.round += 1;
    state.currentPlayer = state.nextFirstPlayer;
    state.firstPlayerTokenAvailable = true;
    for (const player of state.players) player.hasFirstPlayerMarker = false;
    fillFactories(state);
    events.push({type: 'RoundStarted', round: state.round, currentPlayer: state.currentPlayer});
  }
  function applyAction(state, action) {
    if (!legalActions(state).some(candidate => actionsEqual(candidate, action))) throw new Error(`Illegal action: ${JSON.stringify(action)}`);
    const next = clone(state);
    const events = [];
    const tiles = sourceTiles(next, action.source);
    const taken = tiles.filter(tile => tile === action.color);
    if (action.source.kind === 'factory') {
      next.center.push(...tiles.filter(tile => tile !== action.color));
      next.factories[action.source.index] = [];
    } else {
      next.center = next.center.filter(tile => tile !== action.color);
      if (next.firstPlayerTokenAvailable) {
        for (const candidate of next.players) candidate.hasFirstPlayerMarker = false;
        next.players[next.currentPlayer].hasFirstPlayerMarker = true;
        placeFloor(next, next.players[next.currentPlayer], ['first-player']);
        next.firstPlayerTokenAvailable = false;
        next.nextFirstPlayer = next.currentPlayer;
      }
    }
    const player = next.players[next.currentPlayer];
    if (action.destination.kind === 'pattern') {
      const row = action.destination.row;
      const space = row + 1 - player.patternLines[row].length;
      player.patternLines[row].push(...taken.slice(0, space));
      placeFloor(next, player, taken.slice(space));
    } else placeFloor(next, player, taken);
    events.push({type: 'TilesTaken', player: next.currentPlayer, source: clone(action.source), color: action.color, count: taken.length, destination: clone(action.destination)});
    next.actionHistory.push(clone(action));
    next.turn += 1;
    const empty = next.factories.every(factory => factory.length === 0) && next.center.length === 0;
    if (empty) resolveRound(next, events);
    else next.currentPlayer = (next.currentPlayer + 1) % next.playerCount;
    return {state: next, events};
  }
  function snapshot(state) { return clone(state); }
  function migrateLegacyV1(value) {
    const state = clone(value);
    if (!state || state.schemaVersion !== 1) return state;
    const owns = (target, key) => Object.prototype.hasOwnProperty.call(target, key);
    if (!owns(state, 'config')) {
      const names = Array.isArray(state.players)
        ? state.players.map(player => player && player.name)
        : [];
      if (
        Number.isInteger(state.playerCount)
        && Number.isInteger(state.seed)
        && names.length === state.playerCount
        && names.every(name => typeof name === 'string' && name)
      ) {
        state.config = {
          playerCount:state.playerCount,
          seed:state.seed,
          names:clone(names),
        };
      }
    }
    if (Array.isArray(state.players)) {
      const markerFields = state.players.map(player => (
        player && owns(player, 'hasFirstPlayerMarker')
      ));
      if (markerFields.every(present => !present)) {
        const floorOwners = state.players.flatMap((player, seat) => (
          Array.isArray(player.floor) && player.floor.includes('first-player')
            ? [seat]
            : []
        ));
        let owner = null;
        if (state.firstPlayerTokenAvailable === true && floorOwners.length === 0) {
          owner = null;
        } else if (
          state.firstPlayerTokenAvailable === false
          && floorOwners.length === 1
          && floorOwners[0] === state.nextFirstPlayer
        ) {
          owner = floorOwners[0];
        } else if (
          state.firstPlayerTokenAvailable === false
          && floorOwners.length === 0
          && Number.isInteger(state.nextFirstPlayer)
          && (
            state.phase === 'finished'
            || state.players[state.nextFirstPlayer]?.floor?.length === 7
          )
        ) {
          owner = state.nextFirstPlayer;
        } else return state;
        for (const [seat, player] of state.players.entries()) {
          player.hasFirstPlayerMarker = seat === owner;
        }
      }
    }
    return state;
  }
  function validateSnapshot(state) {
    if (!state || state.schemaVersion !== 1 || !Number.isInteger(state.playerCount) || state.playerCount < 2 || state.playerCount > 4) return false;
    if (!state.config || state.config.playerCount !== state.playerCount || state.config.seed !== state.seed) return false;
    if (!Array.isArray(state.config.names) || state.config.names.length !== state.playerCount || state.config.names.some(name => typeof name !== 'string' || !name)) return false;
    if (!Number.isInteger(state.seed) || state.seed < 0 || state.seed > 0xffffffff || !Number.isInteger(state.rngState) || state.rngState < 1 || state.rngState > 0xffffffff) return false;
    if (!Array.isArray(state.actionHistory) || state.actionHistory.length > 2000 || !['draft', 'finished'].includes(state.phase)) return false;
    if (!Number.isInteger(state.currentPlayer) || state.currentPlayer < 0 || state.currentPlayer >= state.playerCount) return false;
    if (!Number.isInteger(state.nextFirstPlayer) || state.nextFirstPlayer < 0 || state.nextFirstPlayer >= state.playerCount) return false;
    if (!Number.isInteger(state.turn) || state.turn !== state.actionHistory.length + 1 || !Number.isInteger(state.round) || state.round < 1 || state.round > state.turn || typeof state.firstPlayerTokenAvailable !== 'boolean') return false;
    if (!Array.isArray(state.bag) || !Array.isArray(state.lid) || !Array.isArray(state.center)) return false;
    if (!Array.isArray(state.factories) || state.factories.length !== state.playerCount * 2 + 1 || state.factories.some(factory => !Array.isArray(factory) || factory.length > 4)) return false;
    if (!Array.isArray(state.players) || state.players.length !== state.playerCount) return false;

    const coloredTiles = [...state.bag, ...state.lid, ...state.center, ...state.factories.flat()];
    let firstPlayerMarkers = 0;
    let firstPlayerMarkerSeat = null;
    const markerOwners = [];
    for (const [seat, player] of state.players.entries()) {
      if (!player || player.id !== seat || player.name !== state.config.names[seat] || !Number.isInteger(player.score) || player.score < 0 || typeof player.hasFirstPlayerMarker !== 'boolean') return false;
      if (player.hasFirstPlayerMarker) markerOwners.push(seat);
      if (!Array.isArray(player.patternLines) || player.patternLines.length !== 5 || !Array.isArray(player.floor) || player.floor.length > 7) return false;
      if (!Array.isArray(player.wall) || player.wall.length !== 5 || player.wall.some(row => !Array.isArray(row) || row.length !== 5)) return false;
      for (let row = 0; row < 5; row += 1) {
        const line = player.patternLines[row];
        if (!Array.isArray(line) || line.length > row + 1 || line.some(tile => !COLORS.includes(tile))) return false;
        if (line.length && (line.some(tile => tile !== line[0]) || player.wall[row][WALL_PATTERN[row].indexOf(line[0])] !== null)) return false;
        for (let column = 0; column < 5; column += 1) {
          const tile = player.wall[row][column];
          if (tile !== null && tile !== WALL_PATTERN[row][column]) return false;
          if (tile !== null) coloredTiles.push(tile);
        }
        coloredTiles.push(...line);
      }
      for (const tile of player.floor) {
        if (tile === 'first-player') {
          firstPlayerMarkers += 1;
          firstPlayerMarkerSeat = seat;
        }
        else coloredTiles.push(tile);
      }
    }
    if (firstPlayerMarkers > 1) return false;
    if (state.firstPlayerTokenAvailable) {
      if (firstPlayerMarkers !== 0 || markerOwners.length !== 0) return false;
    } else {
      if (markerOwners.length !== 1 || markerOwners[0] !== state.nextFirstPlayer) return false;
      if (firstPlayerMarkers === 1 && firstPlayerMarkerSeat !== markerOwners[0]) return false;
      if (state.phase === 'draft' && firstPlayerMarkers === 0 && state.players[markerOwners[0]].floor.length !== 7) return false;
      if (state.phase === 'finished' && firstPlayerMarkers !== 0) return false;
    }
    if (coloredTiles.some(tile => !COLORS.includes(tile))) return false;
    if (!COLORS.every(color => coloredTiles.filter(tile => tile === color).length === 20)) return false;
    if (!Array.isArray(state.winners) || state.winners.some(seat => !Number.isInteger(seat) || seat < 0 || seat >= state.playerCount)) return false;
    if (state.phase === 'finished') {
      if (state.factories.some(factory => factory.length !== 0) || state.center.length !== 0 || state.players.some(player => player.floor.length !== 0)) return false;
      if (!state.players.some(player => completeRows(player) > 0)) return false;
      const ranked = [...state.players].sort((a, b) => b.score - a.score || completeRows(b) - completeRows(a) || a.id - b.id);
      const expected = ranked.filter(player => player.score === ranked[0].score && completeRows(player) === completeRows(ranked[0])).map(player => player.id);
      if (canonicalJson(expected) !== canonicalJson(state.winners) || state.winner !== (expected.length === 1 ? expected[0] : null)) return false;
    } else if (state.winner !== null || state.winners.length !== 0 || legalActions(state).length === 0) return false;
    return true;
  }
  function restore(value) {
    const state = migrateLegacyV1(value);
    if (!validateSnapshot(state)) throw new Error('Unsupported or invalid Azul snapshot.');
    let rebuilt;
    try {
      rebuilt = replay(state.config, state.actionHistory);
    } catch (_error) {
      throw new Error('Unsupported or invalid Azul snapshot.');
    }
    if (canonicalJson(rebuilt) !== canonicalJson(state)) throw new Error('Unsupported or invalid Azul snapshot.');
    return rebuilt;
  }
  function replay(config, actions) {
    let state = createGame(config);
    for (const action of actions) state = applyAction(state, action).state;
    return state;
  }

  return {COLORS, FLOOR_PENALTIES, WALL_PATTERN, createGame, legalActions, canPlacePattern, applyAction, placementScore, finalResult, snapshot, restore, replay};
});
