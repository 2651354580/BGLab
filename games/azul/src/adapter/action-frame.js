(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.BGLabAzulActionFrame = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function sourceId(action) {
    return action.source === 'factory'
      ? `factory-${action.source_index}`
      : 'center';
  }

  function takeChoice(action) {
    return {
      source:action.source,
      sourceIndex:action.source_index,
      color:action.color,
    };
  }

  function placementTarget(item) {
    return item.destination === 'pattern'
      ? `pattern-row-${item.row}`
      : 'floor';
  }

  function outcome(item) {
    const pattern = item.destination === 'pattern';
    const selectedPatternLineScoresAfterAction = (
      pattern && item.endsRound && item.completesLine
    );
    const timing = item.endsRound
      ? (!pattern
        ? 'floor_then_round_end'
        : (selectedPatternLineScoresAfterAction
          ? 'full_scores_at_round_end'
          : 'incomplete_carries_over'))
      : (pattern && item.completesLine
        ? 'full_waits_round_end'
        : 'no_scoring_yet');
    return {
      target:placementTarget(item),
      ...(item.count === 1 && item.completesLine ? {} : {
        overflowToFloor:item.overflow,
        gapAfter:pattern ? item.remainingSlotsAfter : null,
      }),
      timing,
      ...(pattern && item.endsRound ? {
        selectedPatternLineAtRoundEnd:{
          fullAfterPlacement:item.completesLine,
          movesOneTileToWall:item.completesLine,
          scores:item.completesLine,
          persistsIncomplete:!item.completesLine,
        },
      } : {}),
    };
  }

  function movedColors(colors) {
    return Object.fromEntries(
      Object.entries(colors).filter(([, count]) => Number(count) > 0),
    );
  }

  function buildActionFacts(analysis, takeOptions, floorPenaltyBefore) {
    const listedOutcomes = new Map();
    for (const item of analysis) {
      const simple = item.count <= 1 && !item.completesLine && !item.endsRound;
      const key = `${sourceId({
        source:item.source,
        source_index:item.source === 'factory' ? item.factoryIndex : null,
      })}:${item.color}`;
      const values = listedOutcomes.get(key) || [];
      values.push({
        ...(simple
          ? {target:placementTarget(item)}
          : outcome(item)),
        floorLossPoints:{
          alreadyOnFloor:Math.abs(floorPenaltyBefore),
          addedByThisChoice:floorPenaltyBefore-item.floorPenaltyAfter,
          totalAtRoundEnd:Math.abs(item.floorPenaltyAfter),
        },
      });
      listedOutcomes.set(key, values);
    }

    const groups = new Map();
    for (const option of takeOptions) {
      const groupId = sourceId(option.draftAction);
      const values = groups.get(groupId) || [];
      const outcomes = listedOutcomes.get(option.id) || [];
      const outcomesByTarget = new Map(
        outcomes.map(destination => [destination.target, destination]),
      );
      const movedToCenter = movedColors(option.sourceAfter.movedToCenter);
      const centerAfterTake = option.draftAction.source === 'center'
        ? {
          colors:movedColors(option.sourceAfter.remainingColors),
          tileCount:option.sourceAfter.remainingCount,
          colorGroupCount:Object.keys(movedColors(option.sourceAfter.remainingColors)).length,
          empty:option.sourceAfter.sourceEmpty,
          lastRemainingColorGroup:option.sourceAfter.endsRound,
          roundEndsAfterAction:option.sourceAfter.endsRound,
        }
        : null;
      values.push({
        take:takeChoice(option.draftAction),
        selectedCount:option.selectedCount,
        roundEndsAfterAction:option.sourceAfter.endsRound,
        ...(Object.keys(movedToCenter).length ? {movedToCenter} : {}),
        ...(centerAfterTake ? {centerAfterTake} : {}),
        ...(option.takesFirstPlayerToken ? {firstPlayerTokenToFloor:true} : {}),
        destinations:option.compatibleDestinationRefs.map(target => (
          outcomesByTarget.get(target) || {target}
        )),
      });
      groups.set(groupId, values);
    }

    return [...groups.entries()].map(([groupId, options]) => ({
      kind:'DirectOutcomeFact',
      id:`current-drafts:${groupId}`,
      title:`Current legal choices from ${groupId}`,
      data:{options},
    }));
  }

  return {buildActionFacts};
});
