import { placementCoinDelta } from "./placement";
import { createGardens, createTrainingYards, createWorkspaces } from "./data";
import { canonicalFingerprint } from "./canonical";
import { determineWinner, scoreGame } from "./scoring";
import { INFLUENCE_CHECKPOINTS, INFLUENCE_TRACK_END } from "./influence-track";
export { INFLUENCE_CHECKPOINTS } from "./influence-track";
import { createBoardSetup, seededShuffle } from "./setup";
import { materialEffects } from "./effects";
import { castleCardDomainRowGroups, castleCardLightRows, castleCardRowData, castleCardRows, DAIMYO_CARDS, DECREE_CARDS, DIPLOMAT_CARDS, GARDEN_CARDS, STARTING_ACTION_CARDS, STARTING_RESOURCE_CARDS, STEWARD_CARDS, YARD_TILES } from "./material";
import {
  ActionPlan, BoardState, BridgeEnd, CastleActionSelector, CappedResource, CreateGameOptions, Die, DieColor, Effect, EffectGroup, GameAction, GameEvent,
  GameState, MajorAction, MajorActionMode, PendingEffect, PlayerState, PlayerCount, Resource, TrainingYard, Transition, Workspace,
} from "./types";

const COLORS: DieColor[] = ["black", "white", "coral"];
export const RESOURCE_CAPS: Readonly<Record<Exclude<Resource, "coins">, number>> = Object.freeze({ seals: 5, food: 7, iron: 7, pearl: 7 });

function random(seed: number): [number, number] {
  const next = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
  return [next / 0x100000000, next];
}

function createStartingOffers(seed: number, playerCount: PlayerCount): GameState["startingOffers"] {
  const resourceCards = seededShuffle(STARTING_RESOURCE_CARDS, seed + 61).slice(0, playerCount + 1);
  const actionCards = seededShuffle(STARTING_ACTION_CARDS, seed + 67).slice(0, playerCount + 1);
  return resourceCards.map((card, index) => ({
    resourceCard:card.id,
    actionCard:actionCards[index].id,
    claimedBy:null,
  }));
}

function startingSetId(offer: GameState["startingOffers"][number]): string {
  return `resource-${offer.resourceCard}+action-${offer.actionCard}`;
}

function rollBridges(seed: number, round: number, playerCount: PlayerCount): Record<DieColor, Die[]> {
  let cursor = (seed + round * 2654435761) >>> 0;
  const result = {} as Record<DieColor, Die[]>;
  for (const color of COLORS) {
    const values: number[] = [];
    for (let index = 0; index < playerCount + 1; index += 1) {
      const [value, next] = random(cursor);
      cursor = next;
      values.push(Math.floor(value * 6) + 1);
    }
    values.sort((a, b) => a - b);
    result[color] = values.map((value, index) => ({ id: `r${round}-${color}-${index + 1}`, color, value }));
  }
  return result;
}

function createMembers(player: number): PlayerState["members"] {
  return (["courtier", "gardener", "warrior"] as const).flatMap((type) =>
    Array.from({ length: 5 }, (_, index) => ({ id: `p${player}-${type}-${index + 1}`, type, location: "domain" })),
  );
}

function lanternEffectsFromCards(cards: string[]): Effect[] {
  return cards.flatMap((card) => {
    if (card.startsWith("starting-resource-")) {
      const resource = STARTING_RESOURCE_CARDS.find((candidate) => candidate.id === Number(card.split("-").at(-1)));
      return resource ? materialEffects(resource.lantern) : [];
    }
    if (card.startsWith("decree-")) {
      const decree = card.replace("decree-", "") as keyof typeof DECREE_CARDS;
      return DECREE_CARDS[decree] ? materialEffects(DECREE_CARDS[decree]) : [];
    }
    if (card.startsWith("starting-")) return [{ type: "influence", amount: 1 } as Effect];
    if (card.startsWith("steward-")) return [{ type: "gainPoints", amount: 1 } as Effect];
    if (card.startsWith("diplomat-")) return [{ type: "gain", resource: "coins", amount: 1 } as Effect];
    return [];
  });
}

function isPlayerCount(value: unknown): value is PlayerCount {
  return value === 2 || value === 3 || value === 4;
}

function isSeat(state: GameState, value: unknown): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 && value < state.playerCount;
}

export function createGame(options: CreateGameOptions = {}): GameState {
  const seed = options.seed ?? 1;
  const playerCount = options.playerCount ?? options.playerNames?.length ?? 2;
  if (!isPlayerCount(playerCount)) throw new Error("White Castle supports 2, 3 or 4 players.");
  if (options.playerNames && options.playerNames.length !== playerCount) throw new Error("Player names must match the player count.");
  const names = options.playerNames ?? Array.from({ length: playerCount }, (_, index) => `玩家 ${index + 1}`);
  const turnOrder = seededShuffle(names.map((_, index) => index), seed + 59);
  const setupOrder = [...turnOrder].reverse();
  const startingOffers = createStartingOffers(seed, playerCount);
  const players: PlayerState[] = names.map((name, id) => ({
    id,
    name,
    resources: { coins: 0, seals: 0, food: 0, iron: 0, pearl: 0 },
    influence: 0,
    points: 0,
    actionCards: { courtier: "", gardener: "", warrior: "" },
    domainCard: { kind: "starting", id: 0 },
    lanternCards: [],
    lanternEffects: [],
    members: createMembers(id),
  }));
  const bridges = rollBridges(seed, 1, playerCount);
  const board = createBoardSetup(seed, playerCount);
  const state: GameState = {
    version: 1,
    playerCount,
    setup: { seed, initialBridgeValues: Object.fromEntries(COLORS.map((color) => [color, bridges[color].map((die) => die.value)])) as Record<DieColor, number[]>, startingSetIds: [], turnOrder: [...turnOrder], stewardDeck: [...board.stewardDeck], diplomatDeck: [...board.diplomatDeck], daimyoCard: board.daimyoCard, yardTiles: [...board.yardTileIds], gardenCards: [...board.gardenCardIds] },
    round: 1,
    turn: 1,
    turnsThisRound: 0,
    currentPlayer: options.manualSetup ? setupOrder[0] : turnOrder[0],
    turnOrder,
    influenceStackOrder: [...turnOrder].reverse(),
    phase: options.manualSetup ? "setup" : "draft",
    bridges,
    board,
    workspaces: createWorkspaces(players, board),
    gardens: createGardens(board),
    trainingYards: createTrainingYards(board),
    players,
    pendingEffects: [],
    actionFlow: undefined,
    usedDomainRowsThisTurn: [],
    actionHistory: [],
    startingOffers,
    setupOrder,
    setupIndex: 0,
  };
  if (!options.manualSetup) {
    setupOrder.forEach((player, index) => claimStartingPair(state, player, index, true, []));
    state.setupIndex = setupOrder.length;
    state.currentPlayer = turnOrder[0];
  }
  return state;
}

function remainingSingleDieEnd(state: GameState, bridge: DieColor): BridgeEnd {
  const lastDraft = [...state.actionHistory].reverse().find((action) => (
    action.type === "draftDie" && action.bridge === bridge
  ));
  if (lastDraft?.type !== "draftDie") return "left";
  return lastDraft.end === "left" ? "right" : "left";
}

export function getLegalActions(state: GameState): GameAction[] {
  if (state.phase === "finished") return [];
  const exchanges = legalExchanges(state);
  if (state.phase === "setup" && state.pendingEffects.length === 0) {
    return state.startingOffers.flatMap((offer, index): GameAction[] => offer.claimedBy === null ? [{ type: "chooseStartingPair", offer: index }] : []);
  }
  if (state.phase === "draft") {
    const drafts = COLORS.flatMap((bridge): GameAction[] => {
      const dice = state.bridges[bridge];
      if (dice.length === 0) return [];
      if (dice.length === 1) return [{ type: "draftDie", bridge, end: remainingSingleDieEnd(state, bridge) }];
      return [{ type: "draftDie", bridge, end: "left" }, { type: "draftDie", bridge, end: "right" }];
    });
    return [...drafts, ...exchanges];
  }
  if (state.phase === "place" && state.draftedDie) {
    return [
      ...Object.values(state.workspaces).filter((workspace) => canPlace(state, workspace)).map((workspace): GameAction => ({ type: "placeDie", workspace: workspace.id })),
      ...exchanges,
    ];
  }
  const pending = state.pendingEffects[0];
  if (!pending) return [{ type: "finishResolution" }, ...exchanges];
  if (pending.effect.type === "gainChoice") {
    return [...pending.effect.resources.map((_, option): GameAction => ({ type: "chooseEffectOption", effectId: pending.id, option })), ...exchanges];
  }
  if (pending.effect.type === "effectOrder") {
    return [...pending.effect.effects.map((_, option): GameAction => ({ type: "chooseEffectOption", effectId: pending.id, option })), ...exchanges];
  }
  if (pending.effect.type === "actionOrder") {
    return [...pending.effect.groups.map((_, option): GameAction => ({ type: "chooseEffectOption", effectId: pending.id, option })), ...exchanges];
  }
  if (pending.effect.type === "chooseOne") {
    return [...pending.effect.options.map((_, option): GameAction => ({ type: "chooseEffectOption", effectId: pending.id, option })), ...exchanges];
  }
  if (pending.effect.type === "daimyoReward") return [...pending.effect.positions.map((_, option): GameAction => ({ type: "chooseEffectOption", effectId: pending.id, option })), ...exchanges];
  if (pending.effect.type === "domainAction") return [...availableDomainRows(state).map((_, option): GameAction => ({ type: "chooseEffectOption", effectId: pending.id, option })), ...exchanges];
  if (pending.effect.type === "castleTileAction") return [
    ...availableCastleTileActions(state, pending.effect.color).map((target): GameAction => ({
      type: "selectCastleTileAction", effectId: pending.id, room: target.room, rowId: target.rowId,
    })),
    ...exchanges,
  ];
  if (pending.effect.type === "gardenActivation") return [
    ...pending.effect.gardens.map((_, option): GameAction => ({ type: "chooseEffectOption", effectId: pending.id, option })),
    ...exchanges,
  ];
  if (pending.effect.type === "pay") {
    const actions: GameAction[] = pending.effect.optional ? [{ type: "chooseEffectOption", effectId: pending.id, option: 1 }] : [];
    if (state.players[state.currentPlayer].resources[pending.effect.resource] >= pending.effect.amount) actions.unshift({ type: "chooseEffectOption", effectId: pending.id, option: 0 });
    return [...actions, ...exchanges];
  }
  if (pending.effect.type === "influence" && nextInfluenceCheckpoint(state, pending.effect.amount)) {
    const checkpoint = nextInfluenceCheckpoint(state, pending.effect.amount)!;
    const actions: GameAction[] = [{ type: "chooseEffectOption", effectId: pending.id, option: 1 }];
    if (state.players[state.currentPlayer].resources.seals >= checkpoint.cost) actions.unshift({ type: "chooseEffectOption", effectId: pending.id, option: 0 });
    return [...actions, ...exchanges];
  }
  if (pending.effect.type === "castleRefresh") return [{ type: "refreshCastleRoom", room: pending.effect.room }, ...exchanges];
  if (pending.effect.type === "majorAction") return [...legalMajorActions(state, pending), ...exchanges];
  throw new Error(`Unsupported blocking effect: ${pending.effect.type}`);
}

