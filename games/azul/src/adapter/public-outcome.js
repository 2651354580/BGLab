(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.BGLabAzulPublicOutcome = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  // Only public facts produced by _outcome are read. No rule or score calculation.
  function renderPublicOutcome(outcome) {
    const sections = [];
    const atom = value => String(value).trim().replace(/\s+/g, ' ');
    const delta = (label, value) => typeof value === 'number' && value !== 0
      ? `${label}总计${value > 0 ? '增加' : '减少'} ${Math.abs(value)}` : null;
    if ('selectedColor' in outcome && 'selectedTileCount' in outcome)
      sections.push(`选择：${atom(outcome.selectedColor)}×${outcome.selectedTileCount}`);
    if (Number.isInteger(outcome.patternPlacedTileCount) && Number.isInteger(outcome.selectedTilesToFloorCount))
      sections.push(`本次选取去向：花纹行放入 ${outcome.patternPlacedTileCount} 块，地板放入 ${outcome.selectedTilesToFloorCount} 块`);
    if ('destination' in outcome)
      sections.push(`目的地：${atom(outcome.destination)}` + (Number.isInteger(outcome.row) ? `（row=${outcome.row}，从0计数）` : ''));
    const floor = delta('地板', outcome.floorDelta);
    if (floor) sections.push(floor);
    const line = outcome.patternLineAfter;
    const validLine = line && [line.row, line.filled, line.capacity, line.remainingSlots].every(Number.isInteger)
      && typeof line.complete === 'boolean';
    if (validLine) sections.push(`本次放置后：row=${line.row} 花纹线为 ${line.color ?? '空'}，${line.filled}/${line.capacity}，剩余${line.remainingSlots}，${line.complete ? '已满' : '未满'}`);
    if (outcome.endsRound === true) {
      let boundary = '轮次边界：本次行动清空最后来源，随后已完成本轮贴墙、计分和地板清空';
      if (validLine) boundary += line.complete ? '；所选花纹线已满，轮末移 1 块贴墙'
        : outcome.gameFinished === true
          ? '；所选花纹线未满，不贴墙、不计分，本局已无后续轮次'
          : '；所选花纹线未满，不贴墙、不计分，并原样保留到下一轮';
      sections.push(boundary);
    } else if (outcome.endsRound === false) {
      sections.push(validLine && line.complete
        ? '轮次边界：本次行动后尚未贴墙或得分；已满花纹行等待本轮所有来源清空后的轮末结算'
        : '轮次边界：本次行动后仍有来源，尚未进行轮末贴墙或得分');
    }
    if (outcome.gameFinished === true) sections.push('游戏状态：本次行动后游戏已结束');
    const score = delta('分数', outcome.scoreDelta);
    if ('scoreAfter' in outcome) sections.push(score ? `${score}，结束后 ${outcome.scoreAfter}` : `分数结束后 ${outcome.scoreAfter}`);
    else if (score) sections.push(score);
    if (outcome.remaining) {
      const remaining = Object.fromEntries(['bagCount', 'centerCount', 'floorCount', 'lidCount']
        .filter(key => Number.isFinite(outcome.remaining[key])).map(key => [key, outcome.remaining[key]]));
      sections.push('剩余：' + JSON.stringify(remaining));
    }
    if ('nextPlayer' in outcome) sections.push(`下一玩家：${outcome.nextPlayer}`);
    if ('phase' in outcome) sections.push(`阶段：${atom(outcome.phase)}`);
    const text = sections.join('；') + '。';
    if ([...text].length > 600) throw new Error('Azul public outcome exceeds 600 characters');
    return text;
  }
  return {renderPublicOutcome};
});
