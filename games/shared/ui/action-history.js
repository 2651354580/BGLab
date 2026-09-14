(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.BGLabActionHistory = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const clone = value => value == null ? value : JSON.parse(JSON.stringify(value));

  function committedMap(snapshot) {
    return snapshot?.committed
      || snapshot?.game?.committed
      || snapshot?.state?.committed
      || {};
  }

  function actorOf(result, events) {
    const event = (events || []).find(item => Number.isInteger(item?.player));
    if (event) return event.player;
    const action = result?.action;
    if (Number.isInteger(action?.player)) return action.player;
    const step = action?.steps?.find(item => Number.isInteger(item?.player));
    return step?.player ?? null;
  }

  function turnOf(decisionId, result) {
    if (Number.isInteger(result?.turn)) return result.turn;
    const match = String(decisionId || '').match(/(?:^|:)(\d+)(?::|$)/);
    return match ? Number(match[1]) : null;
  }

  function records(snapshot, formatter, limit = 20) {
    const entries = Object.entries(committedMap(snapshot));
    return entries.reverse().map(([decisionId, committed], order) => {
      const result = committed?.result || committed || {};
      const events = Array.isArray(result.events) ? clone(result.events) : [];
      const record = {
        decisionId: result.decisionId || decisionId,
        turnId: result.turnId || result.decisionId || decisionId,
        turn: turnOf(result.decisionId || decisionId, result),
        actor: actorOf(result, events),
        action: clone(result.action),
        events,
        outcome: clone(result.outcome),
        order,
      };
      return {
        ...record,
        text: typeof formatter === 'function' ? formatter(record) : '',
      };
    }).filter(record => record.text).slice(0, limit);
  }

  function merge(existing, incoming, limit = 20) {
    const byDecision = new Map((existing || []).map(item => [item.decisionId, item]));
    for (const item of incoming || []) byDecision.set(item.decisionId, item);
    return [...byDecision.values()].sort((left, right) => right.order - left.order).slice(0, limit);
  }

  return { records, merge, committedMap };
});