export function applyAction(state: GameState, action: GameAction): Transition {
  if (!getLegalActions(state).some((candidate) => actionsEqual(candidate, action))) throw new Error(`Illegal action: ${JSON.stringify(action)}`);
  return applyKnownLegalAction(state, action);
}

function cloneGameValue<T>(value: T): T {
  if (Array.isArray(value)) {
    const clone = new Array(value.length);
    for (let index = 0; index < value.length; index += 1) clone[index] = cloneGameValue(value[index]);
    return clone as T;
  }
  if (value !== null && typeof value === "object") {
    const source = value as Record<string, unknown>;
    const clone: Record<string, unknown> = {};
    for (const key in source) clone[key] = cloneGameValue(source[key]);
    return clone as T;
  }
  return value;
}

/**
 * Applies an action already obtained from getLegalActions(state).
 *
 * This is intentionally separate from applyAction so exhaustive state-space
 * generation does not regenerate the same legal-action list once per outgoing
 * edge. User/model input must continue to use applyAction.
 */
export function applyKnownLegalAction(state: GameState, action: GameAction): Transition {
  const next = cloneGameValue(state);
  const events: GameEvent[] = [];
  next.actionHistory.push(cloneGameValue(action));

  if (action.type === "chooseStartingPair") {
    claimStartingPair(next, next.currentPlayer, action.offer, false, events);
    drainAutomaticEffects(next, events);
    advanceSetupIfReady(next, events);
    return { state: next, events };
  }
  if (action.type === "exchangeSeal") {
    exchangeSeal(next, action.receive, events);
    return { state: next, events };
  }
  if (action.type === "draftDie") {
    const die = removeBridgeEnd(next.bridges[action.bridge], action.end);
    const lanternTriggered = action.end === "left";
    next.draftedDie = { die, fromBridge: action.bridge, fromEnd: action.end, lanternTriggered };
    next.phase = "place";
    events.push({ type: "DieDrafted", player: next.currentPlayer, die, bridge: action.bridge, end: action.end, lanternTriggered });
    return { state: next, events };
  }
  if (action.type === "placeDie") {
    placeDie(next, action.workspace, events);
    drainAutomaticEffects(next, events);
    return { state: next, events };
  }
  if (action.type === "chooseEffectOption") {
    resolveChoice(next, action.effectId, action.option, events);
    drainAutomaticEffects(next, events);
    advanceSetupIfReady(next, events);
    return { state: next, events };
  }
  if (action.type === "selectCastleTileAction") {
    resolveCastleTileChoice(next, action.effectId, action.room, action.rowId, events);
    drainAutomaticEffects(next, events);
    return { state: next, events };
  }
  if (action.type === "beginMajorAction" || action.type === "selectMajorActionSource" || action.type === "selectMajorActionTarget" || action.type === "cancelMajorActionSelection" || action.type === "confirmMajorAction" || action.type === "finishMajorAction") {
    resolveMajorActionFlow(next, action, events);
    drainAutomaticEffects(next, events);
    return { state: next, events };
  }
  if (action.type === "refreshCastleRoom") {
    resolveCastleRefresh(next, action.room, events);
    drainAutomaticEffects(next, events);
    return { state: next, events };
  }
  if (next.phase === "roundEnd") finishRound(next, events);
  else finishTurn(next, events);
  return { state: next, events };
}

/** Applies a complete human/AI action plan while validating every intermediate step against the same legal-action API. */
export function applyActionPlan(state: GameState, plan: ActionPlan): Transition {
  let next = state;
  const events: GameEvent[] = [];
  for (const action of plan.actions) {
    const transition = applyAction(next, action);
    next = transition.state;
    events.push(...transition.events);
  }
  return { state: next, events };
}

function claimStartingPair(state: GameState, playerId: number, offerIndex: number, automaticChoices: boolean, events: GameEvent[]): void {
  const offer = state.startingOffers[offerIndex];
  if (!offer || offer.claimedBy !== null) throw new Error("Starting pair is unavailable.");
  const player = state.players[playerId];
  const resourceCard = STARTING_RESOURCE_CARDS.find((card) => card.id === offer.resourceCard)!;
  const actionCard = STARTING_ACTION_CARDS.find((card) => card.id === offer.actionCard)!;
  offer.claimedBy = playerId;
  for (const [resource, amount] of Object.entries(resourceCard.resources) as [Resource, number][]) {
    if (!automaticChoices) changeResource(state, playerId, resource, amount, events);
    else player.resources[resource] = amount;
  }
  if (resourceCard.choiceResources) {
    if (automaticChoices) {
      const choices: CappedResource[] = ["food", "iron", "pearl"];
      for (let index = 0; index < resourceCard.choiceResources; index += 1) player.resources[choices[index % choices.length]] += 1;
    } else {
      state.pendingEffects.push({
        id: `setup-p${playerId}-resources`,
        source: `starting-resource-${resourceCard.id}`,
        effect: { type: "gainChoice", resources: ["food", "iron", "pearl"], amount: resourceCard.choiceResources },
        owner: playerId,
      });
    }
  }
  player.domainCard = { kind: "starting", id: actionCard.id };
  if (actionCard.effect.type !== "majorAction") throw new Error("Starting action card must contain a family-member action.");
  player.actionCards[actionCard.effect.action] = `starting-action-${actionCard.id}`;
  player.lanternCards = [`starting-resource-${resourceCard.id}`, ...(resourceCard.decree ? [`decree-${resourceCard.decree}`] : [])];
  player.lanternEffects = lanternEffectsFromCards(player.lanternCards);
  state.setup.startingSetIds.push(`resource-${resourceCard.id}+action-${actionCard.id}`);
}

function advanceSetupIfReady(state: GameState, events: GameEvent[]): void {
  if (state.phase !== "setup" || state.pendingEffects.length > 0) return;
  state.setupIndex += 1;
  if (state.setupIndex < state.setupOrder.length) {
    state.currentPlayer = state.setupOrder[state.setupIndex];
    return;
  }
  state.currentPlayer = state.turnOrder[0];
  state.phase = "draft";
  events.push({ type: "TurnStarted", player: state.currentPlayer, turn: state.turn });
}

function actionsEqual(left: GameAction, right: GameAction): boolean {
  return canonicalFingerprint(left) === canonicalFingerprint(right);
}

function orderedPending(source: string, effects: Effect[], id: string, owner?: number): PendingEffect[] {
  if (effects.length === 0) return [];
  const ownership = owner === undefined ? {} : { owner };
  if (effects.length === 1) return [{ id, source, effect: effects[0], ...ownership }];
  return [{ id: `${id}-order`, source, effect: { type: "effectOrder", effects }, ...ownership }];
}

function printedSequencePending(source: string, effects: Effect[], id: string, owner?: number): PendingEffect[] {
  const ownership = owner === undefined ? {} : { owner };
  return effects.map((effect, index) => ({ id: `${id}-${index + 1}`, source, effect, ...ownership }));
}

function lanternPending(source: string, effects: Effect[], id: string, owner?: number): PendingEffect[] {
  return orderedPending(source, effects, id, owner);
}

function actionGroupsPending(source: string, groups: EffectGroup[], id: string, owner?: number, unwrapSingle = false): PendingEffect[] {
  const nonEmpty = groups.filter((group) => group.effects.length > 0);
  if (nonEmpty.length === 0) return [];
  if (unwrapSingle && nonEmpty.length === 1) return printedSequencePending(source, nonEmpty[0].effects, `${id}-${nonEmpty[0].id}`, owner);
  return [{ id: `${id}-actions`, source, effect: { type: "actionOrder", groups: nonEmpty }, ...(owner === undefined ? {} : { owner }) }];
}

function legalExchanges(state: GameState): GameAction[] {
  const player = state.players[state.currentPlayer];
  const result: GameAction[] = [];
  if (player.resources.seals >= 1) result.push({ type: "exchangeSeal", receive: "coins" });
  if (player.resources.seals >= 2) {
    for (const resource of ["food", "iron", "pearl"] as CappedResource[]) {
      if (player.resources[resource] < RESOURCE_CAPS[resource]) result.push({ type: "exchangeSeal", receive: resource });
    }
  }
  return result;
}

function exchangeSeal(state: GameState, receive: "coins" | CappedResource, events: GameEvent[]): void {
  const cost = receive === "coins" ? 1 : 2;
  changeResource(state, state.currentPlayer, "seals", -cost, events);
  changeResource(state, state.currentPlayer, receive, 1, events);
}

