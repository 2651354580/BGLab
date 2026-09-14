import type { BridgeEnd, DieColor, GameAction } from "../core";

export function bridgeDieActionIndex(actions: readonly GameAction[], bridge: DieColor, index: number, count: number): number {
  if (index < 0 || index >= count) return -1;
  return actions.findIndex((action) => action.type === "draftDie" && action.bridge === bridge
    && (action.end === "left" ? index === 0 : index === count - 1));
}

/** Replays the bridge's visible slot changes without coupling the rules engine to CSS coordinates. */
export function bridgeDieSlots(initialIds: string[], remainingIds: string[], draftedEnds: BridgeEnd[]): number[] {
  const dice = initialIds.map((id, slot) => ({ id, slot }));
  for (const end of draftedEnds.slice(0, Math.max(0, initialIds.length - remainingIds.length))) {
    if (dice.length === 0) break;
    const removedSlot = end === "left" ? dice[0].slot : dice[dice.length - 1].slot;
    if (end === "left") dice.shift();
    else dice.pop();
    // Move the next inner die to the vacated end. A final single die stays
    // at its endpoint, preserving whether it grants the lantern reward.
    if (dice.length >= 2) {
      if (end === "left") dice[0].slot = removedSlot;
      else dice[dice.length - 1].slot = removedSlot;
    }
  }
  const byId = new Map(dice.map((die) => [die.id, die.slot]));
  const fallback = remainingIds.map((_, index) => remainingIds.length === 1 ? Math.floor(initialIds.length / 2) : Math.round(index * (initialIds.length - 1) / (remainingIds.length - 1)));
  return remainingIds.map((id, index) => byId.get(id) ?? fallback[index] ?? 1);
}

interface DieRect { x: number; y: number; width: number; height: number }

/** Endpoint identity comes from history; inner geometry comes from today's count. */
export function bridgeDieRects(slots: readonly number[], initialCount: number, baselineY: number): DieRect[] {
  const innerCount = Math.max(0, slots.length - 2);
  return slots.map((slot, index) => {
    if (slots.length === 1 || index === 0 || index === slots.length - 1) {
      const right = slots.length === 1 ? slot === initialCount - 1 : index !== 0;
      return { x: right ? 133 : 28, y: baselineY, width: 35, height: 35 };
    }
    const size = innerCount >= 3 ? 18 : 22;
    const center = 100 + (index - 1 - (innerCount - 1) / 2) * 22;
    return { x: center - size / 2, y: baselineY + (35 - size) / 2, width: size, height: size };
  });
}
