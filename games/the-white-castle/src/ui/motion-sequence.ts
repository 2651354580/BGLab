import { applyAction, canonicalFingerprint, type GameAction, type GameEvent, type GameState } from "../core";

export interface MotionFrame {
  before: GameState;
  after: GameState;
  events: GameEvent[];
}

/** A presentation copy only. Never dispatch or persist these intermediate states. */
export function motionSequence(before: GameState, actions: GameAction[], expected: GameState): MotionFrame[] {
  let current = structuredClone(before);
  let visible = current;
  let events: GameEvent[] = [];
  const frames: MotionFrame[] = [];
  for (const action of actions) {
    const next = applyAction(current, action);
    current = next.state;
    events.push(...next.events);
    if (next.events.some(event => ["DieDrafted", "DiePlaced", "MemberMoved", "CardMoved"].includes(event.type))) {
      frames.push({ before: visible, after: current, events });
      visible = current;
      events = [];
    }
  }
  if (canonicalFingerprint(current) !== canonicalFingerprint(expected)) {
    throw new Error("Presentation replay differs from the confirmed game state.");
  }
  if (events.length || canonicalFingerprint(visible) !== canonicalFingerprint(expected)) {
    frames.push({ before: visible, after: expected, events });
  }
  return frames;
}

export function motionActions(steps: Array<Record<string, unknown>> = []): GameAction[] {
  return steps.filter(step => step.op !== "begin").map(step => {
    // The adapter's public annotations are not engine actions or history fields.
    const { op, row, publicFloor, publicRoom, publicWorkspace, personalRow, choiceType, ...fields } = step;
    return { type: op, ...fields } as GameAction;
  });
}
