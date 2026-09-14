(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.BGLabAzulScoringFrame = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function wallProgress(core, wall) {
    return {
      rows:wall.map((row, index) => ({
        id:`row-${index}`, filled:row.filter(Boolean).length, required:5, bonus:2,
      })),
      columns:Array.from({length:5}, (_, column) => ({
        id:`column-${column}`,
        filled:wall.filter(row => row[column] !== null).length,
        required:5, bonus:7,
      })),
      colors:core.COLORS.map(color => ({
        id:color,
        filled:wall.flat().filter(tile => tile === color).length,
        required:5, bonus:10,
      })),
    };
  }

  function roundEndHorizon(core, state) {
    const completedWallRowWitnesses = [];
    for (const player of state.players) {
      for (let row = 0; row < player.wall.length; row += 1) {
        const emptyColumns = player.wall[row].flatMap((tile, column) => (
          tile === null ? [column] : []
        ));
        if (emptyColumns.length === 0) {
          completedWallRowWitnesses.push({seat:player.id, row, alreadyOnWall:true});
        } else if (emptyColumns.length === 1 && player.patternLines[row].length === row + 1) {
          const column = core.WALL_PATTERN[row].indexOf(player.patternLines[row][0]);
          if (column === emptyColumns[0]) {
            completedWallRowWitnesses.push({seat:player.id, row, column, alreadyOnWall:false});
          }
        }
      }
    }
    return {
      gameEndGuaranteed:completedWallRowWitnesses.length > 0,
      completedWallRowWitnesses,
      meaning:'True: existing wall tiles and full waiting pattern lines already guarantee game end at this round settlement. False: not yet determined; later choices can still end this round. This does not score waiting lines early.',
    };
  }

  function buildScoringDecisionFacts({core, state, seat, analysis}) {
    const player = state.players[seat];
    const currentProgress = wallProgress(core, player.wall);
    const floorPenaltyBefore = core.FLOOR_PENALTIES
      .slice(0, player.floor.length)
      .reduce((total, penalty) => total + penalty, 0);
    const scoreFacts = {
      currentTotal:player.score,
      components:[{id:'wall', label:'current wall score', value:player.score}],
      rules:[
        {
          ruleId:'wall-placement', timing:'round_end',
          formula:'isolated tile = 1; only horizontal neighbors = horizontal length; only vertical neighbors = vertical length; neighbors in both directions = horizontal length + vertical length, counting the tile once per line',
          dependencies:['wall.adjacency'],
          workedExamples:['a horizontal length 3 crossing a vertical length 2 scores 5'],
        },
        {
          ruleId:'floor-penalty', timing:'round_end',
          formula:'floor slots score -1,-1,-2,-2,-2,-3,-3; score cannot fall below 0',
          dependencies:['floor.length'],
          workedExamples:['three floor tiles score -1 -1 -2 = -4'],
        },
        {
          ruleId:'final-bonuses', timing:'game_end',
          formula:'each complete horizontal row = 2; each complete column = 7; each complete color set = 10',
          dependencies:['wall.rows','wall.columns','wall.colors'],
          workedExamples:['one row and one column = 2 + 7 = 9 bonus points'],
        },
      ],
    };
    const scoringGroups = new Map();
    for (const item of analysis) {
      if (item.destination !== 'pattern') continue;
      const key = item.row;
      const group = scoringGroups.get(key) || [];
      group.push(item);
      scoringGroups.set(key, group);
    }
    const scoringTargets = [...scoringGroups.entries()].map(([row, variants]) => {
      const before = player.patternLines[row].length;
      const tilesRequired = row + 1 - before;
      const fits = variants.map(item => item.fits);
      const floorPenalties = variants.map(item => item.floorPenaltyAfter);
      const roundScoreDeltas = variants
        .filter(item => item.endsRound)
        .map(item => Number(item.roundScoreDelta || 0));
      const wallScoresByColor = Object.fromEntries(
        [...new Set(variants.map(item => item.color))].map(color => [
          color,
          core.placementScore(player, row, core.WALL_PATTERN[row].indexOf(color)),
        ]),
      );
      const roundEndWallScoreIfCompleted = variants.some(item => item.completesLine)
        ? Math.max(0, ...Object.values(wallScoresByColor))
        : 0;
      return {
        id:`pattern-row-${row}`,
        actionFamily:'place_tiles',
        printedPoints:0,
        immediateScoreDelta:0,
        endNowScoreDelta:0,
        rawCost:{tiles:tilesRequired},
        effectiveCost:{tiles:tilesRequired},
        spendable:{tiles:Math.max(...variants.map(item => item.count))},
        remainingGap:{tiles:Math.max(0, tilesRequired - Math.max(...fits))},
        affordableNow:variants.some(item => item.completesLine),
        destination:'pattern',
        row,
        currentColor:player.patternLines[row][0] || null,
        eligibleColors:Object.keys(wallScoresByColor),
        wallScoresByColor,
        availableTileCounts:[...new Set(variants.map(item => item.count))].sort((a, b) => a-b),
        roundEndWallScoreIfCompleted,
        scoreTiming:'round_end_when_full_line_resolves',
        floorPenaltyBefore,
        floorPenaltyRange:{
          min:Math.min(...floorPenalties),
          max:Math.max(...floorPenalties),
        },
        possibleRoundScoreDeltas:[...new Set(roundScoreDeltas)].sort((a, b) => a-b),
        scoringStructureChanges:[{
          ruleId:'pattern-line-progress',
          row,
          before,
          afterRange:{
            min:before + Math.min(...fits),
            max:before + Math.max(...fits),
          },
        }],
      };
    });
    return {
      scoreFacts,
      roundEndHorizon:roundEndHorizon(core, state),
      currentProgress,
      floorPenaltyBefore,
      scoringTargets,
      scoringDecisionFacts:{
        contractVersion:'scoring-decision-facts-v2',
        score:scoreFacts,
        endCondition:{
          description:'The game ends after a round in which any player completes a horizontal row; highest score wins, then most complete rows.',
          remaining:{
            ownCompletedHorizontalWallRows:currentProgress.rows.filter(
              row => row.filled === row.required,
            ).length,
            minimumAdditionalWallTilesToCompleteOwnHorizontalRow:Math.min(
              ...currentProgress.rows.map(row => row.required - row.filled),
            ),
            ownEndTriggerReached:currentProgress.rows.some(
              row => row.filled === row.required,
            ),
          },
          tieBreakers:['most complete horizontal rows'],
        },
        scoringTargets,
        endGameProgress:currentProgress,
        currentState:{
          score:player.score,
          patternLines:player.patternLines.map((line, row) => ({
            row,
            capacity:row + 1,
            color:line[0] || null,
            filled:line.length,
            remaining:row + 1 - line.length,
          })),
          floor:{
            occupied:player.floor.length,
            penaltyIfRoundEndedNow:floorPenaltyBefore,
          },
        },
        opponents:state.players
          .filter(candidate => candidate.id !== seat)
          .map(candidate => ({
            seat:candidate.id,
            currentTotal:candidate.score,
            completedRows:candidate.wall.filter(row => row.every(Boolean)).length,
          })),
      },
    };
  }

  return {buildScoringDecisionFacts, wallProgress, roundEndHorizon};
});
