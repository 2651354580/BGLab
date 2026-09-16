import type { GameState } from "../core";

/** Boundary of one human submission, including setup and round-end rewards. */
export function humanDecisionEnded(start: GameState, current: GameState): boolean {
  if (current.phase === "finished") return true;
  if (start.phase === "setup") {
    return current.phase !== "setup" || current.setupIndex > start.setupIndex;
  }
  // Round-end rewards are a separate decision, even if the same seat starts
  // them before the engine increments the turn number.
  if (start.phase !== "roundEnd" && current.phase === "roundEnd") return true;
  return current.turn > start.turn || current.currentPlayer !== start.currentPlayer;
}