function legalMajorActions(state: GameState, pending: PendingEffect): GameAction[] {
  if (pending.effect.type !== "majorAction") return [];
  const player = state.players[state.currentPlayer];
  const flow = state.actionFlow;
  if (flow) {
    const cancel: GameAction = { type: "cancelMajorActionSelection" };
    if (flow.stage === "preview") return [{ type: "confirmMajorAction" }, cancel];
    if (flow.stage === "chooseSource") {
      if (flow.mode === "promote") {
        const sources = player.members.filter((member) => member.type === "courtier" && promotionTargets(state, member.id).length > 0);
        return [...sources.map((member): GameAction => ({ type: "selectMajorActionSource", member: member.id })), cancel];
      }
      const type = flow.mode === "recruit" ? "courtier" : flow.mode;
      const member = player.members.find((candidate) => candidate.type === type && candidate.location === "domain");
      return member ? [{ type: "selectMajorActionSource", member: member.id }, cancel] : [cancel];
    }
    if (!flow.selectedSource) return [cancel];
    if (flow.mode === "gardener") return [...state.gardens.filter((garden) => !garden.gardeners.includes(player.id) && player.resources.food >= garden.foodCost).map((garden): GameAction => ({ type: "selectMajorActionTarget", target: garden.id })), cancel];
    if (flow.mode === "warrior") return [...state.trainingYards.filter((yard) => (yard.capacity === "unlimited" || yard.warriors.length < yard.capacity) && player.resources.iron >= yard.ironCost).map((yard): GameAction => ({ type: "selectMajorActionTarget", target: yard.id })), cancel];
    if (flow.mode === "recruit") return [{ type: "selectMajorActionTarget", target: "gate" }, cancel];
    return [...promotionTargets(state, flow.selectedSource), cancel];
  }

  return [
    ...availableMemberActionModes(state, state.currentPlayer, pending.effect.action, pending.completedSubactions)
      .map((mode): GameAction => ({ type: "beginMajorAction", mode })),
    { type: "finishMajorAction" },
  ];
}

/** Current-resource member entry facts. Does not forecast rewards or open a turn. */
export function availableMemberActionModes(
  state: GameState,
  seat: number,
  action: MajorAction,
  completed: readonly ("recruit" | "promote")[] = [],
): MajorActionMode[] {
  const player = state.players[seat];
  const modes: MajorActionMode[] = [];
  if (action === "gardener") {
    const hasSource = player.members.some((member) => member.type === "gardener" && member.location === "domain");
    const hasTarget = state.gardens.some((garden) => !garden.gardeners.includes(player.id) && player.resources.food >= garden.foodCost);
    if (hasSource && hasTarget) modes.push("gardener");
  } else if (action === "warrior") {
    const hasSource = player.members.some((member) => member.type === "warrior" && member.location === "domain");
    const hasTarget = state.trainingYards.some((yard) => (yard.capacity === "unlimited" || yard.warriors.length < yard.capacity) && player.resources.iron >= yard.ironCost);
    if (hasSource && hasTarget) modes.push("warrior");
  } else {
    if (!completed.includes("recruit") && player.resources.coins >= 2 && player.members.some((member) => member.type === "courtier" && member.location === "domain")) modes.push("recruit");
    if (!completed.includes("promote") && player.members.some((member) => member.type === "courtier" && promotionTargets(state, member.id, seat).length > 0)) modes.push("promote");
  }
  return modes;
}

function promotionTargets(state: GameState, memberId: string, seat = state.currentPlayer): Extract<GameAction, { type: "selectMajorActionTarget" }>[] {
  const player = state.players[seat];
  const member = player.members.find((candidate) => candidate.id === memberId && candidate.type === "courtier");
  if (!member) return [];
  const floor = courtierFloor(member.location);
  const actions: Extract<GameAction, { type: "selectMajorActionTarget" }>[] = [];
  for (const levels of [1, 2] as const) {
    const destination = floor + levels;
    const cost = levels === 1 ? 2 : 5;
    if (floor < 0 || destination > 3 || player.resources.pearl < cost) continue;
    const rooms = destination === 1 ? state.board.castleRooms.filter((room) => room.floor === "steward") : destination === 2 ? state.board.castleRooms.filter((room) => room.floor === "diplomat") : [];
    if (destination === 3) actions.push({ type: "selectMajorActionTarget", target: "daimyo", levels });
    else for (const room of rooms) actions.push({ type: "selectMajorActionTarget", target: room.id, levels });
  }
  return actions;
}

function resolveMajorActionFlow(state: GameState, action: Extract<GameAction, { type: "beginMajorAction" | "selectMajorActionSource" | "selectMajorActionTarget" | "cancelMajorActionSelection" | "confirmMajorAction" | "finishMajorAction" }>, events: GameEvent[]): void {
  const pending = state.pendingEffects[0];
  if (!pending || pending.effect.type !== "majorAction") throw new Error("No major action is pending.");
  const player = state.players[state.currentPlayer];
  if (action.type === "finishMajorAction") {
    state.actionFlow = undefined;
    state.pendingEffects.shift();
    events.push({ type: "EffectResolved", player: player.id, source: pending.source, effect: pending.effect });
    return;
  }
  if (action.type === "cancelMajorActionSelection") {
    state.actionFlow = undefined;
    return;
  }
  if (action.type === "beginMajorAction") {
    state.actionFlow = { id: `${pending.id}-${action.mode}`, kind: pending.effect.action, mode: action.mode, stage: "chooseSource", costs: {}, reversible: true };
    return;
  }
  const flow = state.actionFlow;
  if (!flow) throw new Error("No major-action selection is active.");
  if (action.type === "selectMajorActionSource") {
    flow.selectedSource = action.member;
    flow.selectedTarget = undefined;
    flow.levels = undefined;
    flow.stage = "chooseTarget";
    return;
  }
  if (action.type === "selectMajorActionTarget") {
    flow.selectedTarget = action.target;
    flow.levels = action.levels;
    flow.costs = flow.mode === "gardener" ? { food: state.gardens.find((garden) => garden.id === action.target)?.foodCost ?? 0 }
      : flow.mode === "warrior" ? { iron: state.trainingYards.find((yard) => yard.id === action.target)?.ironCost ?? 0 }
        : flow.mode === "recruit" ? { coins: 2 } : { pearl: action.levels === 2 ? 5 : 2 };
    flow.stage = "preview";
    return;
  }
  if (!flow.selectedSource || !flow.selectedTarget) throw new Error("The major-action preview is incomplete.");
  state.actionFlow = undefined;
  if (flow.mode === "gardener") {
    const garden = state.gardens.find((candidate) => candidate.id === flow.selectedTarget);
    const member = player.members.find((candidate) => candidate.type === "gardener" && candidate.location === "domain");
    if (!garden || !member) throw new Error("Gardener target is unavailable.");
    changeResource(state, player.id, "food", -garden.foodCost, events);
    moveMember(player.id, member, garden.id, events);
    garden.gardeners.push(player.id);
    completeMajorActionWithEffects(state, pending, garden.effects, events);
    return;
  }
  if (flow.mode === "warrior") {
    const yard = state.trainingYards.find((candidate) => candidate.id === flow.selectedTarget);
    const member = player.members.find((candidate) => candidate.type === "warrior" && candidate.location === "domain");
    if (!yard || !member) throw new Error("Warrior target is unavailable.");
    changeResource(state, player.id, "iron", -yard.ironCost, events);
    moveMember(player.id, member, yard.id, events);
    yard.warriors.push(player.id);
    completeMajorActionWithActionGroups(state, pending, yard.effects.map((effect, index) => ({
      id: `yard-tile-${yard.tileIds[index] ?? index + 1}`,
      effects: [effect],
    })), events);
    return;
  }
  pending.completedSubactions ??= [];
  if (flow.mode === "recruit") {
    const member = player.members.find((candidate) => candidate.id === flow.selectedSource && candidate.type === "courtier" && candidate.location === "domain");
    if (!member) throw new Error("No courtier remains in the domain.");
    changeResource(state, player.id, "coins", -2, events);
    moveMember(player.id, member, "gate", events);
    pending.completedSubactions.push("recruit");
    return;
  }
  const member = player.members.find((candidate) => candidate.id === flow.selectedSource && candidate.type === "courtier");
  if (!member) throw new Error("Courtier is unavailable.");
  const floor = courtierFloor(member.location);
  const levels = flow.levels ?? 1;
  const destinationFloor = floor + levels;
  changeResource(state, player.id, "pearl", levels === 1 ? -2 : -5, events);
  const destination = flow.selectedTarget;
  if (!destination) throw new Error("A castle room must be selected for this promotion.");
  moveMember(player.id, member, destination, events);
  pending.completedSubactions.push("promote");
  if (destinationFloor < 3) resolveCastleArrival(state, destination, events);
  else resolveDaimyoArrival(state);
}

export function courtierFloor(location: string): number {
  if (location === "gate") return 0;
  if (location.startsWith("steward-")) return 1;
  if (location.startsWith("diplomat-")) return 2;
  if (location === "daimyo") return 3;
  return -1;
}

function resolveCastleArrival(state: GameState, roomId: string, events: GameEvent[]): void {
  const player = state.players[state.currentPlayer];
  const room = state.board.castleRooms.find((candidate) => candidate.id === roomId);
  if (!room) throw new Error(`Unknown castle room ${roomId}.`);
  const cards = room.floor === "steward" ? STEWARD_CARDS : DIPLOMAT_CARDS;
  const arrivedCard = cards.find((card) => card.id === room.cardId);
  if (!arrivedCard) throw new Error(`Unknown ${room.floor} card ${room.cardId}.`);
  const deck = room.floor === "steward" ? state.board.stewardDeck : state.board.diplomatDeck;
  if (deck.length > 0) {
    const old = player.domainCard;
    player.lanternCards.push(`${old.kind}-${old.id}`);
    player.lanternEffects = lanternEffectsFromCards(player.lanternCards);
    events.push({ type: "CardMoved", player: player.id, card: `${old.kind}-${old.id}`, from: "domain", to: "lantern" });
    player.domainCard = { kind: room.floor, id: arrivedCard.id };
    events.push({ type: "CardMoved", player: player.id, card: `${room.floor}-${arrivedCard.id}`, from: room.id, to: "domain" });
  }
  const options = castleCardLightRows(arrivedCard).map((effects) => materialEffects(effects));
  const refresh: PendingEffect[] = deck.length > 0 ? [{ id: `refresh-${state.turn}-${room.id}`, source: room.id, effect: { type: "castleRefresh", room: room.id } }] : [];
  state.pendingEffects.unshift(
    { id: `arrival-${state.turn}-${room.id}`, source: room.id, effect: { type: "chooseOne", labels: options.map((_, index) => `浅色行动 ${index + 1}`), options } },
    ...refresh,
  );
}

