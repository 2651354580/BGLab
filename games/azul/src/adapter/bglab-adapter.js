(function (root, factory) {
  const core = typeof module === 'object' && module.exports ? require('../core/azul-engine.js') : root.BGLabAzulCore;
  const decisionExplorer = typeof module === 'object' && module.exports
    ? require('../../../_sdk/decision-explorer/index.cjs')
    : root.BGLabDecisionExplorer;
  const scoringFrame = typeof module === 'object' && module.exports
    ? require('./scoring-frame.js')
    : root.BGLabAzulScoringFrame;
  const actionFrame = typeof module === 'object' && module.exports
    ? require('./action-frame.js')
    : root.BGLabAzulActionFrame;
  const publicOutcome = typeof module === 'object' && module.exports
    ? require('./public-outcome.js') : root.BGLabAzulPublicOutcome;
  const api = factory(core, decisionExplorer, scoringFrame, actionFrame, publicOutcome);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && core) root.BGLabGameAdapter = new api.AzulBGLabAdapter();
})(typeof globalThis !== 'undefined' ? globalThis : this, function (core, decisionExplorer, scoringFrame, actionFrame, publicOutcome) {
  'use strict';
  if (!core) throw new Error('Azul core must load before its Adapter.');
  if (!scoringFrame) throw new Error('Azul scoring Frame must load before its Adapter.');
  if (!actionFrame) throw new Error('Azul action Frame must load before its Adapter.');
  const clone = value => structuredClone(value);
  const same = (left, right) => {
    if (left === right) return true;
    if (typeof left !== typeof right || left === null || right === null) return false;
    if (Array.isArray(left) || Array.isArray(right)) {
      if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length) return false;
      return left.every((value, index) => same(value, right[index]));
    }
    if (typeof left !== 'object') return false;
    const leftKeys = Object.keys(left);
    const rightKeys = Object.keys(right);
    if (leftKeys.length !== rightKeys.length) return false;
    return leftKeys.every(key => (
      Object.prototype.hasOwnProperty.call(right, key) && same(left[key], right[key])
    ));
  };
  const localCanonicalFingerprint = value => {
    if (Array.isArray(value)) return `[${value.map(localCanonicalFingerprint).join(',')}]`;
    if (value && typeof value === 'object') {
      return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${localCanonicalFingerprint(value[key])}`).join(',')}}`;
    }
    return JSON.stringify(value);
  };
  const transactionFingerprint = value => decisionExplorer?.canonicalFingerprint
    ? decisionExplorer.canonicalFingerprint(value)
    : localCanonicalFingerprint(value);

  class AzulBGLabAdapter {
    constructor(config = {}) {
      this.protocolVersion = 2;
      this.snapshotVersion = 1;
      this.state = core.createGame(config);
      this.committed = {};
      this.listeners = new Set();
      this._authorityGateway = null;
    }
    start(config = {}) {
      this.state = core.createGame(config);
      this.committed = {};
      this._authorityGateway?.invalidate();
      this._publish([]);
      return this.view(Number(config.seat || 0));
    }
    decisionId() { return `azul:${this.state.turn}:${this.state.currentPlayer}`; }
    coverageCatalog() {
      return {
        actionFamilies:['select_source','select_color','place_tiles'],
        effectFamilies:['TilesTaken','WallTilePlaced','RoundScored','FinalBonus','GameFinished','RoundStarted'],
        boundaryReasons:['turn_passed','game_finished'],
      };
    }
    strategicOpportunityCatalog() { return []; }
    _sourceTiles(source, state = this.state) {
      return source.kind === 'center' ? state.center : (state.factories[source.index] || []);
    }
    nextOperations(prefix = [], state = this.state) {
      const steps = prefix.filter(step => step.op !== 'begin');
      if (steps.length === 0) {
        const factories = state.factories.flatMap((tiles, index) => tiles.length ? [{op:'select_source', source:'factory', factoryIndex:index}] : []);
        return [...factories, ...(state.center.length ? [{op:'select_source', source:'center'}] : [])];
      }
      const sourceStep = steps[0];
      const source = sourceStep.source === 'factory' ? {kind:'factory', index:sourceStep.factoryIndex} : {kind:'center'};
      if (steps.length === 1) return [...new Set(this._sourceTiles(source, state))].map(color => ({op:'select_color', color}));
      const color = steps[1].color;
      if (steps.length === 2) {
        const pattern = Array.from({length:5}, (_, row) => row).flatMap(row => core.canPlacePattern(state, state.currentPlayer, row, color) ? [{op:'place_tiles', destination:'pattern', row}] : []);
        return [...pattern, {op:'place_tiles', destination:'floor'}];
      }
      return [];
    }
    _publicState() {
      return {
        round:this.state.round, turn:this.state.turn,
        currentPlayer:this.state.currentPlayer,
        factories:clone(this.state.factories), center:clone(this.state.center),
        firstPlayerTokenAvailable:this.state.firstPlayerTokenAvailable,
        bagCount:this.state.bag.length, lidCount:this.state.lid.length,
        players:this.state.players.map(player => ({
          id:player.id, name:player.name, score:player.score,
          patternLines:clone(player.patternLines), wall:clone(player.wall),
          floor:clone(player.floor),
          hasFirstPlayerMarker:player.hasFirstPlayerMarker,
        })),
      };
    }
    _analysis() {
      const player = this.state.players[this.state.currentPlayer];
      return core.legalActions(this.state).map(action => {
        const sourceTiles = this._sourceTiles(action.source);
        const count = sourceTiles.filter(tile => tile === action.color).length;
        const row = action.destination.kind === 'pattern' ? action.destination.row : null;
        const remaining = row === null ? 0 : row + 1 - player.patternLines[row].length;
        const fits = row === null ? 0 : Math.min(count, remaining);
        const sourceStep = action.source.kind === 'factory'
          ? {op:'select_source',source:'factory',factoryIndex:action.source.index}
          : {op:'select_source',source:'center'};
        const destinationStep = row === null
          ? {op:'place_tiles',destination:'floor'}
          : {op:'place_tiles',destination:'pattern',row};
        const pushedTiles = action.source.kind === 'factory'
          ? sourceTiles.filter(tile => tile !== action.color) : [];
        const pushedColors = pushedTiles.reduce((totals, color) => {
          totals[color] = (totals[color] || 0) + 1;
          return totals;
        }, {});
        const takesFirstPlayer = action.source.kind === 'center' && this.state.firstPlayerTokenAvailable;
        const floorIncoming = (row === null ? count : count - fits) + (takesFirstPlayer ? 1 : 0);
        const floorAfterCount = Math.min(7, player.floor.length + floorIncoming);
        const floorPenaltyAfter = core.FLOOR_PENALTIES
          .slice(0, floorAfterCount).reduce((total, penalty) => total + penalty, 0);
        const transition = core.applyAction(this.state, action);
        const roundScore = transition.events.find(event =>
          event.type === 'RoundScored' && event.player === player.id
        );
        const endsRound = Boolean(roundScore);
        const remainingSourcesAfter = endsRound ? 0 :
          transition.state.factories.filter(factory => factory.length).length +
          (transition.state.center.length ? 1 : 0);
        return {
          source:action.source.kind,
          ...(action.source.kind === 'factory' ? {factoryIndex:action.source.index} : {}),
          color:action.color, destination:action.destination.kind,
          ...(row === null ? {} : {row}),
          count, fits, overflow:count-fits,
          remainingSlotsBefore:row === null ? null : remaining,
          patternColorAfter:row === null ? null : action.color,
          wallColumn:row === null ? null : core.WALL_PATTERN[row].indexOf(action.color),
          remainingSlotsAfter:row === null ? null : Math.max(0, remaining-fits),
          completesLine:row !== null && player.patternLines[row].length + fits === row + 1,
          takesFirstPlayer,
          pushedToCenter:pushedTiles.length, pushedColors,
          floorAdded:Math.min(floorIncoming, Math.max(0, 7-player.floor.length)),
          floorPenaltyAfter, remainingSourcesAfter, endsRound,
          ...(roundScore ? {roundScoreDelta:roundScore.delta} : {}),
          transaction:{steps:[
            {op:'begin',action:'turn'}, sourceStep,
            {op:'select_color',color:action.color}, destinationStep,
          ]},
        };
      });
    }
    renderPublicOutcome(outcome) { return publicOutcome.renderPublicOutcome(outcome); }
    _outcome(before, after, action, events) {
      const pid = before.currentPlayer;
      const was = before.players[pid];
      const now = after.players[pid];
      const count = this._sourceTiles(action.source, before).filter(tile => tile === action.color).length;
      const row = action.destination.kind === 'pattern' ? action.destination.row : null;
      const patternPlacedTileCount = row === null
        ? 0
        : Math.min(count, Math.max(0, row + 1 - was.patternLines[row].length));
      const selectedTilesToFloorCount = count - patternPlacedTileCount;
      const completedLine = row !== null && was.patternLines[row].length + count >= row + 1;
      const roundScore = events.find(event => event.type === 'RoundScored' && event.player === pid);
      const takesFirstPlayer = action.source.kind === 'center' && before.firstPlayerTokenAvailable;
      const floorIncoming = selectedTilesToFloorCount + (takesFirstPlayer ? 1 : 0);
      const floorDelta = Math.min(floorIncoming, Math.max(0, 7 - was.floor.length));
      const patternLineAfter = row === null ? null : {
        row,
        color:was.patternLines[row][0] ?? action.color,
        filled:was.patternLines[row].length + patternPlacedTileCount,
        capacity:row + 1,
        remainingSlots:Math.max(0, row + 1 - was.patternLines[row].length - patternPlacedTileCount),
        complete:was.patternLines[row].length + patternPlacedTileCount === row + 1,
      };
      return {
        scoreDelta: now.score - was.score,
        scoreAfter: now.score,
        selectedColor: action.color,
        selectedTileCount: count,
        patternPlacedTileCount,
        selectedTilesToFloorCount,
        ...(row === null ? {destination:'floor'} : {
          destination:'pattern', row, completedPatternLine:completedLine,
          patternLineAfter,
        }),
        // Count floor slots occupied by this draft before any round-end cleanup.
        floorDelta,
        endsRound: Boolean(roundScore),
        ...(roundScore ? {roundScoreDelta:roundScore.delta} : {}),
        gameFinished: after.phase === 'finished',
        nextPlayer: after.currentPlayer,
        phase: after.phase,
        remaining:{
          bagCount:after.bag.length,
          lidCount:after.lid.length,
          centerCount:after.center.length,
          floorCount:now.floor.length,
        },
      };
    }
    *_turnPrograms(seat = this.state.currentPlayer) {
      const before = this.state;
      for (const action of core.legalActions(before)) {
        const transition = core.applyAction(before, action);
        const source = action.source.kind === 'factory'
          ? {op:'select_source',source:'factory',factoryIndex:action.source.index}
          : {op:'select_source',source:'center'};
        const destination = action.destination.kind === 'pattern'
          ? {op:'place_tiles',destination:'pattern',row:action.destination.row}
          : {op:'place_tiles',destination:'floor'};
        const steps = [{op:'begin',action:'turn'}, source, {op:'select_color',color:action.color}, destination];
        yield {
          rootKey:source,
          rootAction:source,
          steps,
          causalTrace: steps.map((step, stepIndex) => ({
            step:stepIndex,
            source:stepIndex === 0 ? 'turn' : step.op,
            effect:stepIndex === steps.length - 1 ? 'complete turn' : 'select legal action component',
          })),
          outcome:this._outcome(before, transition.state, action, transition.events),
          terminalKey:JSON.stringify(core.snapshot(transition.state)),
          boundaryReason:transition.state.phase === 'finished' ? 'game_finished' : 'turn_passed',
          termination:'automatic',
        };
      }
    }
    _finiteExplorer(seat = this.state.currentPlayer) {
      const decisionId = this.decisionId();
      if (!decisionExplorer?.createFiniteProgramExplorer) throw new Error('AUTHORITY_WORKER_REQUIRED');
      return decisionExplorer.createFiniteProgramExplorer({
        decisionId,
        snapshot:this.snapshot(),
        pageSize:12,
        programs:() => [...this._turnPrograms(seat)],
      });
    }
    _authorityGatewayFor() {
      if (this._authorityGateway) return this._authorityGateway;
      if (!decisionExplorer?.createAuthorityGateway) throw new Error('AUTHORITY_WORKER_REQUIRED');
      this._authorityGateway = decisionExplorer.createAuthorityGateway({
        currentIdentity:() => ({decisionId:this.decisionId(), seat:this.state.currentPlayer}),
        finiteExplorerForSeat:(seat) => this._finiteExplorer(seat),
        programsForSeat:(seat) => [...this._turnPrograms(seat)],
        decisionMapPageSize:20,
      });
      return this._authorityGateway;
    }
    outcomeIndex(seat = this.state.currentPlayer, _request = {}) {
      return {...this._authorityGatewayFor().outcomeIndex(_request), seat};
    }
    enumerateRoutes(request = {}) {
      return this._authorityGatewayFor().enumerateRoutes(request);
    }
    view(seat = this.state.currentPlayer) {
      const publicState = this._publicState();
      const nextActions = this.nextOperations([]);
      const analysis = this._analysis();
      const player = this.state.players[seat];
      const {
        scoreFacts,
        roundEndHorizon,
        currentProgress,
        floorPenaltyBefore,
        scoringTargets,
        scoringDecisionFacts,
      } = scoringFrame.buildScoringDecisionFacts({
        core,
        state:this.state,
        seat,
        analysis,
      });
      const groupedTiles = tiles => {
        const counts = tiles.reduce((result, color) => {
          result[color] = (result[color] || 0) + 1;
          return result;
        }, {});
        const line = core.COLORS
          .filter(color => counts[color])
          .map(color => `${color}:${counts[color]}`)
          .join(',');
        return line || 'empty';
      };
      const wallProgressLine = wallProgress => (
        `rows=${wallProgress.rows.filter(item => item.filled === item.required).length}/5; `
        + `columns=${wallProgress.columns.filter(item => item.filled === item.required).length}/5; `
        + `colorSets=${wallProgress.colors.filter(item => item.filled === item.required).length}/5; `
        + `rowFill=[${wallProgress.rows.map(item => `${item.id}:${item.filled}/5`).join(',')}]; `
        + `columnFill=[${wallProgress.columns.map(item => `${item.id}:${item.filled}/5`).join(',')}]; `
        + `colorFill=[${wallProgress.colors.map(item => `${item.id}:${item.filled}/5`).join(',')}]`
      );
      const patternLines = player.patternLines.map((line, row) => {
        const acceptsTilesNow = line.length < row + 1;
        const currentColor = line[0] || null;
        const wallColors = (currentColor ? [currentColor] : core.COLORS)
          .filter(color => {
            const column = core.WALL_PATTERN[row].indexOf(color);
            return column >= 0 && player.wall[row][column] === null;
          });
        const destinations = wallColors.map(color => {
            const column = core.WALL_PATTERN[row].indexOf(color);
            return (
              `${color}->wall[${row},${column}],occupied=no,`
            + `scoreOnlyAfterPatternFullAndRoundEnds=${core.placementScore(player, row, column)}`
          );
        }).join(' | ');
        return (
          `row=${row}; capacity=${row + 1}; currentColor=${currentColor || 'empty'}; `
          + `filled=${line.length}; remainingSlots=${row + 1 - line.length}; `
          + `acceptsTilesNow=${acceptsTilesNow ? 'yes' : 'no'}; `
          + `eligibleColorsNow=${acceptsTilesNow ? wallColors.join(',') || 'none' : 'none'}; `
          + `wallDestinationsAtRoundEnd=${destinations || 'none'}`
        );
      });
      const sourceConsequenceLines = [...new Map(analysis.map(item => {
        const sourceId = item.source === 'factory'
          ? `factory-${item.factoryIndex}`
          : 'center';
        const key = `${sourceId}:${item.color}`;
        return [key, (
          `source=${sourceId}; color=${item.color}; take=${item.count}; `
          + `otherTilesMoveToCenter=${item.pushedToCenter}; `
          + `movedColors=${groupedTiles(Object.entries(item.pushedColors).flatMap(([color, count]) => Array(count).fill(color)))}; `
          + `takesFirstPlayerToken=${item.takesFirstPlayer ? 'yes' : 'no'}`
        )];
      })).values()];
      const tileCounts = tiles => Object.fromEntries(
        core.COLORS
          .map(color => [color, tiles.filter(tile => tile === color).length])
          .filter(([, count]) => count > 0),
      );
      // Round settlement runs top to bottom. Already-full earlier lines are
      // guaranteed to join the wall before this row, regardless of later drafts.
      const settlementPlayer = {...player, wall:clone(player.wall)};
      const patternLineFacts = player.patternLines.map((line, row) => {
        const capacity = row + 1;
        const acceptsTilesNow = line.length < capacity;
        const currentColor = line[0] || null;
        const legalColors = (currentColor ? [currentColor] : core.COLORS)
          .filter(color => {
            const column = core.WALL_PATTERN[row].indexOf(color);
            return column >= 0 && player.wall[row][column] === null;
          });
        const facts = {
          id:`pattern-row-${row}`,
          placeAction:{action:'place_tiles', destination:'pattern', row},
          capacity,
          currentColor,
          filled:line.length,
          remainingSlots:capacity-line.length,
          acceptsTilesNow,
          legalColors:acceptsTilesNow ? legalColors : [],
          wallDestinations:legalColors.map(color => {
            const column = core.WALL_PATTERN[row].indexOf(color);
            return {
              color,
              column,
              minimumPointsAfterEarlierFullLines:core.placementScore(settlementPlayer, row, column),
            };
          }),
        };
        if (line.length === capacity) {
          settlementPlayer.wall[row][core.WALL_PATTERN[row].indexOf(line[0])] = line[0];
        }
        return facts;
      });
      const floorFact = {
        id:'floor',
        placeAction:{action:'place_tiles', destination:'floor', row:null},
        count:player.floor.length,
        penaltyNow:floorPenaltyBefore,
        penaltyByRemainingPosition:Object.fromEntries(core.FLOOR_PENALTIES
          .slice(player.floor.length)
          .map((penalty, index) => [player.floor.length + index + 1, penalty])),
      };
      const currentDraftOptionsById = new Map();
      for (const item of analysis) {
        const sourceId = item.source === 'factory'
          ? `factory-${item.factoryIndex}`
          : 'center';
        const optionId = `${sourceId}:${item.color}`;
        const targetRef = item.destination === 'pattern'
          ? `pattern-row-${item.row}`
          : 'floor';
        let option = currentDraftOptionsById.get(optionId);
        if (!option) {
          option = {
            id:optionId,
            draftAction:{
              action:'draft_tiles',
              source:item.source,
              source_index:item.source === 'factory' ? item.factoryIndex : null,
              color:item.color,
            },
            selectedCount:item.count,
            sourceAfter:{
              sourceEmpty:item.source === 'factory'
                ? true
                : this.state.center.length === item.count,
              selectedColorRemaining:0,
              endsRound:item.endsRound,
              movedToCenter:clone(item.pushedColors),
              ...(item.source === 'center' ? {
                remainingColors:tileCounts(
                  this.state.center.filter(tile => tile !== item.color),
                ),
                remainingCount:this.state.center.length-item.count,
              } : {}),
            },
            takesFirstPlayerToken:item.takesFirstPlayer,
            compatibleDestinationRefs:[],
          };
          currentDraftOptionsById.set(optionId, option);
        }
        if (!option.compatibleDestinationRefs.includes(targetRef)) {
          option.compatibleDestinationRefs.push(targetRef);
        }
      }
      const currentActionFacts = actionFrame.buildActionFacts(
        analysis,
        [...currentDraftOptionsById.values()],
        floorPenaltyBefore,
      );
      const modelFacts = {
        version:1,
        coverage:'complete-current-decision',
        facts:[
          {
            kind:'TurnFact', id:'turn', title:'Turn', data:{
              round:this.state.round,
              turn:this.state.turn,
              phase:this.state.phase,
              actorSeat:seat,
              playerCount:this.state.playerCount,
              firstPlayerTokenAvailable:publicState.firstPlayerTokenAvailable,
              hasFirstPlayerMarker:publicState.players[seat].hasFirstPlayerMarker,
              roundEndHorizon,
            },
          },
          {
            kind:'ResourceSnapshot', id:'sources', title:'Tile sources', data:{
              sources:[
                ...this.state.factories
                  .map((tiles, sourceIndex) => ({
                    source:'factory',
                    source_index:sourceIndex,
                    colors:tileCounts(tiles),
                  }))
                  .filter(source => Object.keys(source.colors).length > 0),
                ...(this.state.center.length ? [{
                  source:'center',
                  source_index:null,
                  colors:tileCounts(this.state.center),
                }] : []),
              ],
            },
          },
          {
            kind:'CurrentTargetFact', id:'route-legality-summary', title:'Route constraints', data:{
              choiceRule:'Choose one listed take and one destination listed under that same option; never mix options. acceptsTilesNow=false is unavailable. Copy row=N from pattern-row-N: row=0..4 maps to capacities 1..5.',
              boardOwnershipRule:"Targets and each row's occupiedWallCellsInThisRow belong to actorSeat. Opponents have independent boards; each opponents entry shows that public board and never blocks yours.",
              coordinateRule:'Use zero-based coordinates row=N and column=N; do not renumber as ordinals. Opponent wall[row][column] is the tile color, or null for empty. Pattern row=0..4 has capacity 1..5.',
              timingRule:'roundEndsAfterAction is the whole round boundary. full_waits_round_end: full line, no wall tile or score yet. full_scores_at_round_end: one tile moves to wall and scores now. incomplete_carries_over: no score; line persists. no_scoring_yet: round continues. floor_then_round_end: floor placement then round settlement. A full pattern line is not a completed wall row.',
              conditionalWallScoreRule:'wallScoringIfFullAtRoundEndByColor gives the wall cell and its minimum adjacency points only if this pattern line is full at round end. The value already includes all earlier pattern lines that are full now, resolved top to bottom; do not add them again. Earlier lines that fill on future actions can increase these points. An incomplete line places no wall tile and scores no points. These are not immediate points, round net score or end-game bonuses.',
              colorLockRule:'An incomplete pattern line persists across rounds, locked to currentColor (patternColorAfter after placement) until it resolves.',
              sourceRule:'Take all tiles of the chosen color from exactly one source. Factory leftovers move to center. From center, all other colors stay. lastRemainingColorGroup=true ends the round; colorGroupCount>0 means later actions remain.',
              floorPenaltyRule:'floorLossPoints counts points lost, as nonnegative amounts: alreadyOnFloor before this choice + addedByThisChoice = totalAtRoundEnd. Do not add the current floor penalty a second time.',
              centerFirstPlayerTokenRule:'A center draft with firstPlayerTokenAvailable=true adds that token to floor and its penalty; when it is false, you cannot gain it again.',
            },
          },
          ...patternLineFacts.map(line => ({
            kind:'CurrentTargetFact',
            id:line.id,
            title:`Your row=${line.placeAction.row}`,
            data:{
              row:line.placeAction.row,
              currentColor:line.currentColor,
              filled:line.filled,
              capacity:line.capacity,
              remainingSlots:line.remainingSlots,
              acceptsTilesNow:line.acceptsTilesNow,
              legalColors:line.legalColors,
              wallColumnByColor:Object.fromEntries(line.wallDestinations.map(
                item => [item.color, item.column],
              )),
              wallScoringIfFullAtRoundEndByColor:Object.fromEntries(line.wallDestinations.map(
                item => [item.color, {
                  wallRow:line.placeAction.row,
                  wallColumn:item.column,
                  minimumPointsAfterEarlierFullLines:item.minimumPointsAfterEarlierFullLines,
                }],
              )),
              occupiedWallCellsInThisRow:player.wall[line.placeAction.row].flatMap((color, column) => (
                color === null ? [] : [{row:line.placeAction.row, column, color}]
              )),
              incompleteLinePersistsAcrossRounds:true,
            },
          })),
          {
            kind:'CurrentTargetFact', id:'floor', title:'Floor', data:floorFact,
          },
          ...currentActionFacts,
          {
            kind:'DynamicScoreFact', id:'scoring', title:'Score', data:{
              currentTotal:player.score,
              floorPenaltyIfRoundEndedNow:floorPenaltyBefore,
              completedHorizontalWallRows:currentProgress.rows.filter(
                row => row.filled === row.required,
              ).length,
              wallScoreRule:"Pattern-line capacity never determines wall score; full lines have no separate score or end-game bonus. Wall color does not affect adjacency. Horizontal=same row+adjacent columns; vertical=same column+adjacent rows. Cells differing in both are diagonal and disconnected; any gap breaks the chain. Use target row and target.wallColumnByColor[take.color]. Until endsRound=true, exact round score is unknown.",
              opponents:publicState.players
                .filter(candidate => candidate.id !== seat)
                .map(candidate => ({
                  seat:candidate.id,
                  currentTotal:candidate.score,
                  patternLines:candidate.patternLines.map((line, row) => ({
                    row, color:line[0] ?? null, filled:line.length,
                    capacity:row + 1,
                    remainingSlots:row + 1 - line.length,
                    complete:line.length === row + 1,
                  })),
                  wall:candidate.wall,
                  floor:candidate.floor,
                  hasFirstPlayerMarker:candidate.hasFirstPlayerMarker,
                  completedHorizontalWallRows:candidate.wall.filter(
                    row => row.every(Boolean),
                  ).length,
                })),
            },
          },
          {
            kind:'AuthorityBoundaryFact', id:'current-action-boundary', title:'Scope', data:{
              currentDecisionOnly:true,
              projectionEndsAfterCurrentAction:true,
              exactRoundScoreOnlyWhenEndsRoundNow:true,
              placementComputation:{
                gapBefore:'target.remainingSlots for pattern; not applicable to floor',
                fit:'0 for floor; otherwise draft.selectedCount - (destination.overflowToFloor ?? 0)',
                overflow:'draft.selectedCount - fit',
              },
            },
          },
          {
            kind:'UnknownInformationFact', id:'unknown-information', title:'Unknown', data:{
              items:[
                'bag order',
                'lid order',
                'opponent choices after the current action',
                'later draft supply',
              ],
            },
          },
        ],
      };
      const centerColorGroups = new Set(this.state.center).size;
      const factoriesEmpty = this.state.factories.every(factory => factory.length === 0);
      const exactlyOneColorGroupReturnsToActor = (
        centerColorGroups === this.state.players.length + 1
      );
      const floorPenaltyAfterIncoming = (targetPlayer, incoming) => (
        core.FLOOR_PENALTIES
          .slice(0, Math.min(7, targetPlayer.floor.length + incoming))
          .reduce((total, penalty) => total + penalty, 0)
      );
      const forcedNextFloorFacts = item => {
        if (!exactlyOneColorGroupReturnsToActor) return '';
        const currentAction = {
          source:{kind:'center'}, color:item.color,
          destination:item.destination === 'pattern'
            ? {kind:'pattern', row:item.row}
            : {kind:'floor'},
        };
        const afterCurrent = core.applyAction(this.state, currentAction).state;
        const nextPlayerState = afterCurrent.players[player.id];
        const remainingColors = core.COLORS.filter(color => afterCurrent.center.includes(color));
        const outcomes = remainingColors.map(color => {
          const count = afterCurrent.center.filter(tile => tile === color).length;
          const firstPlayerIncoming = afterCurrent.firstPlayerTokenAvailable ? 1 : 0;
          const penalties = [floorPenaltyAfterIncoming(
            nextPlayerState, count + firstPlayerIncoming,
          )];
          for (let row = 0; row < 5; row += 1) {
            if (!core.canPlacePattern(afterCurrent, player.id, row, color)) continue;
            const remaining = row + 1 - nextPlayerState.patternLines[row].length;
            const overflow = Math.max(0, count - remaining);
            penalties.push(floorPenaltyAfterIncoming(
              nextPlayerState, overflow + firstPlayerIncoming,
            ));
          }
          return {color, bestFloorPenalty:Math.max(...penalties)};
        });
        const worst = Math.min(...outcomes.map(outcome => outcome.bestFloorPenalty));
        return (
          `,forcedNextColorBestFloorPenalty=[${outcomes.map(outcome => (
            `${outcome.color}:${outcome.bestFloorPenalty}`
          )).join(',')}],worstForcedNextFloorPenalty=${worst}`
        );
      };
      const centerTurnOrderPressure = factoriesEmpty && centerColorGroups > 1
        ? [
          (
            `factoriesEmpty=yes; centerColorGroups=${centerColorGroups}; players=${this.state.players.length}; `
            + `colorGroupsAfterOneTake=${centerColorGroups - 1}; `
            + `opponentDraftsBeforeActorReturns=${Math.min(this.state.players.length - 1, centerColorGroups - 1)}; `
            + `actorCanReceiveAnotherCenterDraft=${centerColorGroups > this.state.players.length ? 'yes' : 'no'}; `
            + `exactlyOneColorGroupReturnsToActor=${exactlyOneColorGroupReturnsToActor ? 'yes' : 'no'}`
          ),
          ...core.COLORS.filter(color => this.state.center.includes(color)).map(color => {
            const routes = analysis.filter(item => item.source === 'center' && item.color === color);
            return (
              `color=${color}; take=${routes[0].count}; routes=[`
              + routes.map(item => {
                if (item.destination === 'floor') {
                  return (
                    `floor,fit=0,overflow=${item.overflow},currentActionScoreDelta=0,`
                    + `floorPenaltyAfter=${item.floorPenaltyAfter}`
                    + forcedNextFloorFacts(item)
                  );
                }
                const capacity = item.row + 1;
                const resultingFilled = player.patternLines[item.row].length + item.fits;
                return (
                  `row=${item.row},fit=${item.fits},overflow=${item.overflow},`
                  + `resultingFilled=${resultingFilled}/${capacity},`
                  + `lineReadyForRoundEnd=${item.completesLine ? 'yes' : 'no'},`
                  + `currentActionScoreDelta=0,floorPenaltyAfter=${item.floorPenaltyAfter}`
                  + forcedNextFloorFacts(item)
                );
              }).join(' | ')
              + ']'
            );
          }),
        ]
        : null;
      const opponentLines = this.state.players
        .filter(candidate => candidate.id !== seat)
        .map(candidate => {
          const candidateProgress = scoringFrame.wallProgress(core, candidate.wall);
          return (
            `seat=${candidate.id}; score=${candidate.score}; `
            + `patternLines=[${candidate.patternLines.map((line, row) => (
              `row${row}:${line[0] || 'empty'}:${line.length}/${row + 1}`
            )).join(',')}]; floor=${candidate.floor.length}; `
            + wallProgressLine(candidateProgress)
          );
        });
      const nonEmptyFactoryCount = this.state.factories.filter(tiles => tiles.length > 0).length;
      const sourceGroupsBefore = nonEmptyFactoryCount + (this.state.center.length > 0 ? 1 : 0);
      const groupsAfterCurrentRoutes = analysis.map(item => {
        const factoriesAfter = nonEmptyFactoryCount - (item.source === 'factory' ? 1 : 0);
        const centerAfterCount = item.source === 'factory'
          ? this.state.center.length + item.pushedToCenter
          : this.state.center.length - item.count;
        return factoriesAfter + (centerAfterCount > 0 ? 1 : 0);
      });
      const minGroupsAfterCurrent = groupsAfterCurrentRoutes.length
        ? Math.min(...groupsAfterCurrentRoutes)
        : sourceGroupsBefore;
      const maxGroupsAfterCurrent = groupsAfterCurrentRoutes.length
        ? Math.max(...groupsAfterCurrentRoutes)
        : sourceGroupsBefore;
      const opponentActionsBeforeReturn = Math.max(0, this.state.playerCount - 1);
      const minGroupsWhenActorReturns = Math.max(
        0,
        minGroupsAfterCurrent - opponentActionsBeforeReturn,
      );
      const currentActionCanEndRound = groupsAfterCurrentRoutes.some(count => count === 0);
      const canDecide = (
        seat === this.state.currentPlayer
        && this.state.phase === 'draft'
        && analysis.length > 0
      );
      const decisionSurface = canDecide ? {
        schemaVersion:'natural-decision-surface-v2',
        currentActions:[
          {
            action:'draft_tiles',
            availableNow:analysis.length > 0,
            variants:['factory','center'],
          },
          {
            action:'place_tiles',
            availableNow:analysis.length > 0,
            variants:['pattern','floor'],
          },
        ],
        modelFacts,
        narrativeSections:[
          {
            id:'turn', title:'轮次、行动者与起始玩家标记', lines:[
              `round=${this.state.round}; turn=${this.state.turn}; phase=${this.state.phase}; `
              + `actor seat=${seat}; score=${player.score}; `
              + `firstPlayerTokenAvailable=${this.state.firstPlayerTokenAvailable ? 'yes' : 'no'}`,
              `playerCount=${this.state.playerCount}; cyclicTurnOrder=${this.state.players.map(candidate => candidate.id).join('→')}→repeat; `
              + `if tiles remain after this action nextActorSeat=${(seat + 1) % this.state.playerCount}; `
              + `opponentActionsBeforeActorReturns=${Math.max(0, this.state.playerCount - 1)}; each player action selects exactly one source.`,
              `sourceGroupsBefore=${sourceGroupsBefore}; sourceGroupsAfterCurrentActionRange=${minGroupsAfterCurrent}..${maxGroupsAfterCurrent}; `
              + `currentActionCanEndRound=${currentActionCanEndRound ? 'yes' : 'no'}; `
              + `minimumSourceGroupsWhenActorReturns=${minGroupsWhenActorReturns}; `
              + `actorGuaranteedAnotherDraftThisRound=${minGroupsWhenActorReturns > 0 ? 'yes' : 'no'}.`,
              currentActionCanEndRound
                ? 'exactRoundScoreBoundary=current action may reach round scoring; use the resulting authority outcome.'
                : 'exactRoundScoreBoundary=unknown now; completed lines are guaranteed to place, but later personal drafts can complete other rows and change wall adjacency. Any score stated now is only a lower bound, not an exact round total.',
            ],
          },
          {
            id:'sources', title:'工厂与中央区可见瓷砖', lines:[
              ...this.state.factories.map((tiles, index) => `factory-${index}=${groupedTiles(tiles)}`),
              `center=${groupedTiles(this.state.center)}`,
            ],
          },
          {
            id:'your-pattern-lines', title:'你的图案线与对应墙面格',
            lines:patternLines,
          },
          {
            id:'your-floor', title:'你的地板与边际扣分', lines:[
              `floorTiles=${player.floor.length}; currentRoundFloorPenalty=${floorPenaltyBefore}; `
              + `marginalFloorPenalties=[${core.FLOOR_PENALTIES.slice(player.floor.length).map((penalty, index) => (
                `next${index + 1}:${penalty}`
              )).join(',') || 'none'}]`,
              'Any selected tiles that do not fit the chosen pattern line go to the floor; choosing the floor sends every selected tile there.',
            ],
          },
          {
            id:'your-wall', title:'你的墙面与终局奖励进度', lines:[
              `score=${player.score}; ${wallProgressLine(currentProgress)}`,
            ],
          },
          {
            id:'opponents', title:'公开对手版图',
            lines:opponentLines.length ? opponentLines : ['none'],
          },
          ...(centerTurnOrderPressure ? [{
            id:'center-turn-order-pressure', title:'中央区尾盘轮转与当前放置后果',
            lines:centerTurnOrderPressure,
          }] : []),
          {
            id:'draft-consequences', title:'来源效果与放置计算', lines:[
              ...sourceConsequenceLines,
              'pattern fit = min(selected color count, remainingSlots); overflow = selected color count - fit.',
              'A non-empty pattern line accepts only its existing color; the wall destination for that row and color must still be empty.',
              'lineReadyForRoundEnd=yes means that line places and scores at the end of this current round, not next round, even when the actor is guaranteed another draft first.',
              'A partial line has zero guaranteed round score and still needs the matching color later; tile count or fill percentage alone is not scoring value.',
              'Before choosing a destination, compare every compatible row using fit, overflow, resulting floor penalty, whether the pattern becomes full, score that occurs only when a full line resolves at round end, and persistent wall progress; do not stop at the first line that can complete.',
            ],
          },
          {
            id:'scoring', title:'官方计分与终局', lines:[
              'At round end, an isolated wall tile scores 1; with only horizontal neighbors score the horizontal length; with only vertical neighbors score the vertical length; with neighbors in both directions sum both line lengths, counting the placed tile once in each line.',
              'At round end, completed pattern lines resolve from row 0 through row 4; each wall tile is placed and scored immediately before the next row resolves.',
              'Incomplete pattern lines remain on the player board for the next round; they are not discarded or cleared at round end.',
              'Floor slots score -1,-1,-2,-2,-2,-3,-3 for the round, and total score cannot fall below 0.',
              'At game end, each complete horizontal row scores 2, each complete column 7, and each complete color set 10.',
              'The game ends after a round in which any player completes a horizontal row.',
            ],
          },
        ],
        scoringDecisionFacts:clone(scoringDecisionFacts),
        informationBoundaries:[
          'Only visible factories, center tiles, walls, pattern lines, floors, and scores are shown.',
          'Bag and lid order remain unknown.',
          'This surface contains authority facts only; the acting player remains responsible for its choice.',
        ],
      } : undefined;
      return {
        decisionId: this.decisionId(), seat, phase: this.state.phase,
        turnGroupId:`turn:${this.state.turn}:seat:${this.state.currentPlayer}`,
        round: this.state.round, turn: this.state.turn,
        currentPlayer: this.state.currentPlayer,
        actionFamilies: [...new Set(nextActions.map(step => step.op))],
        nextActions,
        publicState,
        ...(decisionSurface ? {decisionSurface} : {}),
        ...(canDecide ? {privateState:{decisionFacts:{
          score:scoreFacts,
          scoringDecisionFacts,
          legalFamilies:[
            {family:'select_source', variants:['factory', 'center'], availableNow:analysis.length > 0, legalTargets:analysis.map(item => ({id:`${item.source}:${item.factoryIndex ?? 'center'}:${item.color}`, label:`${item.source}:${item.color}`, costs:{}, printedPoints:0, immediateEffects:[{type:'take_tiles', count:item.count}, {type:'move_to_center', count:item.pushedToCenter}]})), routeQuery:{stepsContain:[{op:'select_source'}]}},
            {family:'select_color', variants:[], availableNow:analysis.length > 0, legalTargets:[...new Map(analysis.map(item => [item.color, {id:item.color, label:item.color, costs:{}, printedPoints:0, immediateEffects:[]}])).values()], routeQuery:{stepsContain:[{op:'select_color'}]}},
            {family:'place_tiles', variants:['pattern', 'floor'], availableNow:analysis.length > 0, legalTargets:analysis.map(item => ({id:`${item.destination}:${item.row ?? 'floor'}`, label:item.destination, costs:{floorPenalty:item.floorPenaltyAfter}, printedPoints:item.roundScoreDelta ?? 0, immediateEffects:[{type:'place_tiles', fits:item.fits, overflow:item.overflow, completesLine:item.completesLine}]})), routeQuery:{stepsContain:[{op:'place_tiles'}]}},
          ],
          outcomeDimensions:[{id:'floorTiles', label:'floor tiles', currentValue:player.floor.length}, {id:'round', label:'round', currentValue:this.state.round}],
          actionFamilyCoverage:['select_source', 'select_color', 'place_tiles'].map(family => ({family, variants:[], coverageStatus:'bounded', observedProgramCount:0, presentOnReturnedPage:false, continuationAvailable:true})),
        }}} : {}),
        turnOutcomeSummary:{
          decisionId:this.decisionId(), enumerationComplete:false, coverageStatus:'not_explored',
        },
        analysis,
        constraints: [
          "Use exactly begin(turn), select_source, select_color, place_tiles.",
          "A factory source requires factoryIndex; center does not.",
          "A pattern destination requires zero-based row 0-4: row N has capacity N+1; floor does not use row.",
          "The complete four-step chain is validated and committed atomically.",
        ],
      };
    }
    validateTransaction(decisionId, transaction) {
      let steps;
      if (Array.isArray(decisionId)) {
        steps = decisionId;
        decisionId = this.decisionId();
      } else {
        steps = transaction?.steps;
      }
      if ((decisionId || this.decisionId()) !== this.decisionId()) {
        return {
          ok:false, complete:false, stateChanged:false, failedStep:0,
          validatedPrefix:[], code:'STALE_DECISION',
          message:`Current decisionId is ${this.decisionId()}.`,
          correction:'Refresh the current decision and submit a new check.',
          nextActions:[],
        };
      }
      if (!Array.isArray(steps)) {
        return {
          ok:false, complete:false, stateChanged:false, failedStep:0,
          validatedPrefix:[], code:'INVALID_TRANSACTION',
          message:'steps must be an array.', correction:'Start with begin(turn).',
          nextActions:[{op:'begin', action:'turn'}],
        };
      }
      const validated = [];
      for (let index = 0; index < steps.length; index += 1) {
        const expected = index === 0 ? [{op:'begin', action:'turn'}] : this.nextOperations(validated.slice(1));
        if (!expected.some(candidate => same(candidate, steps[index]))) {
          return {
            ok:false, complete:false, stateChanged:false,
            failedStep:index, validatedPrefix:validated,
            code:'ILLEGAL_STEP', message:'The step is illegal at this boundary.',
            correction:'Keep the validated prefix and choose one next action.',
            events:[], causalTrace:[], immediateEffects:[],
            immediateScoreDelta:0, endNowScoreDelta:0,
            scoringEngineChanges:[], nextActions:expected,
          };
        }
        validated.push(clone(steps[index]));
      }
      const complete = validated.length === 4;
      let events = [];
      let outcome = null;
      if (complete) {
        const transition = core.applyAction(this.state, this._action(validated));
        events = transition.events;
        outcome = this._outcome(this.state, transition.state, this._action(validated), events);
      }
      return {
        ok:true, complete, stateChanged:false, validatedPrefix:validated,
        events:clone(events),
        causalTrace:events.map((event, index) => ({
          step:index + 1, source:event.type, effect:event.type,
        })),
        immediateEffects:[...new Set(events.map(event => event.type))],
        immediateScoreDelta:Number(outcome?.scoreDelta || 0),
        endNowScoreDelta:Number(outcome?.scoreDelta || 0),
        outcome:clone(outcome || {}),
        scoringEngineChanges:[],
        nextActions:complete ? [] : this.nextOperations(validated.slice(1)),
      };
    }
    _action(steps) {
      const sourceStep = steps[1], colorStep = steps[2], destinationStep = steps[3];
      return {
        source: sourceStep.source === 'factory' ? {kind:'factory', index:sourceStep.factoryIndex} : {kind:'center'},
        color: colorStep.color,
        destination: destinationStep.destination === 'pattern' ? {kind:'pattern', row:destinationStep.row} : {kind:'floor'},
      };
    }
    dispatch(decisionId, transaction) {
      if (decisionId && typeof decisionId === 'object' && transaction === undefined) {
        transaction = decisionId.action || {steps:decisionId.steps || []};
        decisionId = decisionId.decisionId;
      }
      const requestedId = decisionId || this.decisionId();
      const requested = transaction || {steps:[]};
      const fingerprint = transactionFingerprint(requested);
      const prior = this.committed[requestedId];
      if (prior) {
        if (prior.fingerprint === fingerprint) return clone({...prior.result, duplicate:true});
        return this._reject(requestedId, 0, [], 'DECISION_ALREADY_COMMITTED', 'This decision already committed a different transaction.', 'Refresh and use the current decisionId.');
      }
      if (requestedId !== this.decisionId()) return this._reject(requestedId, 0, [], 'STALE_DECISION', `Current decisionId is ${this.decisionId()}.`, 'Refresh and rebuild the transaction.');
      if (!Array.isArray(requested.steps)) return this._reject(requestedId, 0, [], 'INVALID_TRANSACTION', 'steps must be an array.', 'Submit all four ordered steps.');
      const validation = this.validateTransaction(requestedId, requested);
      if (!validation.ok) return this._reject(requestedId, validation.failedStep, validation.validatedPrefix, 'ILLEGAL_STEP', 'The step is illegal at this intermediate state.', 'Keep the validated prefix, choose one nextActions item, and resubmit the complete chain.', validation.nextActions);
      if (requested.steps.length < 4) return this._reject(requestedId, requested.steps.length, requested.steps, 'INCOMPLETE_TURN', 'The action chain stops before tile placement.', 'Append one legal nextActions step and resubmit the complete chain.', validation.nextActions);
      if (requested.steps.length > 4) return this._reject(requestedId, 4, requested.steps.slice(0,4), 'EXTRA_STEP', 'Azul turn decisions end after place_tiles.', 'Remove later steps and resubmit the four-step chain.', []);
      const before = this.state;
      let transition;
      try { transition = core.applyAction(this.state, this._action(requested.steps)); }
      catch (error) { return this._reject(requestedId, 3, requested.steps.slice(0,3), 'ILLEGAL_ACTION', error.message, 'Choose one legal placement and resubmit the complete chain.'); }
      this.state = transition.state;
      this._authorityGateway?.invalidate();
      const result = {
        ok:true, decisionId:requestedId, action:clone(requested),
        events:clone(transition.events),
        outcome:this._outcome(before, transition.state, this._action(requested.steps), transition.events),
        snapshotVersion:1,
      };
      this.committed = {
        ...this.committed,
        [requestedId]: {fingerprint, result:clone(result)},
      };
      this._publish(transition.events);
      return result;
    }
    _reject(decisionId, failedStep, validatedPrefix, code, message, correction, nextActions) {
      return {ok:false, decisionId, failedStep, validatedPrefix:clone(validatedPrefix), code, message, correction, nextActions:clone(nextActions ?? this.nextOperations(validatedPrefix.slice(1)))};
    }
    authoritySnapshot() {
      return {
        schemaVersion:1, decisionId:this.decisionId(),
        wrapper:{turn:this.state.turn,currentPlayer:this.state.currentPlayer,phase:this.state.phase,playerCount:this.state.playerCount},
        game:core.snapshot(this.state), committed:clone(this.committed),
      };
    }
    snapshot() {
      return {
        ...this.authoritySnapshot(),
        adapterView:this.view(this.state.currentPlayer),
      };
    }
    finalResult() { return core.finalResult(this.state); }
    restore(snapshot) {
      if (snapshot && snapshot.game) {
        if (snapshot.schemaVersion !== 1) throw new Error('Unsupported Azul adapter snapshot version.');
        this.state = core.restore(snapshot.game);
        this.committed = clone(snapshot.committed || {});
      } else {
        this.state = core.restore(snapshot);
        this.committed = {};
      }
      this._authorityGateway?.invalidate();
      this._publish([]);
      return this.view(this.state.currentPlayer);
    }
    subscribe(listener) { this.listeners.add(listener); return () => this.listeners.delete(listener); }
    _publish(events) { const snapshot = this.snapshot(); for (const listener of this.listeners) listener(snapshot, clone(events)); }
  }
  return {AzulBGLabAdapter};
});
