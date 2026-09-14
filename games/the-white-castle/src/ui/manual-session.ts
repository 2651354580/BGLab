import { applyAction, createGame, deserializeGame, serializeGame, type GameAction, type GameState } from "../core";
import type { BglabActionRecord } from "../../../shared/ui/action-history.js";

/** Local hot-seat authority. No model, semantic adapter, websocket or provider. */
export class ManualGameSession {
  private game: GameState;
  private ledger: BglabActionRecord[] = [];

  constructor(state: GameState) {
    this.game = deserializeGame(serializeGame(state));
    let replay = createGame({
      seed: state.setup.seed, playerCount: state.playerCount,
      playerNames: state.players.map((player) => player.name),
      manualSetup: state.phase === "setup" || state.actionHistory.some((action) => action.type === "chooseStartingPair"),
    });
    for (const action of state.actionHistory) {
      const transition = applyAction(replay, action);
      this.ledger.push(this.record(replay, action, transition.events));
      replay = transition.state;
    }
  }

  snapshot(): GameState { return structuredClone(this.game); }
  history(): BglabActionRecord[] { return structuredClone(this.ledger).reverse().slice(0, 64); }
  revision(): string { return `manual:${this.game.actionHistory.length}`; }

  commit(revision: string, actions: readonly GameAction[]): void {
    if (revision !== this.revision()) throw new Error("局面已更新，请重新选择操作。");
    let candidate = this.game;
    const records: BglabActionRecord[] = [];
    for (const action of actions) {
      const transition = applyAction(candidate, action);
      records.push(this.record(candidate, action, transition.events));
      candidate = transition.state;
    }
    this.game = candidate;
    this.ledger.push(...records);
  }

  private record(before: GameState, action: GameAction, events: unknown[]): BglabActionRecord {
    const { type, ...fields } = action;
    const order = before.actionHistory.length;
    return {
      decisionId: `manual:${order}`, turnId: `turn-${before.turn}`, turn: before.turn,
      actor: before.currentPlayer, action: { steps: [{ op: type, ...fields }] },
      events: structuredClone(events), outcome: null, order, text: "",
    };
  }
}