function resolveCastleRefresh(state: GameState, roomId: string, events: GameEvent[]): void {
  const pending = state.pendingEffects[0];
  if (!pending || pending.effect.type !== "castleRefresh" || pending.effect.room !== roomId) throw new Error("No castle-room refresh is pending.");
  const room = state.board.castleRooms.find((candidate) => candidate.id === roomId);
  if (!room) throw new Error(`Unknown castle room ${roomId}.`);
  const deck = room.floor === "steward" ? state.board.stewardDeck : state.board.diplomatDeck;
  const nextCard = deck.shift();
  state.pendingEffects.shift();
  if (nextCard === undefined) return;
  room.cardId = nextCard;
  refreshCastleWorkspace(state, room.id);
  events.push({ type: "CardMoved", player: state.currentPlayer, card: `${room.floor}-${nextCard}`, from: `${room.floor}-deck`, to: room.id });
  events.push({ type: "EffectResolved", player: state.currentPlayer, source: pending.source, effect: pending.effect });
}

function resolveDaimyoArrival(state: GameState): void {
  const player = state.players[state.currentPlayer];
  const lantern = lanternPending("daimyo-lantern", player.lanternEffects, `daimyo-lantern-${state.turn}`);
  const positions = state.board.daimyoTaken.flatMap((owner, index) => owner === null ? [index] : []);
  const reward: PendingEffect[] = positions.length > 0 ? [{ id: `daimyo-reward-${state.turn}`, source: "daimyo", effect: { type: "daimyoReward", positions } }] : [];
  state.pendingEffects.unshift(...lantern, ...reward);
}

function refreshCastleWorkspace(state: GameState, roomId: string): void {
  const room = state.board.castleRooms.find((candidate) => candidate.id === roomId)!;
  const card = (room.floor === "steward" ? STEWARD_CARDS : DIPLOMAT_CARDS).find((candidate) => candidate.id === room.cardId)!;
  const rows = castleCardRowData(card);
  for (const [index, slot] of room.slots.entries()) {
    slot.rowId = rows[index].id;
    slot.effects = materialEffects(rows[index]?.effects ?? []);
  }
  const workspace = state.workspaces[`castle-${room.id}`];
  workspace.effectsByColor = {};
  workspace.effectGroupsByColor = {};
  for (const color of COLORS) {
    const groups = room.slots.filter((slot) => slot.color === color).map((slot) => ({ id: slot.rowId, effects: [...slot.effects] }));
    workspace.effectGroupsByColor[color] = groups;
    workspace.effectsByColor[color] = groups.flatMap((group) => group.effects);
  }
  workspace.allowedColors = COLORS.filter((color) => (workspace.effectGroupsByColor?.[color]?.length ?? 0) > 0);
}

function completeMajorActionWithEffects(state: GameState, pending: PendingEffect, effects: Effect[], events: GameEvent[]): void {
  state.pendingEffects.shift();
  const expanded = printedSequencePending(pending.source, effects, `${pending.id}-reward`, pending.owner);
  state.pendingEffects.unshift(...expanded);
  events.push({ type: "EffectResolved", player: state.currentPlayer, source: pending.source, effect: pending.effect });
}

function completeMajorActionWithActionGroups(state: GameState, pending: PendingEffect, groups: EffectGroup[], events: GameEvent[]): void {
  state.pendingEffects.shift();
  state.pendingEffects.unshift(...actionGroupsPending(pending.source, groups, `${pending.id}-reward`, pending.owner, true));
  events.push({ type: "EffectResolved", player: state.currentPlayer, source: pending.source, effect: pending.effect });
}

function moveMember(player: number, member: PlayerState["members"][number], to: string, events: GameEvent[]): void {
  const from = member.location;
  member.location = to;
  events.push({ type: "MemberMoved", player, member: member.id, from, to });
}

function placeDie(state: GameState, workspaceId: string, events: GameEvent[]): void {
  const drafted = state.draftedDie;
  if (!drafted) throw new Error("Cannot place a die before drafting one.");
  const workspace = state.workspaces[workspaceId];
  const coinDelta = placementCoinDelta(workspace, drafted.die);
  workspace.dice.push(drafted.die);
  events.push({ type: "DiePlaced", player: state.currentPlayer, die: drafted.die, workspace: workspaceId });
  changeResource(state, state.currentPlayer, "coins", coinDelta, events);
  const domainRow = domainRowFromWorkspace(workspaceId);
  if (domainRow) state.usedDomainRowsThisTurn.push(domainRow);
  const workspaceEffects = domainRow ? domainRowEffects(state, state.currentPlayer, domainRow) : workspace.effectsByColor?.[drafted.die.color] ?? workspace.effects;
  const castleGroups = workspace.kind === "castle" ? workspace.effectGroupsByColor?.[drafted.die.color] : undefined;
  const lanternEffects = drafted.lanternTriggered ? state.players[state.currentPlayer].lanternEffects : [];
  state.pendingEffects = [
    ...lanternPending("lantern", lanternEffects, `turn-${state.turn}-lantern`),
    ...(castleGroups ? actionGroupsPending(workspace.id, castleGroups, `turn-${state.turn}-${workspace.id}`) : printedSequencePending(workspace.id, workspaceEffects, `turn-${state.turn}-${workspace.id}`)),
  ];
  delete state.draftedDie;
  state.phase = "resolve";
}

function drainAutomaticEffects(state: GameState, events: GameEvent[]): void {
  while (state.pendingEffects.length > 0) {
    const pending = state.pendingEffects[0];
    if (pending.owner !== undefined && state.currentPlayer !== pending.owner) {
      state.currentPlayer = pending.owner;
      state.usedDomainRowsThisTurn = [];
    }
    if (pending.effect.type === "majorAction"
      && pending.effect.action === "courtier"
      && pending.completedSubactions?.includes("recruit")
      && pending.completedSubactions.includes("promote")) {
      state.pendingEffects.shift();
      events.push({ type: "EffectResolved", player: state.currentPlayer, source: pending.source, effect: pending.effect });
      continue;
    }
    if (pending.effect.type === "domainAction" && availableDomainRows(state).length === 0 || pending.effect.type === "castleTileAction" && availableCastleTileActions(state, pending.effect.color).length === 0) {
      state.pendingEffects.shift();
      events.push({ type: "EffectResolved", player: state.currentPlayer, source: pending.source, effect: pending.effect });
      continue;
    }
    if (["gainChoice", "chooseOne", "effectOrder", "actionOrder", "majorAction", "castleRefresh", "pay", "domainAction", "castleTileAction", "daimyoReward", "gardenActivation"].includes(pending.effect.type) || (pending.effect.type === "influence" && nextInfluenceCheckpoint(state, pending.effect.amount))) {
      events.push({ type: "ChoiceRequired", player: state.currentPlayer, effect: pending });
      return;
    }
    state.pendingEffects.shift();
    resolveAtomicEffect(state, pending.effect, pending.source, events);
  }
  if (state.phase === "roundEnd") finishRound(state, events);
}

export function stabilizeAutomaticEffects(state: GameState, events: GameEvent[] = []): GameEvent[] {
  drainAutomaticEffects(state, events);
  return events;
}

function domainRowFromWorkspace(workspaceId: string): import("./types").MajorAction | undefined {
  const match = workspaceId.match(/-domain-(courtier|gardener|warrior)$/);
  return match?.[1] as import("./types").MajorAction | undefined;
}

export function availableDomainRows(state: GameState): import("./types").MajorAction[] {
  return (["courtier", "gardener", "warrior"] as import("./types").MajorAction[]).filter((row) => !state.usedDomainRowsThisTurn.includes(row));
}

function visibleDomainRewards(player: PlayerState, row: import("./types").MajorAction): Effect[] {
  const rewards: Record<import("./types").MajorAction, Effect[][]> = {
    courtier: [
      [{ type: "gain", resource: "food", amount: 1 }],
      [{ type: "gain", resource: "food", amount: 1 }],
      [{ type: "gain", resource: "food", amount: 1 }],
      [{ type: "gain", resource: "coins", amount: 2 }],
      [{ type: "gain", resource: "food", amount: 1 }],
      [{ type: "influence", amount: 1 }],
    ],
    gardener: [
      [{ type: "gain", resource: "iron", amount: 1 }],
      [{ type: "gain", resource: "iron", amount: 1 }],
      [{ type: "gain", resource: "iron", amount: 1 }],
      [{ type: "lantern" }],
      [{ type: "gain", resource: "iron", amount: 1 }],
      [{ type: "gain", resource: "coins", amount: 2 }],
    ],
    warrior: [
      [{ type: "gain", resource: "pearl", amount: 1 }],
      [{ type: "gain", resource: "pearl", amount: 1 }],
      [{ type: "gain", resource: "pearl", amount: 1 }],
      [{ type: "influence", amount: 1 }],
      [{ type: "gain", resource: "pearl", amount: 1 }],
      [{ type: "lantern" }],
    ],
  };
  const remaining = player.members.filter((member) => member.type === row && member.location === "domain").length;
  return rewards[row].slice(0, 6 - remaining).flat();
}

function domainCardRowEffects(player: PlayerState, row: import("./types").MajorAction): Effect[] {
  if (player.domainCard.kind === "starting") {
    const card = STARTING_ACTION_CARDS.find((candidate) => candidate.id === player.domainCard.id);
    return card?.effect.type === "majorAction" && card.effect.action === row ? materialEffects([card.effect]) : [];
  }
  const card = (player.domainCard.kind === "steward" ? STEWARD_CARDS : DIPLOMAT_CARDS).find((candidate) => candidate.id === player.domainCard.id);
  if (!card) return [];
  const actions = castleCardRows(card);
  const printedRow = castleCardDomainRowGroups(card).findIndex((domainRows) => domainRows.includes(row));
  return materialEffects(actions[printedRow] ?? []);
}

export function domainRowRewardSources(state: GameState, playerId: number, row: import("./types").MajorAction): { queue: Effect[]; card: Effect[] } {
  const player = state.players[playerId];
  return { queue: visibleDomainRewards(player, row), card: domainCardRowEffects(player, row) };
}

export function domainRowEffects(state: GameState, playerId: number, row: import("./types").MajorAction): Effect[] {
  const sources = domainRowRewardSources(state, playerId, row);
  return [...sources.queue, ...sources.card];
}

export function domainCardLanternEffects(player: PlayerState): Effect[] {
  return lanternEffectsFromCards([`${player.domainCard.kind}-${player.domainCard.id}`]);
}

