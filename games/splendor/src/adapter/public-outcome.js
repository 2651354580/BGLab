(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.BGLabSplendorPublicOutcome = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  function renderPublicOutcome(outcome) {
    const sections = [];
    const delta = (label, value) => typeof value === 'number' && value !== 0
      ? `${label}总计${value > 0 ? '增加' : '减少'} ${Math.abs(value)}` : null;
    const gems = value => Object.fromEntries(Object.entries(value ?? {})
      .filter(([key, amount]) => ['C','D','E','G','O','R','S'].includes(key) && Number.isFinite(amount))
      .sort(([a], [b]) => a.localeCompare(b)));
    if (outcome.gemsDelta) {
      const changes = Object.entries(outcome.gemsDelta).map(([color, value]) => delta(`${color} `, value)).filter(Boolean);
      if (changes.length) sections.push('宝石：' + changes.join('、'));
    }
    if (Number.isInteger(outcome.gemsAfterTotal)) {
      sections.push(Number.isInteger(outcome.gemsPaidTotal)
        ? `支付/持有：实际支付 ${outcome.gemsPaidTotal}，行动后 ${outcome.gemsAfterTotal}`
        : `行动后宝石总数：${outcome.gemsAfterTotal}`);
    }
    if (Array.isArray(outcome.opponentBuyNowSeatsBefore) && Array.isArray(outcome.opponentReserveIfStillVisibleSeatsBefore))
      sections.push(`行动前对手：可买=${JSON.stringify(outcome.opponentBuyNowSeatsBefore)}，可保留=${JSON.stringify(outcome.opponentReserveIfStillVisibleSeatsBefore)}`);
    if (outcome.selectedMarketCardLeaves === true) {
      let market = '市场：所选市场卡已离场';
      if (outcome.selectedMarketCardAvailableAfter === false) market += '且不可再次作为目标';
      if (outcome.replacementRevealedImmediately === true) market += '，立即补牌';
      if (outcome.replacementRevealedImmediately === false) market += '，该层牌库已空，不补牌';
      if (outcome.replacementIdentity === 'unknown') market += '，补牌身份未知';
      sections.push(market);
    }
    if (outcome.card) {
      const card = Object.fromEntries(['bonus','id','level','points'].filter(key => key in outcome.card).map(key => [key,outcome.card[key]]));
      sections.push('卡牌：' + JSON.stringify(card));
    }
    for (const [key, label] of [['reservedDelta','保留卡'],['cardsBought','购卡'],['noblesGained','贵族']]) {
      const change = delta(label, outcome[key]);
      if (change) sections.push(change);
    }
    if (outcome.gameFinished === true) sections.push('游戏状态：本次行动后游戏已结束');
    const score = delta('分数', outcome.scoreDelta);
    if ('scoreAfter' in outcome) sections.push(score ? `${score}，结束后 ${outcome.scoreAfter}` : `分数结束后 ${outcome.scoreAfter}`);
    else if (score) sections.push(score);
    if (outcome.remaining) sections.push('剩余：' + JSON.stringify({bank:gems(outcome.remaining.bank), gems:gems(outcome.remaining.gems)}));
    if ('nextPlayer' in outcome) sections.push(`下一玩家：${outcome.nextPlayer}`);
    if ('phase' in outcome) sections.push(`阶段：${outcome.phase}`);
    const text = sections.join('；') + '。';
    if ([...text].length > 600) throw new Error('Splendor public outcome exceeds 600 characters');
    return text;
  }
  return {renderPublicOutcome};
});
