(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.BGLabSplendorActionFrame = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function combinations(values, count, start = 0, current = [], result = []) {
    if (current.length === count) {
      result.push([...current]);
      return result;
    }
    for (let index = start; index < values.length; index += 1) {
      current.push(values[index]);
      combinations(values, count, index + 1, current, result);
      current.pop();
    }
    return result;
  }

  function buildActionFact(input) {
    if (input.phase === 'choose_noble') {
      return {
        kind:'CurrentTargetFact',
        id:'current-player-choices',
        title:'Current player choices',
        data:{
          meaning:'Choose one currently listed noble.',
          chooseNobleIds:[...input.nobleIds],
        },
      };
    }
    if (input.phase === 'reserve_discard') {
      return {
        kind:'CurrentTargetFact',
        id:'current-player-choices',
        title:'Current player choices',
        data:{
          meaning:'Discard exactly the required total using only tokens currently held.',
          discardGems:{
            requiredTotal:input.requiredDiscardCount,
            availableTokens:{...input.availableTokens},
          },
        },
      };
    }

    const colors = [...input.availableColors];
    const distinctCount = Math.min(3, colors.length);
    return {
      kind:'CurrentTargetFact',
      id:'current-player-choices',
      title:'Current player choices',
      data:{
        meaning:'These are legal main-action targets. Buying also requires payment tokens using the displayed discounted cost; taking excess tokens requires discards. Use only listed colors, cards and deck levels.',
        choices:{
          takeGems:{
            distinctColorSets:combinations(colors, distinctCount),
            sameColorPairs:input.sameColorPairColors.map(color => [color, color]),
          },
          buyCards:{
            marketIds:input.affordableCards
              .filter(card => card.source === 'market')
              .map(card => card.id),
            reservedIds:input.affordableCards
              .filter(card => card.source === 'reserved')
              .map(card => card.id),
          },
          reserveCards:input.reserveAvailable ? {
            marketIds:[...input.marketCardIds],
            deckLevels:[...input.deckLevels],
          } : {marketIds:[], deckLevels:[]},
        },
      },
    };
  }

  return {buildActionFact};
});