export function availableCastleTileActions(state: GameState, selector: CastleActionSelector): { room: string; rowId: string; source: string; effects: Effect[] }[] {
  return state.board.castleRooms.flatMap((room) => {
    const material = (room.floor === "steward" ? STEWARD_CARDS : DIPLOMAT_CARDS)
      .find((card) => card.id === room.cardId);
    return room.slots.flatMap((slot, index) => {
      const matches = selector === "any"
        || (selector === "light" ? material?.rows[index]?.tone === "light" : slot.color === selector);
      return matches ? [{
        room: room.id,
        rowId: slot.rowId,
        source: `${room.id}:${slot.rowId}`,
        effects: [...slot.effects],
      }] : [];
    });
  });
}

function resolveCastleTileChoice(state: GameState, effectId: string, roomId: string, rowId: string, events: GameEvent[]): void {
  const pending = state.pendingEffects.shift();
  if (!pending || pending.id !== effectId || pending.effect.type !== "castleTileAction") throw new Error("No castle die-tile action is pending.");
  const selected = availableCastleTileActions(state, pending.effect.color).find((target) => target.room === roomId && target.rowId === rowId);
  if (!selected) throw new Error("Castle die-tile action is unavailable.");
  state.pendingEffects.unshift(...printedSequencePending(selected.source, selected.effects, `${pending.id}-${selected.room}-${selected.rowId}`, pending.owner));
  events.push({ type: "EffectResolved", player: state.currentPlayer, source: pending.source, effect: pending.effect });
}

function moveInfluenceMarkerToTop(state: GameState, playerId: number, moved: number): void {
  if (moved <= 0) return;
  state.influenceStackOrder = state.influenceStackOrder.filter((id) => id !== playerId);
  state.influenceStackOrder.push(playerId);
}

function resolveAtomicEffect(state: GameState, effect: Effect, source: string, events: GameEvent[]): void {
  if (effect.type === "gain") changeResource(state, state.currentPlayer, effect.resource, effect.amount, events);
  else if (effect.type === "gainPoints") state.players[state.currentPlayer].points += effect.amount;
  else if (effect.type === "influence") {
    const player = state.players[state.currentPlayer];
    const before = player.influence;
    player.influence = Math.min(INFLUENCE_TRACK_END, before + effect.amount);
    const moved = player.influence - before;
    moveInfluenceMarkerToTop(state, player.id, moved);
    events.push({ type: "InfluenceChanged", player: player.id, amount: moved, total: player.influence });
  } else if (effect.type === "lantern") {
    const expanded = lanternPending("lantern", state.players[state.currentPlayer].lanternEffects, `lantern-${state.turn}`);
    state.pendingEffects.unshift(...expanded);
  } else if (effect.type === "wellAction") {
    const wellSpace = Object.values(state.workspaces).find((workspace) => workspace.kind === "well");
    if (!wellSpace) throw new Error("Well action has no configured well.");
    const expanded = printedSequencePending("well", wellSpace.effects, `well-${state.turn}`);
    state.pendingEffects.unshift(...expanded);
  } else throw new Error(`Effect ${effect.type} is not atomic.`);
  events.push({ type: "EffectResolved", player: state.currentPlayer, source, effect });
}

function resolveChoice(state: GameState, effectId: string, option: number, events: GameEvent[]): void {
  const pending = state.pendingEffects.shift();
  if (!pending || pending.id !== effectId) throw new Error("The selected effect is no longer pending.");
  if (pending.effect.type === "effectOrder") {
    const selected = pending.effect.effects[option];
    if (!selected) throw new Error("Ordered effect is unavailable.");
    const remaining = pending.effect.effects.filter((_, index) => index !== option);
    state.pendingEffects.unshift(
      ...orderedPending(pending.source, selected ? [selected] : [], `${pending.id}-selected-${option}`, pending.owner),
      ...orderedPending(pending.source, remaining, `${pending.id}-remaining`, pending.owner),
    );
    events.push({ type: "EffectResolved", player: state.currentPlayer, source: pending.source, effect: pending.effect });
    return;
  }
  if (pending.effect.type === "actionOrder") {
    const selected = pending.effect.groups[option];
    if (!selected) throw new Error("Ordered action group is unavailable.");
    const remaining = pending.effect.groups.filter((_, index) => index !== option);
    state.pendingEffects.unshift(
      ...printedSequencePending(pending.source, selected.effects, `${pending.id}-selected-${option}`, pending.owner),
      ...actionGroupsPending(pending.source, remaining, `${pending.id}-remaining`, pending.owner, pending.effect.groups.every((group) => group.id.startsWith("yard-tile-"))),
    );
    events.push({ type: "EffectResolved", player: state.currentPlayer, source: pending.source, effect: pending.effect });
    return;
  }
  if (pending.effect.type === "gainChoice") {
    const resource = pending.effect.resources[option];
    resolveAtomicEffect(state, { type: "gain", resource, amount: 1 }, pending.source, events);
    if (pending.effect.amount > 1) {
      state.pendingEffects.unshift({
        ...pending,
        effect: { ...pending.effect, amount: pending.effect.amount - 1 },
      });
    }
    return;
  }
  if (pending.effect.type === "chooseOne") {
    const selected = pending.effect.options[option];
    const expanded = printedSequencePending(pending.source, selected, `${pending.id}-option-${option}`, pending.owner);
    state.pendingEffects.unshift(...expanded);
    events.push({ type: "EffectResolved", player: state.currentPlayer, source: pending.source, effect: pending.effect });
    return;
  }
  if (pending.effect.type === "daimyoReward") {
    const position = pending.effect.positions[option];
    if (position === undefined || state.board.daimyoTaken[position] !== null) throw new Error("Daimyo reward is unavailable.");
    state.board.daimyoTaken[position] = state.currentPlayer;
    const card = DAIMYO_CARDS.find((candidate) => candidate.id === state.board.daimyoCard)!;
    const expanded = printedSequencePending(pending.source, materialEffects(card.rewards[position]), `${pending.id}-position-${position}`, pending.owner);
    state.pendingEffects.unshift(...expanded);
    events.push({ type: "EffectResolved", player: state.currentPlayer, source: pending.source, effect: pending.effect });
    return;
  }
  if (pending.effect.type === "domainAction") {
    const row = availableDomainRows(state)[option];
    if (!row) throw new Error("Personal-domain row is unavailable.");
    state.usedDomainRowsThisTurn.push(row);
    const effects = domainRowEffects(state, state.currentPlayer, row);
    const expanded = printedSequencePending(`domain-${row}`, effects, `${pending.id}-${row}`, pending.owner);
    state.pendingEffects.unshift(...expanded);
    events.push({ type: "EffectResolved", player: state.currentPlayer, source: pending.source, effect: pending.effect });
    return;
  }
  if (pending.effect.type === "gardenActivation") {
    const gardenId = pending.effect.gardens[option];
    const garden = state.gardens.find((candidate) => candidate.id === gardenId);
    if (!garden) throw new Error("Garden activation is unavailable.");
    const remaining = pending.effect.gardens.filter((id) => id !== gardenId);
    if (remaining.length > 0) state.pendingEffects.unshift({ id: `${pending.id}-remaining`, source: "round-end-gardens", effect: { type: "gardenActivation", gardens: remaining }, owner: pending.owner });
    const expanded = printedSequencePending(garden.id, garden.effects, `${pending.id}-${garden.id}`, pending.owner);
    state.pendingEffects.unshift(...expanded);
    events.push({ type: "EffectResolved", player: state.currentPlayer, source: pending.source, effect: pending.effect });
    return;
  }
  if (pending.effect.type === "pay") {
    if (option === 0) {
      changeResource(state, state.currentPlayer, pending.effect.resource, -pending.effect.amount, events);
      const expanded = printedSequencePending(pending.source, pending.effect.effects, `${pending.id}-paid`, pending.owner);
      state.pendingEffects.unshift(...expanded);
    } else if (option !== 1 || !pending.effect.optional) throw new Error("This payment cannot be skipped.");
    events.push({ type: "EffectResolved", player: state.currentPlayer, source: pending.source, effect: pending.effect });
    return;
  }
  if (pending.effect.type === "influence") {
    const checkpoint = nextInfluenceCheckpoint(state, pending.effect.amount);
    if (!checkpoint) throw new Error("Influence effect does not need a checkpoint choice.");
    const player = state.players[state.currentPlayer];
    const target = player.influence + pending.effect.amount;
    if (option === 0) {
      changeResource(state, player.id, "seals", -checkpoint.cost, events);
      const moved = checkpoint.position - player.influence;
      player.influence = checkpoint.position;
      moveInfluenceMarkerToTop(state, player.id, moved);
      events.push({ type: "InfluenceChanged", player: player.id, amount: moved, total: player.influence });
      const remaining = target - checkpoint.position;
      if (remaining > 0) state.pendingEffects.unshift({ id: `${pending.id}-after-${checkpoint.position}`, source: pending.source, effect: { type: "influence", amount: remaining } });
    } else if (option === 1) {
      const destination = checkpoint.position - 1;
      const moved = Math.max(0, destination - player.influence);
      player.influence = destination;
      moveInfluenceMarkerToTop(state, player.id, moved);
      events.push({ type: "InfluenceChanged", player: player.id, amount: moved, total: player.influence });
    } else throw new Error("Invalid checkpoint choice.");
    events.push({ type: "EffectResolved", player: state.currentPlayer, source: pending.source, effect: pending.effect });
    return;
  }
  throw new Error("Pending effect does not accept an option.");
}

function changeResource(state: GameState, playerId: number, resource: Resource, requested: number, events: GameEvent[]): void {
  const player = state.players[playerId];
  const before = player.resources[resource];
  const maximum = resource === "coins" ? Number.POSITIVE_INFINITY : RESOURCE_CAPS[resource];
  const after = Math.max(0, Math.min(maximum, before + requested));
  if (before + requested < 0) throw new Error(`Player ${playerId} cannot pay ${Math.abs(requested)} ${resource}.`);
  player.resources[resource] = after;
  events.push({ type: "ResourceChanged", player: playerId, resource, amount: after - before, total: after });
}

function nextInfluenceCheckpoint(state: GameState, amount: number) {
  const current = state.players[state.currentPlayer].influence;
  const target = current + amount;
  return INFLUENCE_CHECKPOINTS.find((checkpoint) => current < checkpoint.position && target >= checkpoint.position);
}

