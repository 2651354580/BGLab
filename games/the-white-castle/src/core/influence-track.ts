// Positions count moves from the heron (0), not the golden fan scores.
export const INFLUENCE_CHECKPOINTS = [
  { position: 6, cost: 1 },
  { position: 11, cost: 2 },
  { position: 15, cost: 3 },
] as const;
export const INFLUENCE_TRACK_END = 20;

export function scoreTimeTrack(position: number): number {
  if (position < INFLUENCE_CHECKPOINTS[0].position) return 0;
  if (position < INFLUENCE_CHECKPOINTS[1].position) return 3;
  if (position < INFLUENCE_CHECKPOINTS[2].position) return 6;
  return Math.min(INFLUENCE_TRACK_END, position) - 5;
}
