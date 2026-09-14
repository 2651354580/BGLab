(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.BGLabDraftLifecycle = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const copy = value => value == null ? value : JSON.parse(JSON.stringify(value));

  function create(initial = null) {
    let checkpoint = copy(initial);
    let lockedReason = '';
    const steps = [];
    return {
      start(value) { checkpoint = copy(value); steps.length = 0; lockedReason = ''; },
      push(value) { if (lockedReason) return false; steps.push(copy(value)); return true; },
      undo(current) {
        if (lockedReason || !steps.length) return copy(current);
        return copy(steps.pop());
      },
      restart() { return lockedReason ? copy(checkpoint) : copy(checkpoint); },
      lock(reason) { lockedReason = String(reason || '当前操作不可撤回'); steps.length = 0; },
      clear() { steps.length = 0; lockedReason = ''; },
      get lockedReason() { return lockedReason; },
      get canUndo() { return !lockedReason && steps.length > 0; },
      get depth() { return steps.length; },
    };
  }

  return { create };
});