function canPlace(state: GameState, workspace: Workspace): boolean {
  const die = state.draftedDie?.die;
  if (!die || !workspace.active || !workspace.allowedColors.includes(die.color)) return false;
  if (workspace.owner !== undefined && workspace.owner !== state.currentPlayer) return false;
  if (workspace.capacity !== "unlimited" && workspace.dice.length >= workspace.capacity) return false;
  const domainRow = domainRowFromWorkspace(workspace.id);
  if (domainRow && state.usedDomainRowsThisTurn.includes(domainRow)) return false;
  return state.players[state.currentPlayer].resources.coins >= Math.max(0, -placementCoinDelta(workspace, die));
}

function removeBridgeEnd(dice: Die[], end: BridgeEnd): Die {
  const die = end === "left" ? dice.shift() : dice.pop();
  if (!die) throw new Error("Cannot draft from an empty bridge.");
  return die;
}

function finishTurn(state: GameState, events: GameEvent[]): void {
  state.turnsThisRound += 1;
  if (COLORS.reduce((total, color) => total + state.bridges[color].length, 0) === 3) {
    beginRoundEnd(state, events);
    return;
  }
  const orderIndex = state.turnOrder.indexOf(state.currentPlayer);
  state.currentPlayer = state.turnOrder[(orderIndex + 1) % state.turnOrder.length];
  state.turn += 1;
  state.usedDomainRowsThisTurn = [];
  state.phase = "draft";
  events.push({ type: "TurnStarted", player: state.currentPlayer, turn: state.turn });
}

function beginRoundEnd(state: GameState, events: GameEvent[]): void {
  events.push({ type: "RoundEnded", round: state.round });
  state.turnOrder = [...state.turnOrder].sort((left, right) => state.players[right].influence - state.players[left].influence || state.influenceStackOrder.indexOf(right) - state.influenceStackOrder.indexOf(left));
  state.phase = "roundEnd";
  state.usedDomainRowsThisTurn = [];
  if (state.round === 3) {
    finishRound(state, events);
    return;
  }
  const activeBridges = new Set(COLORS.filter((color) => state.bridges[color].length > 0));
  state.pendingEffects = state.turnOrder.flatMap((player) => {
    const gardens = state.gardens
      .filter((garden) => activeBridges.has(garden.bridge) && garden.gardeners.includes(player))
      .map((garden) => garden.id);
    return gardens.length > 0 ? [{
      id: `round-${state.round}-gardens-p${player}`,
      source: "round-end-gardens",
      effect: { type: "gardenActivation", gardens } as Effect,
      owner: player,
    }] : [];
  });
  drainAutomaticEffects(state, events);
}

function finishRound(state: GameState, events: GameEvent[]): void {
  if (state.round === 3) {
    const scores = scoreGame(state);
    const winner = determineWinner(state, scores);
    state.scores = scores;
    state.winner = winner;
    state.phase = "finished";
    events.push({ type: "GameScored", scores, winner });
    return;
  }
  state.round += 1;
  state.turn += 1;
  state.turnsThisRound = 0;
  state.currentPlayer = state.turnOrder[0];
  state.bridges = rollBridges(state.setup.seed, state.round, state.playerCount);
  for (const workspace of Object.values(state.workspaces)) workspace.dice = [];
  state.phase = "draft";
  state.usedDomainRowsThisTurn = [];
  events.push({ type: "RoundStarted", round: state.round, firstPlayer: state.currentPlayer });
  events.push({ type: "TurnStarted", player: state.currentPlayer, turn: state.turn });
}

export function serializeGame(state: GameState): string {
  return JSON.stringify(state);
}

function migrateLegacyCastleActionGroups(state: GameState): void {
  for (const pending of state.pendingEffects) {
    if (!pending.source.startsWith("castle-")) continue;
    const workspace = state.workspaces[pending.source];
    const die = workspace?.dice.at(-1);
    const groups = die ? workspace.effectGroupsByColor?.[die.color] ?? [] : [];
    if (pending.effect.type === "actionOrder") {
      const legacyGroups = pending.effect.groups as unknown as (EffectGroup | Effect[])[];
      if (legacyGroups.some((group) => Array.isArray(group))) {
        pending.effect.groups = legacyGroups.flatMap((legacy) => {
          const effects = Array.isArray(legacy) ? legacy : legacy.effects;
          const match = groups.find((group) => JSON.stringify(group.effects) === JSON.stringify(effects));
          return match ? [structuredClone(match)] : [];
        });
      }
      continue;
    }
    if (pending.effect.type !== "effectOrder") continue;
    for (let mask = 1; mask < 1 << groups.length; mask += 1) {
      const selected = groups.filter((_, index) => (mask & (1 << index)) !== 0);
      if (JSON.stringify(selected.flatMap((group) => group.effects)) === JSON.stringify(pending.effect.effects)) {
        pending.effect = { type: "actionOrder", groups: structuredClone(selected) };
        break;
      }
    }
  }
}

function migrateLegacyStartingResourceChoices(state: GameState): void {
  const migrated: PendingEffect[] = [];
  for (let index = 0; index < state.pendingEffects.length;) {
    const pending = state.pendingEffects[index];
    if (!/^setup-p\d+-resource-\d+$/.test(pending.id) || pending.effect.type !== "gainChoice" || pending.effect.amount !== 1) {
      migrated.push(pending);
      index += 1;
      continue;
    }
    const legacyEffect = pending.effect;
    const group: PendingEffect[] = [];
    while (index < state.pendingEffects.length) {
      const candidate = state.pendingEffects[index];
      if (!/^setup-p\d+-resource-\d+$/.test(candidate.id) || candidate.source !== pending.source || candidate.owner !== pending.owner || candidate.effect.type !== "gainChoice" || candidate.effect.amount !== 1) break;
      group.push(candidate);
      index += 1;
    }
    migrated.push({
      ...pending,
      id: pending.id.replace(/-resource-\d+$/, "-resources"),
      effect: { ...legacyEffect, amount: group.length },
    });
  }
  state.pendingEffects = migrated;
}

function migrateLegacyTrainingYardRewardChoices(state: GameState): void {
  const effectsByGroupId = new Map<string, Effect>(state.trainingYards.flatMap((yard) => yard.tileIds.map((tileId, index) => [
    `yard-tile-${tileId}`,
    yard.effects[index],
  ] as const)));
  for (const pending of state.pendingEffects) {
    if (pending.effect.type !== "actionOrder") continue;
    pending.effect.groups = pending.effect.groups.map((group) => {
      const migratedEffect = effectsByGroupId.get(group.id);
      return migratedEffect ? { id: group.id, effects: [structuredClone(migratedEffect)] } : group;
    });
  }
  const lastAction = state.actionHistory.at(-1);
  let confirmedTarget: Extract<GameAction, { type: "selectMajorActionTarget" }> | undefined;
  if (lastAction?.type === "confirmMajorAction") {
    for (let index = state.actionHistory.length - 2; index >= 0; index -= 1) {
      const action = state.actionHistory[index];
      if (action.type === "selectMajorActionTarget" && action.target.startsWith("yard-") && !confirmedTarget) confirmedTarget = action;
      if (action.type === "beginMajorAction") {
        if (action.mode !== "warrior") confirmedTarget = undefined;
        break;
      }
    }
  }
  const confirmedYard = confirmedTarget?.type === "selectMajorActionTarget"
    ? state.trainingYards.find((yard) => yard.id === confirmedTarget.target)
    : undefined;
  for (let index = 0; index < state.pendingEffects.length; index += 1) {
    const first = state.pendingEffects[index];
    const match = first.id.match(/^(.*-reward)-1$/);
    if (!match) continue;
    const matchesPendingIds = (candidate: TrainingYard) => candidate.effects.every((_, offset) => state.pendingEffects[index + offset]?.id === `${match[1]}-${offset + 1}`);
    const yard = confirmedYard && matchesPendingIds(confirmedYard) ? confirmedYard : state.trainingYards.find((candidate) => candidate.effects.length > 1
      && candidate.effects.every((effect, offset) => {
        const pending = state.pendingEffects[index + offset];
        return pending?.id === `${match[1]}-${offset + 1}` && JSON.stringify(pending.effect) === JSON.stringify(effect);
      }));
    if (!yard) continue;
    if (yard.effects.length === 1) {
      state.pendingEffects[index] = { ...first, effect: structuredClone(yard.effects[0]) };
      continue;
    }
    const groups = yard.effects.map((effect, offset) => ({ id: `yard-tile-${yard.tileIds[offset]}`, effects: [effect] }));
    state.pendingEffects.splice(index, yard.effects.length, {
      id: `${match[1]}-actions`, source: first.source, owner: first.owner, effect: { type: "actionOrder", groups },
    });
  }
}

function validateRestoredGameState(state: GameState): void {
  if (state.version !== 1 || !isPlayerCount(state.playerCount) || state.players.length !== state.playerCount) throw new Error("Unsupported or invalid game snapshot.");
  if (!isSeat(state, state.currentPlayer) || !["setup", "draft", "place", "resolve", "roundEnd", "finished"].includes(state.phase)) throw new Error("Invalid White Castle actor or phase.");
  const isSeatOrder = (value: unknown): value is number[] => Array.isArray(value) && value.length === state.playerCount && new Set(value).size === state.playerCount && value.every((seat) => isSeat(state, seat));
  if (!isSeatOrder(state.turnOrder) || !isSeatOrder(state.influenceStackOrder) || !isSeatOrder(state.setupOrder)) throw new Error("Invalid White Castle turn order.");
  if (!Number.isInteger(state.round) || state.round < 1 || state.round > 3 || !Number.isInteger(state.turn) || state.turn < 1) throw new Error("Invalid White Castle round or turn.");

  const expectedOffers = createStartingOffers(state.setup.seed, state.playerCount);
  if (!Array.isArray(state.startingOffers) || state.startingOffers.length !== expectedOffers.length
    || state.startingOffers.some((offer, index) => (
      !offer || offer.resourceCard !== expectedOffers[index].resourceCard
      || offer.actionCard !== expectedOffers[index].actionCard
      || (offer.claimedBy !== null && !isSeat(state, offer.claimedBy))
    ))) throw new Error("Invalid White Castle starting offers.");

  const expectedSetupTurnOrder = seededShuffle(state.players.map((_, seat) => seat), state.setup.seed + 59);
  const expectedSetupOrder = [...expectedSetupTurnOrder].reverse();
  if (canonicalFingerprint(state.setup.turnOrder) !== canonicalFingerprint(expectedSetupTurnOrder)
    || canonicalFingerprint(state.setupOrder) !== canonicalFingerprint(expectedSetupOrder)
    || !Array.isArray(state.setup.startingSetIds)
    || !Number.isInteger(state.setupIndex) || state.setupIndex < 0 || state.setupIndex > state.setupOrder.length) {
    throw new Error("Invalid White Castle starting ownership.");
  }
  const offerBySetId = new Map(state.startingOffers.map((offer) => [startingSetId(offer), offer]));
  const recordedSets = state.setup.startingSetIds;
  if (recordedSets.length > state.setupOrder.length
    || new Set(recordedSets).size !== recordedSets.length
    || recordedSets.some((setId) => !offerBySetId.has(setId))) {
    throw new Error("Invalid White Castle starting ownership.");
  }
  const recordedSetIds = new Set(recordedSets);
  for (const [index, setId] of recordedSets.entries()) {
    if (offerBySetId.get(setId)?.claimedBy !== state.setupOrder[index]) {
      throw new Error("Invalid White Castle starting ownership.");
    }
  }
  if (state.startingOffers.some((offer) => (
    recordedSetIds.has(startingSetId(offer)) !== (offer.claimedBy !== null)
  ))) throw new Error("Invalid White Castle starting ownership.");

  for (const player of state.players) {
    const offer = state.startingOffers.find((candidate) => candidate.claimedBy === player.id);
    const resourceCards = player.lanternCards.flatMap((card) => {
      const match = card.match(/^starting-resource-([0-9]+)$/);
      return match ? [Number(match[1])] : [];
    });
    const physicalActionCards = [
      ...(player.domainCard.kind === "starting" && player.domainCard.id > 0 ? [player.domainCard.id] : []),
      ...player.lanternCards.flatMap((card) => {
        const match = card.match(/^starting-([0-9]+)$/);
        return match ? [Number(match[1])] : [];
      }),
    ];
    const decreeCards = player.lanternCards.filter((card) => card.startsWith("decree-")).sort();
    const expectedDecrees = offer
      ? STARTING_RESOURCE_CARDS.find((card) => card.id === offer.resourceCard)?.decree
      : undefined;
    const expectedActionCards:PlayerState["actionCards"] = {
      courtier:"", gardener:"", warrior:"",
    };
    const startingAction = offer
      ? STARTING_ACTION_CARDS.find((card) => card.id === offer.actionCard)
      : undefined;
    if (startingAction?.effect.type === "majorAction") {
      expectedActionCards[startingAction.effect.action] = `starting-action-${startingAction.id}`;
    }
    if (canonicalFingerprint(resourceCards) !== canonicalFingerprint(offer ? [offer.resourceCard] : [])
      || canonicalFingerprint(physicalActionCards) !== canonicalFingerprint(offer ? [offer.actionCard] : [])
      || canonicalFingerprint(decreeCards) !== canonicalFingerprint(expectedDecrees ? [`decree-${expectedDecrees}`] : [])
      || canonicalFingerprint(player.actionCards) !== canonicalFingerprint(expectedActionCards)) {
      throw new Error("Invalid White Castle starting ownership.");
    }
  }

  if (state.phase === "setup") {
    if (state.setupIndex >= state.setupOrder.length || state.currentPlayer !== state.setupOrder[state.setupIndex]
      || (recordedSets.length !== state.setupIndex && recordedSets.length !== state.setupIndex + 1)) {
      throw new Error("Invalid White Castle starting ownership.");
    }
    const currentClaimPending = recordedSets.length === state.setupIndex + 1;
    const setupChoice = state.pendingEffects[0];
    if (currentClaimPending !== Boolean(
      setupChoice?.id === `setup-p${state.currentPlayer}-resources`
      && setupChoice.owner === state.currentPlayer
      && setupChoice.effect.type === "gainChoice"
    )) throw new Error("Invalid White Castle starting ownership.");
  } else if (state.setupIndex !== state.setupOrder.length
    || recordedSets.length !== state.setupOrder.length) {
    throw new Error("Invalid White Castle starting ownership.");
  }

  if (!state.board || !Array.isArray(state.gardens) || !Array.isArray(state.trainingYards)) throw new Error("Invalid White Castle board containers.");
  const gardenIds = new Set(state.gardens.map((garden) => garden.id));
  const yardIds = new Set(state.trainingYards.map((yard) => yard.id));
  const roomIds = new Set(state.board.castleRooms.map((room) => room.id));

  const assertCastleCardPartition = (kind: "steward" | "diplomat", material: typeof STEWARD_CARDS, deck: number[]): void => {
    const refreshingRooms = new Set(state.pendingEffects.flatMap((pending) => pending.effect.type === "castleRefresh" ? [pending.effect.room] : []));
    const visible = state.board.castleRooms.filter((room) => room.floor === kind && !refreshingRooms.has(room.id)).map((room) => room.cardId);
    const held = state.players.flatMap((player) => [
      ...(player.domainCard.kind === kind ? [player.domainCard.id] : []),
      ...player.lanternCards.flatMap((card) => {
        const match = card.match(new RegExp(`^${kind}-(\\d+)$`));
        return match ? [Number(match[1])] : [];
      }),
    ]);
    const actual = [...visible, ...deck, ...held].sort((left, right) => left - right);
    const expected = material.filter((card) => state.playerCount > 2 || !card.excludedAtTwoPlayers).map((card) => card.id).sort((left, right) => left - right);
    if (actual.length !== new Set(actual).size || JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error(`Invalid White Castle ${kind} card partition.`);
  };
  assertCastleCardPartition("steward", STEWARD_CARDS, state.board.stewardDeck);
  assertCastleCardPartition("diplomat", DIPLOMAT_CARDS, state.board.diplomatDeck);
  if (state.board.gardenCardIds.length !== 6 || new Set(state.board.gardenCardIds).size !== 6 || state.board.gardenCardIds.some((id) => !GARDEN_CARDS.some((card) => card.id === id))) throw new Error("Invalid White Castle garden card setup.");
  if (state.board.yardTileIds.length !== 4 || new Set(state.board.yardTileIds).size !== 4 || state.board.yardTileIds.some((id) => !YARD_TILES.some((tile) => tile.id === id))) throw new Error("Invalid White Castle yard tile setup.");
  if (!DAIMYO_CARDS.some((card) => card.id === state.board.daimyoCard)) throw new Error("Invalid White Castle Daimyo card.");

  for (const [seat, player] of state.players.entries()) {
    if (player.id !== seat || !Number.isFinite(player.points) || player.points < 0 || !Number.isInteger(player.influence) || player.influence < 0 || player.influence > INFLUENCE_TRACK_END) throw new Error("Invalid White Castle player track.");
    const resources = player.resources;
    if (!Number.isInteger(resources.coins) || resources.coins < 0
      || !Number.isInteger(resources.seals) || resources.seals < 0 || resources.seals > 5
      || ([resources.food, resources.iron, resources.pearl].some((amount) => !Number.isInteger(amount) || amount < 0 || amount > 7))) throw new Error("Invalid White Castle resources.");
    if (!Array.isArray(player.members) || player.members.length !== 15 || new Set(player.members.map((member) => member.id)).size !== 15) throw new Error("Invalid White Castle members.");
    for (const type of ["courtier", "gardener", "warrior"] as const) {
      if (player.members.filter((member) => member.type === type).length !== 5) throw new Error("Invalid White Castle members.");
    }
    for (const member of player.members) {
      const validLocation = member.type === "courtier"
        ? member.location === "domain" || member.location === "gate" || member.location === "daimyo" || roomIds.has(member.location)
        : member.type === "gardener" ? member.location === "domain" || gardenIds.has(member.location)
          : member.location === "domain" || yardIds.has(member.location);
      if (!validLocation) throw new Error("Invalid White Castle member location.");
    }
  }

  for (const garden of state.gardens) {
    if (!Array.isArray(garden.gardeners) || new Set(garden.gardeners).size !== garden.gardeners.length || garden.gardeners.some((owner) => !isSeat(state, owner))) throw new Error("Invalid White Castle garden occupants.");
    for (const player of state.players) {
      const members = player.members.filter((member) => member.type === "gardener" && member.location === garden.id).length;
      const entries = garden.gardeners.filter((owner) => owner === player.id).length;
      if (members !== entries) throw new Error("Invalid White Castle garden occupants.");
    }
  }
  for (const yard of state.trainingYards) {
    if (!Array.isArray(yard.warriors) || yard.warriors.some((owner) => !isSeat(state, owner))) throw new Error("Invalid White Castle yard occupants.");
    for (const player of state.players) {
      const members = player.members.filter((member) => member.type === "warrior" && member.location === yard.id).length;
      const entries = yard.warriors.filter((owner) => owner === player.id).length;
      if (members !== entries) throw new Error("Invalid White Castle yard occupants.");
    }
  }
  if (!Array.isArray(state.board.daimyoTaken) || state.board.daimyoTaken.length !== 3 || state.board.daimyoTaken.some((owner) => owner !== null && !isSeat(state, owner))) throw new Error("Invalid White Castle Daimyo claims.");
  for (const player of state.players) {
    const claims = state.board.daimyoTaken.filter((owner) => owner === player.id).length;
    const courtiers = player.members.filter((member) => member.type === "courtier" && member.location === "daimyo").length;
    if (claims > courtiers) throw new Error("Invalid White Castle Daimyo claims.");
  }

  if (!state.bridges || !state.workspaces || !Array.isArray(state.pendingEffects) || !Array.isArray(state.actionHistory)) throw new Error("Invalid White Castle state shape.");
  for (const color of COLORS) {
    if (!Array.isArray(state.bridges[color])
      || state.bridges[color].some((die) => die.color !== color)
      || state.bridges[color].some((die, index, dice) => index > 0 && dice[index - 1].value > die.value)) throw new Error("Invalid White Castle bridge dice.");
  }
  for (const workspace of Object.values(state.workspaces)) {
    if (!Array.isArray(workspace.dice)
      || (workspace.capacity !== "unlimited" && workspace.dice.length > workspace.capacity)
      || workspace.dice.some((die) => !workspace.allowedColors.includes(die.color))
      || (workspace.owner !== undefined && !isSeat(state, workspace.owner))) throw new Error("Invalid White Castle workspace dice.");
  }
  const dice = [
    ...COLORS.flatMap((color) => Array.isArray(state.bridges[color]) ? state.bridges[color] : []),
    ...Object.values(state.workspaces).flatMap((workspace) => Array.isArray(workspace.dice) ? workspace.dice : []),
    ...(state.draftedDie ? [state.draftedDie.die] : []),
  ];
  if (dice.length !== 3 * (state.playerCount + 1) || new Set(dice.map((die) => die.id)).size !== dice.length || dice.some((die) => !COLORS.includes(die.color) || !Number.isInteger(die.value) || die.value < 1 || die.value > 6)) throw new Error("Invalid White Castle dice inventory.");
  if (COLORS.some((color) => dice.filter((die) => die.color === color).length !== state.playerCount + 1)) throw new Error("Invalid White Castle dice colors.");
  if ((state.phase === "place") !== Boolean(state.draftedDie)) throw new Error("Invalid White Castle drafted die state.");
  if (state.pendingEffects.some((pending) => pending.owner !== undefined && !isSeat(state, pending.owner))) throw new Error("Invalid White Castle pending owner.");
  if ((state.phase === "draft" || state.phase === "place" || state.phase === "finished") && state.pendingEffects.length > 0) throw new Error("Invalid White Castle pending phase.");
  if ((state.phase === "setup" || state.phase === "draft" || state.phase === "place" || state.phase === "finished") && state.actionFlow) throw new Error("Invalid White Castle action flow.");
  if (state.actionFlow && state.pendingEffects[0]?.effect.type !== "majorAction") throw new Error("Invalid White Castle action flow.");
  const firstPending = state.pendingEffects[0];
  const blockingTypes: Effect["type"][] = ["gainChoice", "chooseOne", "effectOrder", "actionOrder", "majorAction", "castleRefresh", "pay", "domainAction", "castleTileAction", "daimyoReward", "gardenActivation"];
  if (firstPending && !blockingTypes.includes(firstPending.effect.type)
    && !(firstPending.effect.type === "influence" && nextInfluenceCheckpoint(state, firstPending.effect.amount))) throw new Error("Invalid White Castle automatic pending effect.");
}

function assertWorkspaceIdentity(state: GameState): void {
  if (!state.workspaces || typeof state.workspaces !== "object" || Array.isArray(state.workspaces)) {
    throw new Error("Invalid White Castle workspace topology.");
  }
  const expectedIds = Object.keys(createWorkspaces(state.players, state.board)).sort();
  const actualIds = Object.keys(state.workspaces).sort();
  if (canonicalFingerprint(actualIds) !== canonicalFingerprint(expectedIds)) {
    throw new Error("Invalid White Castle workspace topology.");
  }
  for (const id of expectedIds) {
    const workspace = state.workspaces[id];
    if (!workspace || workspace.id !== id || !Array.isArray(workspace.dice)) {
      throw new Error("Invalid White Castle workspace topology.");
    }
  }
}

function restoreWorkspaceTopology(state: GameState): void {
  assertWorkspaceIdentity(state);
  const expected = createWorkspaces(state.players, state.board);
  state.workspaces = Object.fromEntries(Object.entries(expected).map(([id, workspace]) => ([
    id,
    { ...structuredClone(workspace), dice:structuredClone(state.workspaces[id].dice) },
  ])));
}

function assertValidSetupSeed(state: GameState): void {
  if (!state.setup || typeof state.setup !== "object"
    || typeof state.setup.seed !== "number" || !Number.isSafeInteger(state.setup.seed)) {
    throw new Error("Invalid White Castle setup seed.");
  }
}

function canonicalAuthorityState(state: GameState): string {
  // deserializeGame normalizes the documented caches first (lanternEffects,
  // material projections, and finished scores/winner). No GameState field is
  // excluded here: terminal result, topology, setup provenance, and ledger
  // must all equal the replayed current representation.
  // Compare the persisted representation: optional undefined object properties
  // disappear in JSON. They do not change authority and cannot invalidate a
  // legitimate save between selecting a member and selecting its destination.
  return canonicalFingerprint(JSON.parse(serializeGame(state)));
}

function assertActionHistoryReplays(state: GameState): void {
  // Version-1 actions have no legacy aliases. The snapshot migrations above
  // normalize state derived from them; replayGame itself calls only
  // createGame/applyAction, so this restore check cannot recurse.
  const manualSetup = state.phase === "setup"
    || state.actionHistory.some((action) => action.type === "chooseStartingPair");
  let replayed:GameState;
  try {
    replayed = replayGame({
      seed:state.setup.seed,
      playerCount:state.playerCount,
      playerNames:state.players.map((player) => player.name),
      manualSetup,
    }, structuredClone(state.actionHistory));
  } catch {
    throw new Error("Invalid White Castle action history.");
  }
  if (canonicalAuthorityState(replayed) !== canonicalAuthorityState(state)) {
    throw new Error("Invalid White Castle action history.");
  }
}

export function deserializeGame(snapshot: string): GameState {
  const state = JSON.parse(snapshot) as GameState;
  if (state.version !== 1 || !isPlayerCount(state.playerCount) || !Array.isArray(state.actionHistory)) throw new Error("Unsupported or invalid game snapshot.");
  assertValidSetupSeed(state);
  if (!Array.isArray(state.players) || state.players.length !== state.playerCount || !isSeat(state, state.currentPlayer)
    || !Array.isArray(state.pendingEffects)
    || state.pendingEffects.some((pending) => pending.owner !== undefined && !isSeat(state, pending.owner))
    || ((state.phase === "draft" || state.phase === "place" || state.phase === "finished") && state.pendingEffects.length > 0)) throw new Error("Invalid White Castle restore preflight.");
  const pendingBeforeMigration = JSON.stringify(state.pendingEffects);
  if (!("actionFlow" in state)) state.actionFlow = undefined;
  if (!Array.isArray(state.influenceStackOrder)) state.influenceStackOrder = [...state.turnOrder].reverse();
  for (const player of state.players) {
    const resourceEntry = player.lanternCards.find((card) => card.startsWith("starting-resource-"));
    const resourceCard = resourceEntry ? STARTING_RESOURCE_CARDS.find((card) => card.id === Number(resourceEntry.split("-").at(-1))) : undefined;
    if (resourceCard?.decree && !player.lanternCards.includes(`decree-${resourceCard.decree}`)) player.lanternCards.splice(1, 0, `decree-${resourceCard.decree}`);
    player.lanternEffects = lanternEffectsFromCards(player.lanternCards);
  }
  const regeneratedBoard = createBoardSetup(state.setup.seed, state.playerCount);
  const initialBridges = rollBridges(state.setup.seed, 1, state.playerCount);
  state.setup.initialBridgeValues = Object.fromEntries(COLORS.map((color) => ([
    color,
    initialBridges[color].map((die) => die.value),
  ]))) as Record<DieColor, number[]>;
  state.setup.stewardDeck = [...regeneratedBoard.stewardDeck];
  state.setup.diplomatDeck = [...regeneratedBoard.diplomatDeck];
  state.setup.daimyoCard = regeneratedBoard.daimyoCard;
  state.setup.yardTiles = [...regeneratedBoard.yardTileIds];
  state.setup.gardenCards = [...regeneratedBoard.gardenCardIds];
  for (const room of state.board.castleRooms) {
    const regeneratedRoom = regeneratedBoard.castleRooms.find((candidate) => candidate.id === room.id);
    for (const [index, slot] of room.slots.entries()) {
      const legacySlot = slot as typeof slot & { reward?: Effect };
      if (legacySlot.reward === undefined && regeneratedRoom?.slots[index]) legacySlot.reward = regeneratedRoom.slots[index].reward;
    }
  }
  const legacyBoard = state.board as BoardState & { wellTileEffects?: Effect[] };
  if (!Array.isArray(state.board.wellTiles)) state.board.wellTiles = regeneratedBoard.wellTiles;
  delete legacyBoard.wellTileEffects;
  const migratedGardens = createGardens(state.board);
  for (const garden of migratedGardens) garden.gardeners = state.gardens.find((candidate) => candidate.id === garden.id)?.gardeners ?? [];
  state.gardens = migratedGardens;
  const migratedTrainingYards = createTrainingYards(state.board);
  for (const yard of migratedTrainingYards) yard.warriors = state.trainingYards.find((candidate) => candidate.id === yard.id)?.warriors ?? [];
  state.trainingYards = migratedTrainingYards;
  assertWorkspaceIdentity(state);
  const wellWorkspace = Object.values(state.workspaces).find((workspace) => workspace.kind === "well");
  if (wellWorkspace) wellWorkspace.effects = [{ type: "gain", resource: "seals", amount: 1 }, ...state.board.wellTiles.map((tile) => tile.reward)];
  for (const room of state.board.castleRooms) refreshCastleWorkspace(state, room.id);
  restoreWorkspaceTopology(state);
  migrateLegacyStartingResourceChoices(state);
  migrateLegacyTrainingYardRewardChoices(state);
  migrateLegacyCastleActionGroups(state);
  const pending = state.pendingEffects[0];
  const lastAction = state.actionHistory.at(-1);
  if (pending?.source.startsWith("castle-") && (pending.effect.type === "effectOrder" || pending.effect.type === "actionOrder")
    && lastAction?.type === "placeDie" && lastAction.workspace === pending.source) {
    const workspace = state.workspaces[pending.source];
    const die = workspace?.dice.at(-1);
    if (die) pending.effect = { type: "actionOrder", groups: structuredClone(workspace.effectGroupsByColor?.[die.color] ?? []) };
  }
  if (JSON.stringify(state.pendingEffects) !== pendingBeforeMigration) drainAutomaticEffects(state, []);
  if (state.phase === "finished") {
    state.scores = scoreGame(state);
    state.winner = determineWinner(state, state.scores);
  }
  validateRestoredGameState(state);
  assertActionHistoryReplays(state);
  return state;
}

export function replayGame(options: CreateGameOptions, actions: GameAction[]): GameState {
  let state = createGame(options);
  for (const action of actions) state = applyAction(state, action).state;
  return state;
}
