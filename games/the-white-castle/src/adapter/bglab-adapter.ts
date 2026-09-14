import {
  applyAction,
  availableCastleTileActions,
  availableDomainRows,
  canonicalFingerprint,
  courtierFloor,
  createGame,
  deserializeGame,
  domainRowEffects,
  getLegalActions,
  serializeGame,
  stabilizeAutomaticEffects,
  INFLUENCE_CHECKPOINTS,
  STARTING_ACTION_CARDS,
  STARTING_RESOURCE_CARDS,
  DECREE_CARDS,
  DAIMYO_CARDS,
  materialEffects,
  placementReferenceValue,
  placementCoinDelta,
  type CreateGameOptions,
  type CastleActionSelector,
  type GameAction,
  type GameEvent,
  type GameState,
  type MajorActionMode,
} from "../core";
import * as gameCore from "../core";
import { determineWinner, scoreGame, scoreResources } from "../core/scoring";
import {
  buildCourtierPromotionSources,
  buildScoringDecisionFacts,
} from "./scoring-frame";
import { buildOpportunityFrameData } from "./opportunity-frame";
import { buildMinimalBoardFrame } from "./minimal-board-frame";
import { renderPublicOutcome } from "./public-outcome";
import { semanticChoiceKindForEngineStep } from "./semantic-choice";
import { describeCastleRowSelection } from "./effect-labels";
import { createRequire } from "node:module";
import { createHash } from "node:crypto";

const { createDecisionExplorer, createDecisionMap, createAuthorityGateway } = createRequire(import.meta.url)("../../../_sdk/decision-explorer/index.cjs") as {
  createDecisionExplorer: (initial: GameState, seat: number, hooks: Record<string, unknown>, limits: Record<string, number>) => {
    outcomeIndex: (request?: Record<string, unknown>) => Record<string, unknown>;
    enumerateRoutes: (request?: Record<string, unknown>) => Record<string, unknown>;
    diagnostics: () => Record<string, unknown>;
  };
  createDecisionMap: (options: Record<string, unknown>) => {
    enumerateRoutes: (request?: Record<string, unknown>) => Record<string, unknown>;
    issueComplete: (candidate: Record<string, unknown>) => Record<string, unknown>;
    stop: () => { released: boolean };
  };
  createAuthorityGateway: (options: {
    currentIdentity: () => { decisionId: string; seat: number };
    finiteExplorerForSeat: (seat: number) => {
      outcomeIndex: (request?: Record<string, unknown>) => Record<string, unknown>;
      enumerateRoutes: (request?: Record<string, unknown>) => Record<string, unknown>;
      raw: ReturnType<typeof createDecisionExplorer>;
    };
    decisionMapFactory: (context: {
      frame: { gameId: string; decisionId: string; seat: number; stateHash: string };
      seat: number;
      pageSize: number;
      finiteExplorer: {
        raw: ReturnType<typeof createDecisionExplorer>;
      };
    }) => {
      enumerateRoutes: (request?: Record<string, unknown>) => Record<string, unknown>;
      stop: () => { released: boolean };
    };
    decisionMapPageSize?: number;
  }) => {
    outcomeIndex: (request?: Record<string, unknown>) => Record<string, unknown>;
    enumerateRoutes: (request?: Record<string, unknown>) => Record<string, unknown>;
    invalidate: () => { released: boolean };
  };
};

type SemanticRouteToken = { action:string; args:Record<string, unknown> };
type SemanticRouteAutomatonState = {
  final:boolean;
  transitions:Array<SemanticRouteToken & { to:number }>;
};
type RawSemanticRouteAutomaton = {
  schemaVersion:1;
  coverageStatus:"complete" | "bounded";
  enumerationComplete:boolean;
  startState:number | null;
  routeCount:string | null;
  states:SemanticRouteAutomatonState[];
  diagnostics:Record<string, unknown>;
};

const { createSemanticRouteAutomaton } = createRequire(import.meta.url)("../../../_sdk/semantic-route-automaton/index.cjs") as {
  createSemanticRouteAutomaton: (
    initialState:GameState,
    hooks:{
      stateKey:(state:GameState) => string;
      boundary:(state:GameState) => string | null;
      accepting?:(state:GameState, depth:number) => boolean;
      normalizeState?:(state:GameState, depth:number) => GameState | void;
      successors:(state:GameState, depth:number) => Array<{ token:SemanticRouteToken; state:GameState }>;
    },
    limits:{ maxNodes:number; maxTimeMs:number },
  ) => RawSemanticRouteAutomaton;
};

function applyEnumeratedAction(state:GameState, action:GameAction) {
  const fast = (gameCore as unknown as {
    applyKnownLegalAction?:typeof applyAction;
  }).applyKnownLegalAction;
  return fast ? fast(state, action) : applyAction(state, action);
}

function stableSha256(value:unknown):string {
  return createHash("sha256").update(canonicalFingerprint(value)).digest("hex");
}

function explorerLimit(name:string, fallback:number):number {
  const processLike = (globalThis as unknown as {
    process?: { env?:Record<string, string | undefined> };
  }).process;
  const value = Number(processLike?.env?.[name]);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

export interface ActionStep extends Record<string, unknown> {
  op: string;
}

export interface ActionTransaction {
  steps: ActionStep[];
}

export interface AdapterSnapshot {
  schemaVersion: 1;
  decisionId: string;
  wrapper: {
    turn: number;
    currentPlayer: number;
    phase: string;
    playerCount: number;
  };
  game: GameState;
  committed: Record<string, { fingerprint: string; result: DispatchResult }>;
  adapterView: AdapterGuidance;
}

export type AuthoritySnapshot = Omit<AdapterSnapshot, "adapterView">;

export interface AdapterGuidance {
  decisionId: string;
  turnGroupId: string;
  seat: number;
  phase: string;
  round: number;
  turn: number;
  currentPlayer: number;
  actionFamilies: string[];
  nextActions: GameAction[];
  publicState?: Record<string, unknown>;
  privateState?: Record<string, unknown>;
  decisionSurface?: Record<string, unknown>;
  turnOutcomeSummary?: Record<string, unknown>;
  constraints: string[];
}

export type DispatchResult = {
  ok: boolean;
  decisionId: string;
  boundaryReason?: "turn_passed" | "other_actor" | "game_finished";
  action?: ActionTransaction;
  events?: GameEvent[];
  duplicate?: boolean;
  failedStep?: number;
  validatedPrefix?: ActionStep[];
  code?: string;
  message?: string;
  correction?: string;
  nextActions?: GameAction[];
  continuationChoices?: Array<{ step: ActionStep; meaning: string }>;
  continuationStatus?: "must_continue" | "may_stop";
  declineActions?: GameAction[];
};

export type TransactionValidationResult = Omit<DispatchResult, "nextActions" | "declineActions"> & {
  nextActions?: ActionStep[];
  declineActions?: ActionStep[];
  complete: boolean;
  stateChanged: false;
  autoAdvancedSteps?: ActionStep[];
  causalTrace?: TurnProgram["causalTrace"];
  factualCosts?: Record<string, number>;
  factualGains?: Record<string, number>;
  immediateEffects?: string[];
  immediateScoreDelta?: number;
  endNowScoreDelta?: number;
  scoringEngineChanges?: Array<Record<string, unknown>>;
  outcome?: Record<string, unknown>;
};

export type TurnProgram = { programId: string; decisionId: string; rootId: string; boundaryReason: string; termination?: string; steps: ActionStep[]; causalTrace: Array<{ step: number; actionStep?: number; source: string; effect: string; majorAction?: string; choiceType?: string; choiceSource?: string; delta?: Record<string, number>; enables?: string[] }>; outcome: Record<string, unknown>; netOutcome: Record<string, unknown>; factualCosts: Record<string, number>; factualGains: Record<string, number>; immediateEffects: string[]; immediateScoreDelta: number; endNowScoreDelta: number; scoringEngineChanges: Array<Record<string, unknown>>; complete: true; stateKeys: string[] };
export type OutcomeIndex = Record<string, unknown> & {
  decisionId: string;
  seat: number;
  enumerationComplete: boolean;
  roots: Array<{ rootId: string; initialAction: ActionStep; reachable: Record<string, unknown> }>;
  reachableActionFamilies?: Array<{
    family: string;
    variants: string[];
    reachability: "observed";
    coverageStatus: string;
    observedProgramCount: number;
    routeQuery: { stepsContain: ActionStep[] };
    factualCosts: unknown[];
    factualImmediateEffects: string[];
    observedEffects: string[];
  }>;
  message?: string;
};
export type RouteEnumerationResult = { status: "routes"; decisionId: string; enumerationId: string; coverageStatus: string; enumerationComplete:boolean; ordering?: "coverage-diverse-not-ranked"; programs: TurnProgram[]; nextCursor: string | null; totalMatches: number; routeDiagnostics?:Record<string, unknown> };
export type SemanticRouteAutomatonResult = {
  status:"semantic-route-automaton";
  schemaVersion:1;
  decisionId:string;
  enumerationId:string;
  coverageStatus:"complete" | "bounded";
  enumerationComplete:boolean;
  code?:"INCOMPLETE_OUTCOME_INDEX" | "SEMANTIC_PREFIX_NOT_AT_BOUNDARY";
  startState:number | null;
  routeCount:string | null;
  prefixActionCount:number;
  states:SemanticRouteAutomatonState[];
  routeDiagnostics:Record<string, unknown>;
};

type SemanticPrefixProjection = {
  status:"complete" | "partial";
  authorityBase:GameState;
  state:GameState;
  semanticActions:SemanticRouteToken[];
  prefixSteps:ActionStep[];
};

type SemanticOptionProjection = {
  choiceId:string;
  action:string;
  args:Record<string, unknown>;
  cost:Record<string, number>;
  reward:Record<string, unknown>;
  boundary:Record<string, unknown>;
};

type SemanticOptionPlan = {
  choiceId:string;
  action:string;
  args:Record<string, unknown>;
  engineActions:GameAction[];
  choiceDetails:Record<string, unknown>;
  projectedState?:GameState;
  events?:GameEvent[];
};

const BEGIN_STEP: ActionStep = { op: "begin", action: "turn" };

function clone<T>(value: T): T {
  return structuredClone(value);
}

function endedMajorActionFromTrace(
  trace: TurnProgram["causalTrace"],
): string | undefined {
  return [...trace].reverse().find((event) => (
    event.effect === "majorAction" && typeof event.majorAction === "string"
  ))?.majorAction;
}

function startingResourceCard(cardId: number) {
  const card = STARTING_RESOURCE_CARDS.find((candidate) => candidate.id === cardId);
  if (!card) throw new Error(`Unknown starting resource card ${cardId}.`);
  return card;
}

function startingActionCard(cardId: number) {
  const card = STARTING_ACTION_CARDS.find((candidate) => candidate.id === cardId);
  if (!card) throw new Error(`Unknown starting action card ${cardId}.`);
  return card;
}

// Frame projection only: the printed card contains both immediate resources
// and installed future effects. Keep these roles explicit in every offer.
function startingResourceFacts(cardId:number) {
  const { resources, lantern, decree, ...card } = clone(startingResourceCard(cardId));
  return {
    ...card,
    grantedImmediatelyAtSetup:resources,
    installedLanternForLater_noSetupGain:lantern,
    ...(decree === undefined ? {} : {
      installedDecreeForFutureLantern_noSetupGain:{ id:decree, effects:clone(DECREE_CARDS[decree]) },
    }),
  };
}

function stepToAction(step: ActionStep): GameAction {
  const {
    op,
    row: _publicRow,
    publicFloor: _publicFloor,
    publicRoom: _publicRoom,
    publicWorkspace: _publicWorkspace,
    personalRow: _personalRow,
    choiceType: _choiceType,
    ...fields
  } = step;
  return { type: op, ...fields } as GameAction;
}

function actionToValidationStep(state:GameState, action:GameAction):ActionStep {
  const step = actionToStep(action);
  if (action.type !== "chooseEffectOption") return step;
  const pending = state.pendingEffects.find((item) => item.id === action.effectId);
  return pending
    ? { ...step, choiceType:pending.effect.type }
    : step;
}

function castleRowNumber(room:string, rowId:string):number {
  if (rowId.endsWith("-row-top")) return 1;
  if (rowId.endsWith("-row-middle")) return 2;
  if (rowId.endsWith("-row-bottom")) return room.startsWith("diplomat-") ? 2 : 3;
  throw new Error(`Castle row lacks a natural position: ${room}`);
}

function actionToStep(action: GameAction): ActionStep {
  const { type, ...fields } = action;
  if (action.type === "placeDie") {
    const personal = /^p[0-9]+-domain-(courtier|gardener|warrior)$/.exec(action.workspace);
    if (personal) return { op:type, ...fields, personalRow:personal[1] };
    const castle = /^castle-(steward|diplomat)-([0-9]+)$/.exec(action.workspace);
    if (castle) {
      return {
        op:type,
        ...fields,
        publicFloor:castle[1],
        publicRoom:Number(castle[2]),
      };
    }
    return { op:type, ...fields, publicWorkspace:action.workspace };
  }
  if (action.type === "selectMajorActionTarget") {
    const castle = /^(steward|diplomat)-[0-9]+$/.exec(action.target);
    if (castle) return { op:type, ...fields, publicFloor:castle[1] };
    if (action.target === "daimyo") {
      return { op:type, ...fields, publicFloor:"daimyo" };
    }
  }
  return action.type === "selectCastleTileAction"
    ? {
      op:type,
      ...fields,
      publicFloor:action.room.startsWith("diplomat-") ? "diplomat" : "steward",
      publicRoom:Number(action.room.split("-").at(-1)),
      row:castleRowNumber(action.room, action.rowId),
    }
    : { op:type, ...fields };
}

function validationFrontier(result: DispatchResult): Omit<DispatchResult, "nextActions" | "declineActions"> & {
  nextActions?: ActionStep[];
  declineActions?: ActionStep[];
} {
  const { nextActions, declineActions, ...rest } = result;
  return {
    ...rest,
    ...(nextActions ? { nextActions:nextActions.map(actionToStep) } : {}),
    ...(declineActions ? { declineActions:declineActions.map(actionToStep) } : {}),
  };
}

const PUBLIC_RESOURCE_NAMES: Record<string, string> = {
  coins:"coins", seals:"seals", food:"food", iron:"iron", pearl:"pearl",
};

function describePublicEffects(effects: unknown): string {
  if (!Array.isArray(effects) || effects.length === 0) return "nothing further";
  return effects.map(describePublicEffect).join(", then ");
}

function describePublicEffect(effect: unknown): string {
  if (!effect || typeof effect !== "object") return String(effect);
  const item = effect as Record<string, unknown>;
  const type = String(item.type ?? "unknown effect");
  if (type === "gain") {
    return `gain ${item.amount} ${PUBLIC_RESOURCE_NAMES[String(item.resource)] ?? item.resource}`;
  }
  if (type === "gainChoice") {
    return `make ${item.amount} resource selections from ${JSON.stringify(item.resources ?? [])}; each selection gains exactly 1 chosen resource; the same resource may be selected again`;
  }
  if (type === "influence") return `gain ${item.amount} influence`;
  if (type === "gainPoints") return `gain ${item.amount} points`;
  if (type === "lantern") return "resolve the current lantern reward";
  if (type === "wellAction") return "resolve the current well rewards";
  if (type === "domainAction") return "choose one currently available personal-board row";
  if (type === "castleTileAction") return describeCastleRowSelection(String(item.color));
  if (type === "gardenActivation") return `activate garden ${JSON.stringify(item.gardens ?? [])}`;
  if (type === "majorAction") return `start one ${item.action} action`;
  if (type === "castleRefresh") return `refresh castle room ${item.room}`;
  if (type === "daimyoReward") return `choose one open daimyo reward position ${JSON.stringify(item.positions ?? [])}`;
  if (type === "pay") {
    return item.optional
      ? `optional payment: pay ${item.amount} ${item.resource} to unlock ${describePublicEffects(item.effects)}; decline = skip every unlocked effect`
      : `pay ${item.amount} ${item.resource}, then ${describePublicEffects(item.effects)}`;
  }
  return type;
}

function continuationMeaning(state: GameState, action: GameAction): string {
  const step = actionToStep(action);
  if (action.type !== "chooseEffectOption") return `execute ${JSON.stringify(step)}`;
  const pending = state.pendingEffects.find((item) => item.id === action.effectId);
  if (!pending) return `choose option ${action.option} for ${action.effectId}`;
  const effect = pending.effect;
  if (effect.type === "pay") {
    return action.option === 0
      ? `pay ${effect.amount} ${effect.resource}, then ${describePublicEffects(effect.effects)}`
      : "decline the optional payment; its gated effects do not happen";
  }
  if (effect.type === "actionOrder") {
    const group = effect.groups[action.option];
    return group
      ? `resolve group ${group.id}: ${describePublicEffects(group.effects)}`
      : `choose unavailable group index ${action.option}`;
  }
  if (effect.type === "effectOrder") {
    return `resolve next: ${describePublicEffect(effect.effects[action.option])}`;
  }
  if (effect.type === "gainChoice") {
    return `gain exactly 1 ${effect.resources[action.option]}; selectionsRemainingAfter=${Math.max(0, effect.amount - 1)}; the same resource may be selected again`;
  }
  if (effect.type === "chooseOne") {
    const label = effect.labels[action.option] ?? `option ${action.option}`;
    return `choose ${label}: ${describePublicEffects(effect.options[action.option])}`;
  }
  if (effect.type === "daimyoReward") {
    return `choose daimyo reward position ${effect.positions[action.option]}`;
  }
  if (effect.type === "gardenActivation") {
    return `activate garden ${effect.gardens[action.option]}`;
  }
  if (effect.type === "domainAction") {
    return `choose currently available personal-board row at option ${action.option}`;
  }
  return `choose option ${action.option} while resolving ${describePublicEffect(effect)}`;
}

function continuationChoices(
  state: GameState,
  actions: GameAction[],
): Array<{ step: ActionStep; meaning: string }> {
  return actions
    .filter((action) => action.type === "chooseEffectOption")
    .map((action) => ({
      step: actionToStep(action),
      meaning: continuationMeaning(state, action),
    }));
}

function routeStateKey(state: GameState): string {
  // The key only omits the top-level audit history. A shallow replacement is
  // sufficient because serialization is read-only and avoids cloning every
  // nested board object once per explored Authority state.
  return serializeGame({ ...state, actionHistory:[] });
}

function equivalentOrderContinuation(state: GameState): GameAction | undefined {
  const pending = state.pendingEffects[0];
  if (!pending || !["effectOrder", "actionOrder"].includes(pending.effect.type)) {
    return undefined;
  }
  const initial = getLegalActions(state).filter((action): action is Extract<GameAction, { type: "chooseEffectOption" }> => (
    action.type === "chooseEffectOption" && action.effectId === pending.id
  ));
  if (initial.length < 2) return undefined;

  let terminalKey: string | undefined;
  let visitedNodes = 0;
  let equivalent = true;
  const visit = (candidate: GameState): void => {
    if (!equivalent || visitedNodes >= 64) {
      equivalent = false;
      return;
    }
    visitedNodes += 1;
    const current = candidate.pendingEffects[0];
    if (
      !current
      || !["effectOrder", "actionOrder"].includes(current.effect.type)
    ) {
      const key = routeStateKey(candidate);
      terminalKey ??= key;
      if (terminalKey !== key) equivalent = false;
      return;
    }
    const choices = getLegalActions(candidate).filter((action): action is Extract<GameAction, { type: "chooseEffectOption" }> => (
      action.type === "chooseEffectOption" && action.effectId === current.id
    ));
    if (choices.length === 0) {
      equivalent = false;
      return;
    }
    for (const choice of choices) visit(applyAction(candidate, choice).state);
  };
  visit(state);
  return equivalent ? (initial.find((action) => action.option === 0) ?? initial[0]) : undefined;
}

function routeActionsForDeterminism(actions: GameAction[]): GameAction[] {
  return actions.filter((action) => (
    action.type !== "exchangeSeal"
    && action.type !== "cancelMajorActionSelection"
  ));
}

function exchangesChangeCurrentChoice(
  state: GameState,
  routeActions: GameAction[],
): boolean {
  if (!getLegalActions(state).some((action) => action.type === "exchangeSeal")) return false;
  const baseline = new Set(routeActions.map((action) => canonicalFingerprint(action)));
  const seen = new Set<string>();
  const pending: GameState[] = [structuredClone(state)];
  while (pending.length > 0) {
    const current = pending.pop()!;
    const stateKey = canonicalFingerprint(serializeGame(current));
    if (seen.has(stateKey)) continue;
    seen.add(stateKey);
    for (const exchange of getLegalActions(current).filter(
      (action): action is Extract<GameAction, { type:"exchangeSeal" }> => (
        action.type === "exchangeSeal"
      ),
    )) {
      const next = applyEnumeratedAction(current, exchange).state;
      const nextRouteActions = routeActionsForDeterminism(getLegalActions(next));
      const nextSet = new Set(nextRouteActions.map((action) => canonicalFingerprint(action)));
      if (
        nextSet.size !== baseline.size
        || [...nextSet].some((fingerprint) => !baseline.has(fingerprint))
      ) {
        return true;
      }
      pending.push(next);
    }
  }
  return false;
}

function deterministicRouteContinuation(state: GameState, actions: GameAction[]): GameAction | undefined {
  const routeActions = routeActionsForDeterminism(actions);
  // Exchanges are normally omitted from forced mechanical progress so they do
  // not create redundant permutations.  They are not ignorable when one or
  // more exchanges unlocks a new current choice (for example recruit, promote,
  // or accepting an optional payment).  In that case finishing/skipping is a
  // real player decision and must stay in the semantic route language.
  if (exchangesChangeCurrentChoice(state, routeActions)) return undefined;
  if (routeActions.length === 1) return routeActions[0];
  return equivalentOrderContinuation(state);
}

function decisionBoundary(
  before: GameState,
  after: GameState,
): "turn_passed" | "other_actor" | "game_finished" | null {
  if (after.phase === "finished") return "game_finished";
  if (before.phase === "setup") {
    return after.phase !== "setup" || after.setupIndex > before.setupIndex
      ? "turn_passed"
      : null;
  }
  if (before.phase !== "roundEnd" && after.phase === "roundEnd") {
    return "turn_passed";
  }
  if (
    before.phase === "roundEnd"
    && after.phase === "roundEnd"
    && after.currentPlayer !== before.currentPlayer
  ) {
    return "other_actor";
  }
  return after.turn > before.turn ? "turn_passed" : null;
}

function decisionComplete(before: GameState, after: GameState): boolean {
  return decisionBoundary(before, after) !== null;
}

function correctionFor(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error);
  if (message.startsWith("Illegal action:")) {
    return "该步骤不在当前中间状态的合法动作中。保留已验证前缀，查询 nextActions，修正失败步骤后重新提交完整 steps。";
  }
  return "保留已验证前缀，按当前中间状态的规则修正失败步骤，然后重新提交完整 steps。";
}

export class WhiteCastleBGLabAdapter {
  readonly protocolVersion = 2;
  readonly snapshotVersion = 1;
  private state: GameState;
  private committed: Record<string, { fingerprint: string; result: DispatchResult }> = {};
  private listeners = new Set<(snapshot: AdapterSnapshot, events: GameEvent[]) => void>();
  private authorityGateway: ReturnType<typeof createAuthorityGateway>;
  private prefixExplorerCache = new Map<string, {
    explorer:ReturnType<typeof createDecisionExplorer>;
    prefixSteps:ActionStep[];
    prefixTrace:TurnProgram["causalTrace"];
  }>();
  private semanticRouteAutomatonCache = new Map<string, SemanticRouteAutomatonResult>();

  private clearOutcomeIndex(): void {
    this.authorityGateway.invalidate();
    this.prefixExplorerCache.clear();
    this.semanticRouteAutomatonCache.clear();
  }

  constructor(options: CreateGameOptions = {}) {
    this.state = createGame({ ...options, manualSetup: options.manualSetup ?? true });
    this.authorityGateway = createAuthorityGateway({
      currentIdentity:() => ({ decisionId:this.decisionId(), seat:this.state.currentPlayer }),
      finiteExplorerForSeat:(seat) => {
        const raw = this.decisionExplorer(seat);
        return {
          raw,
          outcomeIndex:(request = {}) => raw.outcomeIndex(request),
          enumerateRoutes:(request = {}) => this.enumerateFiniteRoutes(
            request,
            seat,
            raw,
          ) as unknown as Record<string, unknown>,
        };
      },
      decisionMapFactory:({ frame, seat, pageSize, finiteExplorer }) => (
        this.createDecisionMapForFrame(frame, seat, pageSize, finiteExplorer.raw)
      ),
      decisionMapPageSize:20,
    });
  }

  start(config: {
    names?: string[];
    playerNames?: string[];
    playerCount?: CreateGameOptions["playerCount"];
    seed?: number;
    manualSetup?: boolean;
  } = {}): AdapterGuidance {
    const supplied = config.playerNames ?? config.names;
    const playerNames = supplied?.map(String);
    this.state = createGame({
      seed:config.seed,
      playerCount:config.playerCount,
      playerNames,
      manualSetup:config.manualSetup ?? true,
    });
    this.committed = {};
    this.clearOutcomeIndex();
    this.publish([]);
    return this.view(this.state.currentPlayer);
  }

  decisionId(): string {
    if (this.state.phase === "roundEnd") {
      const pendingId = this.state.pendingEffects[0]?.id ?? "complete";
      return `twc:${this.state.turn}:roundEnd:${this.state.currentPlayer}:${pendingId}`;
    }
    return `twc:${this.state.turn}:${this.state.setupIndex}:${this.state.currentPlayer}`;
  }

  coverageCatalog(): { actionFamilies: string[]; effectFamilies: string[]; boundaryReasons: string[] } {
    return {
      actionFamilies:["chooseStartingPair", "draftDie", "placeDie", "chooseEffectOption", "selectCastleTileAction", "exchangeSeal", "beginMajorAction", "selectMajorActionSource", "selectMajorActionTarget", "cancelMajorActionSelection", "confirmMajorAction", "refreshCastleRoom", "finishMajorAction", "finishResolution"],
      effectFamilies:["DieDrafted", "DiePlaced", "ResourceChanged", "InfluenceChanged", "MemberMoved", "CardMoved", "ChoiceRequired", "EffectResolved", "TurnStarted", "RoundEnded", "RoundStarted", "GameScored"],
      boundaryReasons:["turn_passed", "other_actor", "game_finished"],
    };
  }

  private canonicalStateKey(state: GameState): string {
    // actionHistory is audit-only: rules, legal actions, scoring, and terminal
    // state are determined by the remaining serialized authoritative fields.
    return routeStateKey(state);
  }

  renderPublicOutcome(outcome: Record<string, unknown>): string {
    return renderPublicOutcome(outcome);
  }

  private outcomeFor(before: GameState, after: GameState): Record<string, unknown> {
    const player = before.currentPlayer;
    const was = before.players[player];
    const now = after.players[player];
    const scoreBefore = scoreGame(before).find((item) => item.player === player)!;
    const scoreAfter = scoreGame(after).find((item) => item.player === player)!;
    const structuralBenefits:Array<Record<string, unknown>> = [];
    for (const row of ["courtier", "gardener", "warrior"] as const) {
      const beforeEffects = domainRowEffects(before, player, row);
      const afterEffects = domainRowEffects(after, player, row);
      if (canonicalFingerprint(beforeEffects) === canonicalFingerprint(afterEffects)) continue;
      structuralBenefits.push({
        kind:"personal_board",
        row,
        before:describePublicEffects(beforeEffects),
        after:describePublicEffects(afterEffects),
      });
    }
    if (canonicalFingerprint(was.lanternEffects) !== canonicalFingerprint(now.lanternEffects)) {
      structuralBenefits.push({
        kind:"lantern_reward",
        before:describePublicEffects(was.lanternEffects),
        after:describePublicEffects(now.lanternEffects),
      });
    }
    const castleCourtiers = (state:GameState) => state.players[player].members.filter((member) => (
      member.type === "courtier"
      && (member.location.startsWith("steward-")
        || member.location.startsWith("diplomat-")
        || member.location === "daimyo")
    )).length;
    const multiplierBefore = castleCourtiers(before);
    const multiplierAfter = castleCourtiers(after);
    if (multiplierBefore !== multiplierAfter) {
      structuralBenefits.push({
        kind:"warrior_multiplier",
        before:multiplierBefore,
        after:multiplierAfter,
      });
    }
    return {
      foodDelta: now.resources.food - was.resources.food,
      ironDelta: now.resources.iron - was.resources.iron,
      pearlDelta: now.resources.pearl - was.resources.pearl,
      sealsDelta: now.resources.seals - was.resources.seals,
      influenceDelta: now.influence - was.influence,
      scoreDelta: scoreAfter.duringGame - scoreBefore.duringGame,
      scoreAfter: scoreAfter.duringGame,
      immediateScoreDelta: scoreAfter.duringGame - scoreBefore.duringGame,
      endNowScoreDelta: scoreAfter.total - scoreBefore.total,
      ...(structuralBenefits.length ? { structuralBenefits } : {}),
      remaining:{resources:clone(now.resources), influence:now.influence},
      scoreIfGameEnded:{before:clone(scoreBefore), after:clone(scoreAfter)},
      nextPlayer: after.currentPlayer,
      phase: after.phase,
    };
  }

  private decisionFacts(seat: number, legal: GameAction[]): Record<string, unknown> {
    const player = this.state.players[seat];
    const score = scoreGame(this.state).find((item) => item.player === seat)!;
    const castleCourtierCount = player.members.filter((member) => (
      member.type === "courtier"
      && (member.location.startsWith("steward-") || member.location.startsWith("diplomat-") || member.location === "daimyo")
    )).length;
    const flow = this.state.actionFlow;
    const targetFacts = legal
      .filter((action): action is Extract<GameAction, { type: "selectMajorActionTarget" }> => action.type === "selectMajorActionTarget")
      .map((action) => {
        if (flow?.mode === "gardener") {
          const garden = this.state.gardens.find((candidate) => candidate.id === action.target)!;
          return { id:garden.id, foodCost:garden.foodCost, points:garden.points, bridge:garden.bridge, effects:clone(garden.effects) };
        }
        if (flow?.mode === "warrior") {
          const yard = this.state.trainingYards.find((candidate) => candidate.id === action.target)!;
          return {
            id:yard.id, ironCost:yard.ironCost, warriorValue:yard.warriorValue,
            capacity:yard.capacity, occupied:yard.warriors.length, effects:clone(yard.effects),
            scorePerOwnedWarrior:yard.warriorValue * castleCourtierCount,
          };
        }
        if (flow?.mode === "recruit") return { id:"gate", coinsCost:2, courtierScore:1 };
        const room = this.state.board.castleRooms.find((candidate) => candidate.id === action.target);
        const destinationScore = action.target === "daimyo" ? 10 : room?.floor === "diplomat" ? 6 : 3;
        return { id:action.target, levels:action.levels, pearlCost:action.levels === 2 ? 5 : 2, courtierScore:destinationScore };
      });
    const familyNames = [...new Set(legal.map((action) => action.type))];
    const modes = legal
      .filter((action): action is Extract<GameAction, { type: "beginMajorAction" }> => action.type === "beginMajorAction")
      .map((action) => action.mode);
    const coverage = familyNames.map((family) => ({
      family, variants:family === "beginMajorAction" ? modes : [],
      coverageStatus:"bounded", observedProgramCount:0, presentOnReturnedPage:true,
      continuationAvailable:true,
    }));
    const promotionOptions = legal
      .filter((action): action is Extract<GameAction, { type: "beginMajorAction" }> => (
        action.type === "beginMajorAction" && action.mode === "promote"
      ))
      .map((begin) => {
        const started = applyAction(structuredClone(this.state), begin).state;
        const sources = getLegalActions(started)
          .filter((action): action is Extract<GameAction, { type: "selectMajorActionSource" }> => action.type === "selectMajorActionSource")
          .map((source) => {
            const selected = applyAction(started, source).state;
            const targets = getLegalActions(selected)
              .filter((action): action is Extract<GameAction, { type: "selectMajorActionTarget" }> => action.type === "selectMajorActionTarget")
              .map((target) => {
                const room = selected.board.castleRooms.find((candidate) => candidate.id === target.target);
                return {
                  id:target.target,
                  levels:target.levels,
                  pearlCost:target.levels === 2 ? 5 : 2,
                  courtierScore:target.target === "daimyo" ? 10 : room?.floor === "diplomat" ? 6 : 3,
                };
              });
            return { member:source.member, targets };
          });
        return { mode:"promote", sources };
      });
    const scoringDecisionFacts = buildScoringDecisionFacts(this.state, seat);
    const scoreFacts = scoringDecisionFacts.score as Record<string, unknown>;
    return {
      score:scoreFacts,
      scoringDecisionFacts,
      legalFamilies:coverage.map(({ family, variants }) => ({
        family, variants, availableNow:true,
        legalTargets:family === "selectMajorActionTarget" ? targetFacts : [],
        routeQuery:{ stepsContain:[{ op:family }] },
      })),
      outcomeDimensions:[
        { id:"influence", label:"influence", currentValue:player.influence },
        { id:"castleCourtiers", label:"castle courtiers", currentValue:castleCourtierCount },
      ],
      scoringStructure:{
        courtiers:player.members.filter((member) => member.type === "courtier").map((member) => ({
          id:member.id, location:member.location,
          endGamePoints:member.location === "gate" ? 1
            : member.location.startsWith("steward-") ? 3
              : member.location.startsWith("diplomat-") ? 6
                : member.location === "daimyo" ? 10 : 0,
        })),
        gardens:this.state.gardens.map((garden) => ({
          id:garden.id, foodCost:garden.foodCost, points:garden.points,
          bridge:garden.bridge, occupiedBySeat:garden.gardeners.includes(seat), effects:clone(garden.effects),
        })),
        trainingYards:this.state.trainingYards.map((yard) => ({
          id:yard.id, ironCost:yard.ironCost, warriorValue:yard.warriorValue,
          capacity:yard.capacity, occupied:yard.warriors.length,
          scorePerOwnedWarrior:yard.warriorValue * castleCourtierCount, effects:clone(yard.effects),
        })),
        timeTrack:{ influence:player.influence, currentPoints:score.timeTrack },
      },
      actionFamilyCoverage:coverage,
      majorActionOptions:promotionOptions,
      ...(flow ? { legalMajorAction:{ mode:flow.mode, stage:flow.stage, selectedSource:flow.selectedSource, targets:targetFacts } } : {}),
    };
  }

  private decisionSurface(
    seat: number,
    decisionFacts: Record<string, unknown>,
  ): Record<string, unknown> {
    if (!["setup", "draft", "roundEnd"].includes(this.state.phase)) {
      throw new Error(
        `Player DecisionFrame is unavailable during phase ${this.state.phase}`,
      );
    }
    const player = this.state.players[seat];
    const legal = getLegalActions(this.state);
    const semanticOptions = this.projectSemanticOptions(this.state, legal);
    const currentActions = this.semanticCurrentActions(semanticOptions);
    const currentSemanticOptionsFact = {
      kind:"CurrentTargetFact",
      id:"current-semantic-options",
      title:"Options",
      data:{ options:clone(semanticOptions) },
    };
    if (this.state.phase === "setup") {
      const setupHasLaterActor = this.state.setupIndex + 1 < this.state.setupOrder.length;
      const setupSuccessorPhase = setupHasLaterActor ? "setup" : "draft";
      const setupSuccessorActor = setupHasLaterActor
        ? this.state.setupOrder[this.state.setupIndex + 1]
        : this.state.turnOrder[0];
      const setupSemanticAction = (action: GameAction): string => {
        if (action.type === "chooseStartingPair") {
          return `choose_starting_pair(offer=${action.offer})`;
        }
        if (action.type === "chooseEffectOption") {
          return `choose_reward(kind=starting_resource, choice=${action.option})`;
        }
        return "authority_choice";
      };
      const setupOffers = this.state.startingOffers.map((offer, index) => {
        const resourceCard = startingResourceCard(offer.resourceCard);
        const actionCard = startingActionCard(offer.actionCard);
        const projectedPlayer = clone(player);
        for (const [resource, amount] of Object.entries(resourceCard.resources)) {
          const key = resource as keyof typeof projectedPlayer.resources;
          const cap = key === "coins" ? Number.POSITIVE_INFINITY : 7;
          projectedPlayer.resources[key] = Math.min(
            cap,
            projectedPlayer.resources[key] + Number(amount),
          );
        }
        const choiceScores: number[] = [];
        const choiceCount = resourceCard.choiceResources ?? 0;
        const choiceResources = ["food", "iron", "pearl"] as const;
        const enumerateChoiceScores = (remaining: number, candidate: typeof projectedPlayer): void => {
          if (remaining === 0) {
            choiceScores.push(scoreResources(candidate));
            return;
          }
          for (const resource of choiceResources) {
            const next = clone(candidate);
            next.resources[resource] = Math.min(7, next.resources[resource] + 1);
            enumerateChoiceScores(remaining - 1, next);
          }
        };
        enumerateChoiceScores(choiceCount, projectedPlayer);
        const resourceScoreMin = Math.min(...choiceScores);
        const resourceScoreMax = Math.max(...choiceScores);
        const installedAction = actionCard.effect.type === "majorAction"
          ? actionCard.effect.action
          : "none";
      const installedActionMeaning: Record<string, string> = {
          courtier:"when a later route triggers it, opens the courtier window: recruit costs 2 coins; promote one level costs 2 pearl or two levels costs 5 pearl; final location scores at game end",
          gardener:"when a later route triggers it, opens one gardener deployment; public garden food costs range from 1 to 5; occupied gardens score at game end and may reactivate at round end",
          warrior:"when a later route triggers it, opens one warrior deployment; public yard iron costs are 1, 3, or 5; final warrior score uses yard value times final castle-courtier count",
        };
        return (
          `offer=${index}; available=${offer.claimedBy === null}; claimedBy=${offer.claimedBy ?? "none"}; `
          + `resourceCard=${resourceCard.materialId}; resources=${JSON.stringify(resourceCard.resources)}; `
          + `choiceResources=${resourceCard.choiceResources ?? 0}; decree=${resourceCard.decree ?? "none"}; `
          + `choiceResourceOptions=${choiceCount > 0 ? '["food","iron","pearl"]' : "none"}; `
          + `choiceResourceOptionIds=${choiceCount > 0 ? "[0=food,1=iron,2=pearl]" : "none"}; `
          + `lanternFutureEffect=${describePublicEffects(resourceCard.lantern)}; `
          + `decreeFutureEffect=${resourceCard.decree ? describePublicEffects(DECREE_CARDS[resourceCard.decree]) : "none"}; `
          + `installedLanternTiming=future lantern resolutions, not setup immediate; `
          + `resourceScoreIfGameEndedAfterSetupChoice=${resourceScoreMin === resourceScoreMax ? resourceScoreMin : `${resourceScoreMin}..${resourceScoreMax}`}; `
          + `actionCard=${actionCard.materialId}; installs action family=${installedAction}; `
          + `installedActionMeaning=${installedActionMeaning[installedAction] ?? "none"}`
        );
      });
      return {
        schemaVersion:"natural-decision-surface-v2",
        currentActions,
        modelFacts:{
          version:1,
          coverage:"complete-current-decision",
          phaseScope:"setup",
          facts:[
            {
              kind:"TurnFact", id:"setup-turn", title:"Setup turn",
              data:{
                lines:[
                  `Current setup choice is happening now; actor=P${seat} must choose exactly one available offer from this Frame.`,
                  `Global setup pick=${this.state.setupIndex + 1} of ${this.state.setupOrder.length}; reverse setup order has already been applied by the Engine. All offers marked available=true are selectable now; do not wait for another seat before choosing.`,
                  "Choosing an offer grants only its listed immediate resources now; resolve any listed resource choice before setup advances. Lantern and decree effects are installed for future lantern resolutions and grant nothing during setup. The action card is installed for later and does not execute its member action during setup; a later die, workspace, or reward must trigger that action.",
                  `After this offer and any resource choices finish: successorPhase=${setupSuccessorPhase}; nextActor=P${setupSuccessorActor}.`,
                ],
              },
            },
            {
              kind:"ResourceSnapshot", id:"setup-actor", title:"Actor resources",
              data:{
                resources:clone(player.resources),
                influence:player.influence,
                points:player.points,
              },
            },
            {
              ...currentSemanticOptionsFact,
              data:{ options:semanticOptions.map((option) => {
                const { boundary, ...choice } = clone(option);
                const { actorSeat, status, ...details } = boundary;
                return {
                  ...choice,
                  availableNow:true,
                  afterThisChoice:{
                    decisionStatus:status,
                    ...(actorSeat === undefined ? {} : { nextActorSeat:actorSeat }),
                    ...details,
                  },
                };
              }) },
            },
            {
              kind:"CurrentTargetFact", id:"setup-options", title:"Starting offers",
              data:{
                semanticActions:currentActions,
                offers:this.state.startingOffers.map((offer, index) => ({
                  offer:index,
                  available:offer.claimedBy === null,
                  claimedBy:offer.claimedBy,
                  resourceCard:startingResourceFacts(offer.resourceCard),
                  actionCard:clone(startingActionCard(offer.actionCard)),
                })),
                pendingEffect:this.state.pendingEffects.length === 0
                  ? null
                  : describePublicEffects(this.state.pendingEffects[0].effect),
              },
            },
          ],
        },
        narrativeSections:[
          {
            id:"setup-turn",
            title:"初始设置与行动者",
            lines:[
              `round ${this.state.round}/3; turn ${this.state.turn}; phase setup; actor seat ${seat} (${player.name})`,
              `global setup pick ${this.state.setupIndex + 1}/${this.state.setupOrder.length}; each seat chooses exactly one starting pair in setupOrder, so the current seat will not choose a second pair during setup.`,
              `successorBoundary=after the chosen offer and all pending setup resource choices; successorPhase=${setupSuccessorPhase}; nextActorSeat=${setupSuccessorActor}.`,
            ],
          },
          {
            id:"setup-actions",
            title:"当前合法初始设置行动",
            lines:[
              `semantic action family=${legal.some((action) => action.type === "chooseStartingPair") ? "choose_starting_pair" : "choose_reward"}`,
              ...legal.map((action) => `semantic action=${setupSemanticAction(action)}`),
              `pending choice=${this.state.pendingEffects.length === 0 ? "none" : describePublicEffects(this.state.pendingEffects[0].effect)}`,
            ],
          },
          {
            id:"setup-offers",
            title:"公开初始组合",
            lines:setupOffers,
          },
          {
            id:"setup-rules",
            title:"初始组合结算规则",
            lines:[
              "Choose exactly one available starting pair.",
              "Each seat chooses exactly once; the setup numerator/denominator counts global seat picks, not multiple picks for the current seat.",
              "The resource card grants its listed resources now; resolve any listed resource choice before setup advances.",
              "resourceScoreIfGameEndedAfterSetupChoice is a hypothetical if-ended resource-score component; it is never immediate points granted during setup.",
              "Lantern and decree icons are installed for future lantern resolutions; they do not grant their printed effect during the setup choice itself.",
              "The action card installs that family action on the personal board and does not execute that family action during setup.",
              "An installed family action is not a standalone main action: a later authoritative die/workspace/reward route must explicitly trigger that family before it can be used.",
              "Only the current legal action entries belong in this setup DecisionFrame; dice drafting and placement begin after every player finishes setup.",
            ],
          },
        ],
        scoringDecisionFacts:clone(decisionFacts.scoringDecisionFacts),
        informationBoundaries:[
          "Effects and choices not present in this surface are unknown until authority execution.",
          "This surface contains facts only; it does not rank or recommend an offer.",
        ],
      };
    }
    if (this.state.phase === "roundEnd") {
      const pending = this.state.pendingEffects[0];
      const scores = scoreGame(this.state);
      const actorScore = scores.find((item) => item.player === seat)!;
      const pendingGardenIds = pending?.effect.type === "gardenActivation"
        ? pending.effect.gardens
        : [];
      const semanticRoundEndAction = (state: GameState, action: GameAction): string => {
        if (action.type === "exchangeSeal") {
          return `exchange_seal(receive=${action.receive})`;
        }
        if (action.type === "selectCastleTileAction") {
          const floor = action.room.startsWith("diplomat-") ? "diplomat" : "steward";
          return `choose_castle_row(floor=${floor}, room=${Number(action.room.split("-").at(-1))}, row=${castleRowNumber(action.room, action.rowId)})`;
        }
        if (action.type === "refreshCastleRoom") {
          return `choose_reward(kind=refresh_castle_room, choice=${action.room})`;
        }
        if (action.type === "finishMajorAction") return "finish_action";
        if (action.type === "chooseEffectOption") {
          const kind = this.semanticChoiceKind(state, action);
          if (kind) return `choose_reward(kind=${kind}, choice=${action.option})`;
        }
        return "authority_choice";
      };
      const semanticRoundEndMeaning = (state: GameState, action: GameAction): string => {
        if (action.type === "exchangeSeal") {
          return action.receive === "coins"
            ? "pay 1 seal and gain 1 coin; the pending garden choice remains"
            : `pay 2 seals and gain 1 ${action.receive}; the pending garden choice remains`;
        }
        return continuationMeaning(state, action);
      };
      const gardenLines = pendingGardenIds.map((gardenId) => {
        const garden = this.state.gardens.find((candidate) => candidate.id === gardenId);
        if (!garden) return `garden=${gardenId}; unavailable`;
        return (
          `garden=${garden.id}; bridge=${garden.bridge}; gardenComponentIfGameEndedNow=${garden.points}; `
          + `alreadyIncludedInCurrentTotalIfScoredNow=yes; activationImmediatePointsDeltaBeforeReward=0; `
          + `rewardTree=${describePublicEffects(garden.effects)}; `
          + "exact follow-up choices=see round-end-followups generated from authority legal actions"
        );
      });
      const followupLines = legal.flatMap((action) => {
        let projected = structuredClone(this.state);
        try {
          projected = applyAction(projected, action).state;
          stabilizeAutomaticEffects(projected);
        } catch {
          return [];
        }
        const followups = getLegalActions(projected).map((next) => {
          let meaning = semanticRoundEndMeaning(projected, next);
          if (next.type === "selectCastleTileAction") {
            const room = projected.board.castleRooms.find((candidate) => candidate.id === next.room);
            const slot = room?.slots.find((candidate) => candidate.rowId === next.rowId);
            meaning = `resolve ${next.room} Row ${castleRowNumber(next.room, next.rowId)}: ${describePublicEffects(slot?.effects ?? [])}`;
          }
          return `${semanticRoundEndAction(this.state, action)} -> ${semanticRoundEndAction(projected, next)}: ${meaning}`;
        });
        return followups.length
          ? followups
          : [`${semanticRoundEndAction(this.state, action)} -> automatic/boundary`];
      });
      return {
        schemaVersion:"natural-decision-surface-v2",
        currentActions,
        modelFacts:{
          version:1,
          coverage:"complete-current-decision",
          phaseScope:"round-end",
          facts:[
            {
              kind:"TurnFact", id:"round-end-turn", title:"Round-end turn",
              data:{
                round:this.state.round,
                turn:this.state.turn,
                actorSeat:seat,
                pendingOwnerSeat:pending?.owner ?? seat,
                nextRoundFirstActorSeat:this.state.round < 3
                  ? this.state.turnOrder[0]
                  : null,
              },
            },
            {
              kind:"ResourceSnapshot", id:"round-end-actor", title:"Actor state",
              data:{
                resources:clone(player.resources),
                influence:player.influence,
                points:player.points,
                scoreIfEndedNow:clone(actorScore),
              },
            },
            currentSemanticOptionsFact,
            {
              kind:"CurrentTargetFact", id:"round-end-options", title:"Round-end choices",
             data:{
               semanticActions:currentActions,
               pendingGardens:pendingGardenIds.map((gardenId) => {
                 const garden = this.state.gardens.find((item) => item.id === gardenId);
                  return garden ? {
                    garden:garden.id,
                    bridge:garden.bridge,
                    points:garden.points,
                   effects:clone(garden.effects),
                 } : { garden:gardenId, unavailable:true };
               }),
               immediateFollowUps:followupLines,
             },
           },
          ],
        },
        narrativeSections:[
          {
            id:"round-end-turn",
            title:"轮末庭园结算与行动者",
            lines:[
              `round ${this.state.round}/3; turn ${this.state.turn}; phase roundEnd; resolving seat ${seat} (${player.name})`,
              `pendingOwnerSeat=${pending?.owner ?? seat}; pendingEffect=${pending ? describePublicEffect(pending.effect) : "none"}`,
              `nextRoundFirstActorSeat=${this.state.round < 3 ? this.state.turnOrder[0] : "game-finished-after-resolution"}; this DecisionFrame contains only the current round-end owner choices.`,
            ],
          },
          {
            id:"round-end-actions",
            title:"当前精确合法选择",
            lines:legal.length
              ? legal.map((action) => (
                `semantic action=${semanticRoundEndAction(this.state, action)}; meaning=${semanticRoundEndMeaning(this.state, action)}`
              ))
              : ["legal action=none; authority will advance automatically"],
          },
          {
            id:"round-end-state",
            title:"当前资源、若此刻结束分与公开对手",
            lines:[
              `actor seat ${seat}: coins=${player.resources.coins}; seals=${player.resources.seals}; food=${player.resources.food}; iron=${player.resources.iron}; pearl=${player.resources.pearl}; influence=${player.influence}; points=${player.points}; currentTotalIfScoredNow=${actorScore.total}.`,
              `current score components: duringGame=${actorScore.duringGame}; resources=${actorScore.resources}; timeTrack=${actorScore.timeTrack}; courtiers=${actorScore.courtiers}; warriors=${actorScore.warriors}; gardeners=${actorScore.gardeners}.`,
              ...this.state.players.filter((candidate) => candidate.id !== seat).map((candidate) => {
                const score = scores.find((item) => item.player === candidate.id)!;
                return `opponent seat ${candidate.id}: coins=${candidate.resources.coins}; seals=${candidate.resources.seals}; food=${candidate.resources.food}; iron=${candidate.resources.iron}; pearl=${candidate.resources.pearl}; influence=${candidate.influence}; points=${candidate.points}; currentTotalIfScoredNow=${score.total}.`;
              }),
              "Seal exchanges are optional auxiliary conversions; they do not activate or complete a garden, and the pending garden choices remain afterward.",
              `current lantern reward=${describePublicEffects(player.lanternEffects)}.`,
              `current well rewards=${describePublicEffects(Object.values(this.state.workspaces).find((workspace) => workspace.kind === "well")?.effects ?? [])}.`,
              "resource caps: seals=5; food=7; iron=7; pearl=7; coins=no cap. A gain above a cap is discarded rather than increasing the resource.",
              "if-ended resource score: floor((coins+seals)/5) + 1 for each food/iron/pearl at 3..6, or +2 when that resource is exactly 7.",
              "if-ended influence score: positions 0..5=0; 6..10=3; 11..14=6; 15..20=10..15 (position minus 5). Influence counts spaces from the heron, not printed score values.",
            ],
          },
          {
            id:"round-end-gardens",
            title:"当前待激活庭园与公开奖励树",
            lines:gardenLines.length ? gardenLines : ["none"],
          },
          {
            id:"round-end-followups",
            title:"选择庭园后的下一层公开选择",
            lines:followupLines.length ? followupLines : ["none before authority advances"],
          },
          {
            id:"round-end-rules",
            title:"轮末边界",
            lines:[
              "Choose every currently pending garden once in an authority-confirmed order; each chosen garden resolves its printed reward tree now.",
              "round_end_garden_order choices are recalculated after each selection; when exactly one garden remains, that remaining garden always uses choice=0 in the same semantic chain.",
              "Every listed follow-up action remains inside this same owner DecisionFrame until all of this owner's pending gardens and their reward choices finish.",
              "Every displayed follow-up option and numeric gain is already an authoritative public fact; only which option will be selected is uncommitted.",
              "Do not call a listed follow-up's numeric outcome unknown merely because it has not been selected; compare its exact counterfactual effect from the displayed facts.",
              "A displayed gain N points changes the points field immediately. A gardenComponentIfGameEndedNow is already included in currentTotalIfScoredNow and is not gained again on activation.",
              "All listed garden, castle-row, and resource choices remain inside this same DecisionFrame; do not label one of them Frame+1. Frame+1 begins only after authority delivers a new DecisionFrame.",
              "This is a round-end owner DecisionFrame, not a die draft or placement. After all owners finish, the next round starts with nextRoundFirstActorSeat.",
            ],
          },
        ],
        scoringDecisionFacts:clone(decisionFacts.scoringDecisionFacts),
        informationBoundaries:[
          "Only the current pending owner's public round-end choices are actionable.",
          "A later owner's choice or the next round is unavailable until authority advances.",
        ],
      };
    }
    const castleCourtierCount = player.members.filter((member) => (
      member.type === "courtier"
      && (member.location.startsWith("steward-") || member.location.startsWith("diplomat-") || member.location === "daimyo")
    )).length;
    const withCostFacts = (
      resource: keyof typeof player.resources,
      cost: number,
    ) => ({
      rawCost:{ [resource]:cost },
      spendable:{ [resource]:player.resources[resource] },
      remainingGap:{ [resource]:Math.max(0, cost - player.resources[resource]) },
      affordableNow:player.resources[resource] >= cost,
    });
    const resourceNames: Record<string, string> = {
      coins:"coins",
      seals:"seals",
      food:"food",
      iron:"iron",
      pearl:"pearl",
    };
    const describeEffect = (effect: unknown): string => {
      if (!effect || typeof effect !== "object") return String(effect);
      const item = effect as Record<string, unknown>;
      if (item.effect) {
        return `${item.source ?? "continuation"}: ${describeEffect(item.effect)}`;
      }
      if (!item.type && Array.isArray(item.effects)) {
        return `${item.id ?? "group"}: ${describeEffects(item.effects)}`;
      }
      const type = String(item.type ?? "unknown");
      if (type === "gain") {
        return `gain ${item.amount} ${resourceNames[String(item.resource)] ?? item.resource}`;
      }
      if (type === "gainChoice") {
        const resources = Array.isArray(item.resources) ? item.resources : [];
        return `make ${item.amount} resource selections from ${JSON.stringify(resources)}; choiceIds=[${resources.map((resource, option) => `${option}=${resource}`).join(",")}]; each selection gains exactly 1 chosen resource; the same resource may be selected again`;
      }
      if (type === "influence") return `gain ${item.amount} influence`;
      if (type === "gainPoints") return `gain ${item.amount} points`;
      if (type === "lantern") return "resolve the current lantern reward";
      if (type === "wellAction") return "resolve the current well rewards";
      if (type === "domainAction") return "choose one currently available personal-board row";
      if (type === "castleTileAction") return describeCastleRowSelection(String(item.color));
      if (type === "gardenActivation") return `activate gardens ${JSON.stringify(item.gardens ?? [])}`;
      if (type === "majorAction") return `start one ${item.action} action`;
      if (type === "castleRefresh") return `refresh castle room ${item.room}`;
      if (type === "daimyoReward") {
        const positions = Array.isArray(item.positions) ? item.positions : [];
        const card = DAIMYO_CARDS.find((candidate) => candidate.id === this.state.board.daimyoCard);
        const choices = positions.map((position, option) => {
          const index = Number(position);
          return `option${option}(position=${index})=${describeEffects(materialEffects(card?.rewards[index] ?? []))}`;
        });
        return `choose one open daimyo reward; positionChoices=[${choices.join(" | ") || "none"}]`;
      }
      if (type === "pay") {
        return item.optional
          ? `optional payment: choiceIds=[0=pay,1=decline]; pay ${item.amount} ${item.resource} to unlock ${describeEffects(item.effects)}; decline = skip every unlocked effect`
          : `pay ${item.amount} ${item.resource} → ${describeEffects(item.effects)}`;
      }
      if (type === "chooseOne") {
        const labels = Array.isArray(item.labels) ? item.labels : [];
        const options = Array.isArray(item.options) ? item.options : [];
        return `chooseOne: ${options.map((option, index) => (
          `choice ${index} (${labels[index] ?? `option ${index + 1}`}) = ${describeEffects(option)}`
        )).join(" OR ")}`;
      }
      if (type === "effectOrder") {
        return `resolve each effect once in chosen order: ${describeEffects(item.effects)}`;
      }
      if (type === "actionOrder") {
        const groups = Array.isArray(item.groups) ? item.groups : [];
        return `resolve each group once in chosen order: ${groups.map((group) => {
          const entry = group as Record<string, unknown>;
          return `${entry.id ?? "group"} = ${describeEffects(entry.effects)}`;
        }).join(" | ")}`;
      }
      return type;
    };
    const describeEffects = (effects: unknown): string => {
      if (!Array.isArray(effects) || effects.length === 0) return "none";
      return effects.map(describeEffect).join(" → ");
    };
    const hasListedContinuation = (value: unknown): boolean => {
      if (Array.isArray(value)) return value.some(hasListedContinuation);
      if (!value || typeof value !== "object") return false;
      const item = value as Record<string, unknown>;
      if (item.effect) return hasListedContinuation(item.effect);
      if (!item.type && Array.isArray(item.effects)) {
        return hasListedContinuation(item.effects);
      }
      const type = String(item.type ?? "");
      if (["gain", "influence", "gainPoints"].includes(type)) return false;
      if (type === "actionOrder") return hasListedContinuation(item.groups);
      return [
        "gainChoice",
        "lantern",
        "wellAction",
        "domainAction",
        "castleTileAction",
        "gardenActivation",
        "effectOrder",
        "daimyoReward",
        "pay",
        "majorAction",
        "castleRefresh",
        "chooseOne",
      ].includes(type);
    };
    const listedMajorActions = (value: unknown): string[] => {
      const found = new Set<string>();
      const visit = (candidate: unknown): void => {
        if (Array.isArray(candidate)) {
          candidate.forEach(visit);
          return;
        }
        if (!candidate || typeof candidate !== "object") return;
        const item = candidate as Record<string, unknown>;
        if (item.type === "majorAction" && typeof item.action === "string") {
          found.add(item.action);
        }
        for (const key of ["effect", "effects", "groups", "options"]) {
          visit(item[key]);
        }
      };
      visit(value);
      return [...found].sort();
    };
    const containsEffectType = (value: unknown, wanted: string): boolean => {
      if (Array.isArray(value)) {
        return value.some((candidate) => containsEffectType(candidate, wanted));
      }
      if (!value || typeof value !== "object") return false;
      const item = value as Record<string, unknown>;
      if (item.type === wanted) return true;
      return ["effect", "effects", "groups", "options"].some((key) => (
        containsEffectType(item[key], wanted)
      ));
    };
    const promotionSources = buildCourtierPromotionSources(this.state, seat);
    const bridgeEnds = getLegalActions(this.state).flatMap((action) => {
      if (action.type !== "draftDie") return [];
      const dice = this.state.bridges[action.bridge];
      const die = action.end === "left" ? dice[0] : dice[dice.length - 1];
      if (!die) return [];
      return [{
        bridge:action.bridge,
        end:action.end,
        die:clone(die),
        triggersLantern:action.end === "left",
      }];
    });
    const availableWorkspaces = Object.values(this.state.workspaces)
      .filter((workspace) => (
        workspace.active
        && (workspace.owner === undefined || workspace.owner === seat)
        && (
          workspace.capacity === "unlimited"
          || workspace.dice.length < workspace.capacity
        )
      ))
      .map((workspace) => {
        const placementOutcomesByColor = workspace.allowedColors.flatMap((color) => {
          const entries = bridgeEnds.flatMap((draft) => {
            if (draft.die.color !== color) return [];
            const coinDelta = placementCoinDelta(workspace, draft.die);
            const coinDeficit = Math.max(0, -(player.resources.coins + coinDelta));
            if (coinDeficit > player.resources.seals) return [];
            return [{
              bridge:draft.bridge,
              end:draft.end,
              dieId:draft.die.id,
              dieValue:draft.die.value,
              coinDelta,
              requiredPrePlacementSealExchanges:coinDeficit,
              coinsAfterPlacement:player.resources.coins + coinDeficit + coinDelta,
            }];
          });
          if (entries.length === 0) return [];
          const castleGroups = workspace.kind === "castle"
            ? workspace.effectGroupsByColor?.[color]
            : undefined;
          const domainRow = workspace.id === `p${seat}-domain-courtier`
            ? "courtier"
            : workspace.id === `p${seat}-domain-gardener`
              ? "gardener"
              : workspace.id === `p${seat}-domain-warrior`
                ? "warrior"
                : undefined;
          const effects = domainRow
            ? domainRowEffects(this.state, seat, domainRow)
            : workspace.effectsByColor?.[color] ?? workspace.effects;
          return [{
            dieColor:color,
            entries,
            orderedEffects:clone(castleGroups ?? effects),
          }];
        });
        return {
          id:workspace.id,
          label:workspace.label,
          kind:workspace.kind,
          allowedColors:clone(workspace.allowedColors),
          printedValue:workspace.printedValue,
          referenceValue:placementReferenceValue(workspace),
          topDie:clone(workspace.dice.at(-1) ?? null),
          capacity:workspace.capacity,
          occupied:workspace.dice.length,
          grantsOnlyListedEffects:true,
          placementOutcomesByColor,
        };
      });
    const currentScores = scoreGame(this.state);
    const currentScore = currentScores.find((score) => score.player === seat)!;
    const nextInfluenceCheckpoint = INFLUENCE_CHECKPOINTS.find(
      (checkpoint) => checkpoint.position > player.influence,
    );
    const resourceLine = (
      `coins=${player.resources.coins}; seals=${player.resources.seals}; `
      + `food=${player.resources.food}; iron=${player.resources.iron}; `
      + `pearl=${player.resources.pearl}; influence=${player.influence}; `
      + `points=${player.points}`
    );
    const memberLine = (type: "courtier" | "gardener" | "warrior") => (
      player.members
        .filter((member) => member.type === type)
        .map((member) => `${member.id}@${member.location}`)
        .join(", ") || "none"
    );
    const diceLines = bridgeEnds.map((draft) => (
      `bridge=${draft.bridge}; end=${draft.end}; die=${draft.die.id}; `
      + `color=${draft.die.color}; value=${draft.die.value}; `
      + (
        draft.triggersLantern
          ? `left-end lantern=yes; lantern reward=${describeEffects(player.lanternEffects)}`
          : "left-end lantern=no"
      )
    ));
    const placementLines = availableWorkspaces.flatMap((workspace) => (
      workspace.placementOutcomesByColor.map((outcome) => {
        const coinFacts = outcome.entries.map((entry) => (
          `die=${entry.dieId}@${entry.bridge}-${entry.end}; value=${entry.dieValue}; `
          + `coinDelta=${entry.coinDelta}; requiredPrePlacementSealExchanges=${entry.requiredPrePlacementSealExchanges}; `
          + `coinsAfter=${entry.coinsAfterPlacement}`
        )).join(" | ");
        const continuation = hasListedContinuation(outcome.orderedEffects)
          ? "listed continuation=yes; follow only the listed reward tree"
          : "listed continuation=no; chain stops after the listed reward unless a later BgAct check reveals state-dependent new information";
        const majorActions = listedMajorActions(outcome.orderedEffects);
        return (
          `workspace=${workspace.id} (${workspace.label}); kind=${workspace.kind}; `
          + `dieColor=${outcome.dieColor}; printedValue=${workspace.printedValue}; referenceValue=${workspace.referenceValue}; occupied=${workspace.occupied}/${workspace.capacity}; `
          + `coin arithmetic=[${coinFacts}]; reward=${describeEffects(outcome.orderedEffects)}; `
          + `explicitMajorActions=[${majorActions.join(",") || "none"}]; `
          + continuation
        );
      })
    ));
    const gardenerSources = player.members
      .filter((member) => member.type === "gardener" && member.location === "domain")
      .map((member) => member.id);
    const gardenerLines = this.state.gardens
      .filter((garden) => !garden.gardeners.includes(seat))
      .map((garden) => {
        const gap = Math.max(0, garden.foodCost - player.resources.food);
        return (
          `gardener: source=${gardenerSources.join(", ") || "none"}; target=${garden.id}; `
          + `foodCost=${garden.foodCost}; foodNow=${player.resources.food}; gap=${gap}; `
          + `affordableAtFrameStart=${gap === 0}; requiredPriorDelta=${JSON.stringify({ food:gap })}; gardenScoreIfGameEndedAfterDeployment=${garden.points}; `
          + `immediatePointsDeltaBeforeReward=0; `
          + `roundEndReactivation=rounds1-2 only when bridge ${garden.bridge} retains at least one die; `
          + `bridgeDiceNow=${this.state.bridges[garden.bridge].length}; drafting a ${garden.bridge} die now reduces that count by 1; `
          + `reward=${describeEffects(garden.effects)}`
        );
      });
    const warriorSources = player.members
      .filter((member) => member.type === "warrior" && member.location === "domain")
      .map((member) => member.id);
    const warriorLines = this.state.trainingYards
      .filter((yard) => yard.capacity === "unlimited" || yard.warriors.length < yard.capacity)
      .map((yard) => {
        const gap = Math.max(0, yard.ironCost - player.resources.iron);
        return (
          `warrior: source=${warriorSources.join(", ") || "none"}; target=${yard.id}; `
          + `ironCost=${yard.ironCost}; ironNow=${player.resources.iron}; gap=${gap}; `
          + `affordableAtFrameStart=${gap === 0}; requiredPriorDelta=${JSON.stringify({ iron:gap })}; warriorScoreDeltaIfGameEndedAfterThisPlacement=${yard.warriorValue * castleCourtierCount}; `
          + `warriorFinalFormula=yardValue(${yard.warriorValue})×finalCastleCourtierCount; `
          + `currentCastleCourtierMultiplier=${castleCourtierCount}; `
          + `reward=${describeEffects(yard.effects)}`
        );
      });
    const courtierSources = player.members
      .filter((member) => member.type === "courtier" && member.location === "domain")
      .map((member) => member.id);
    const promotionLines = promotionSources.flatMap((source) => (
      source.targets.map((target) => (
        `courtier promote: source=${source.member}@${source.location}; target=${target.id}; `
        + `levels=${target.levels}; pearlCost=${(target.rawCost as { pearl: number }).pearl}; `
        + `pearlNow=${player.resources.pearl}; gap=${(target.remainingGap as { pearl: number }).pearl}; `
        + `affordableAtFrameStart=${target.affordableNow}; requiredPriorDelta=${JSON.stringify(target.remainingGap)}; courtierScoreIfGameEndedAfterThisAction=${target.scoreBefore}→${target.scoreAfter}; `
        + `immediatePointsDeltaBeforeArrivalReward=0; `
        + `arrival reward=${describeEffects(target.publicArrivalEffects)}`
      ))
    ));
    const memberActionLines = [
      "affordableAtFrameStart uses resources before the selected die, lantern, placement, and reward prefix; false does not make a route illegal. Recompute affordability after every known prior effect in that same current chain, and require the accumulated gains to cover requiredPriorDelta.",
      "courtier action window: recruit at most once and promote at most once, in either legal order.",
      (
        `courtier recruit: source=${courtierSources.join(", ") || "none"}; target=gate; `
        + `coinsCost=2; coinsNow=${player.resources.coins}; `
        + `affordableAtFrameStart=${player.resources.coins >= 2}; requiredPriorDelta=${JSON.stringify({ coins:Math.max(0, 2 - player.resources.coins) })}; courtierScoreIfGameEndedAfterRecruit=1; immediatePointsDelta=0`
      ),
      ...promotionLines,
      ...gardenerLines,
      ...warriorLines,
    ];
    const personalBoardLines = [
      `lantern reward: ${describeEffects(player.lanternEffects)}`,
      `courtier row: workspace=p${seat}-domain-courtier; dieColor=coral; reward=${describeEffects(domainRowEffects(this.state, seat, "courtier"))}`,
      `gardener row: workspace=p${seat}-domain-gardener; dieColor=black; reward=${describeEffects(domainRowEffects(this.state, seat, "gardener"))}`,
      `warrior row: workspace=p${seat}-domain-warrior; dieColor=white; reward=${describeEffects(domainRowEffects(this.state, seat, "warrior"))}`,
    ];
    const castleChoiceSelectors: CastleActionSelector[] = ["black", "white", "coral", "light", "any"];
    const castleChoiceCatalog = castleChoiceSelectors.map((selector) => {
      const targets = availableCastleTileActions(this.state, selector).map((target) => (
        `${target.room}/Row${castleRowNumber(target.room, target.rowId)}=${describeEffects(target.effects)}`
      ));
      return `action=choose_castle_row; selector=${selector}; choices=[${targets.join(" | ") || "none"}]`;
    });
    const decisionEffectTrees = [
      ...availableWorkspaces.flatMap((workspace) => (
        workspace.placementOutcomesByColor.map((outcome) => outcome.orderedEffects)
      )),
      ...this.state.gardens.map((garden) => garden.effects),
      ...this.state.trainingYards.map((yard) => yard.effects),
      ...promotionSources.flatMap((source) => (
        source.targets.map((target) => target.publicArrivalEffects)
      )),
    ];
    const domainChoiceCatalog = decisionEffectTrees.some((effects) => (
      containsEffectType(effects, "domainAction")
    ))
      ? availableDomainRows(this.state).map((row, option) => (
        `kind=personal_row_choice; choiceId=${option}; row=${row}; meaning=${describeEffects(domainRowEffects(this.state, seat, row))}`
      ))
      : [];
    const actorTurnsRemaining = (() => {
      if (this.state.phase === "finished") return 0;
      const orderIndex = this.state.turnOrder.indexOf(this.state.currentPlayer);
      if (orderIndex < 0 || this.state.turnOrder.length === 0) return 0;
      const remainingPlacements = Math.max(
        0,
        Object.values(this.state.bridges).reduce((total, dice) => total + dice.length, 0) - 3,
      );
      const currentTurnInProgress = this.state.phase !== "draft" && Boolean(this.state.draftedDie);
      let count = currentTurnInProgress && this.state.currentPlayer === seat ? 1 : 0;
      const startOffset = currentTurnInProgress ? 1 : 0;
      for (let offset = 0; offset < remainingPlacements; offset += 1) {
        const player = this.state.turnOrder[
          (orderIndex + startOffset + offset) % this.state.turnOrder.length
        ];
        if (player === seat) count += 1;
      }
      return count;
    })();
    const actorOrderIndex = this.state.turnOrder.indexOf(seat);
    const nextActorSeat = actorOrderIndex >= 0 && this.state.turnOrder.length > 0
      ? this.state.turnOrder[(actorOrderIndex + 1) % this.state.turnOrder.length]
      : seat;
    const bridgeDiceBefore = Object.values(this.state.bridges).reduce(
      (total, dice) => total + dice.length,
      0,
    );
    const currentActionGuaranteedEndsRound = bridgeDiceBefore === 4;
    const currentActionGuaranteedEndsGame = (
      currentActionGuaranteedEndsRound && this.state.round === 3
    );
    const modelFactsComplexityExceeded = Symbol("model-facts-complexity-exceeded");
    const boundaryFactData = {
      currentActions,
      currentActionGuaranteedEndsRound,
      currentActionGuaranteedEndsGame,
      nextActorSeat:currentActionGuaranteedEndsRound ? null : nextActorSeat,
      actorLaterPersonalTurnsRemaining:Math.max(0, actorTurnsRemaining - 1),
      successorSummary:currentActionGuaranteedEndsGame
        ? "Current route ends the game; no later DecisionFrame."
        : currentActionGuaranteedEndsRound
          ? "Current route ends the round; next-round actor is known only after round-end resolution."
          : `After current route: nextActorSeat=${nextActorSeat}; roundEnds=false; gameEnds=false; actorSeat=${seat} still has ${Math.max(0, actorTurnsRemaining - 1)} later personal turns.`,
    };
    const buildBoundedOrdinaryTurnProjection = () => {
      const compactEffect = (value:unknown):unknown => {
        if (Array.isArray(value)) return value.map(compactEffect);
        if (!value || typeof value !== "object") return value;
        const item = value as Record<string, unknown>;
        if (item.effect) {
          return ["source", item.source ?? null, compactEffect(item.effect)];
        }
        if (!item.type && Array.isArray(item.effects)) {
          return ["group", item.id ?? null, compactEffect(item.effects)];
        }
        const type = String(item.type ?? "unknown");
        if (type === "gain") return [type, item.resource, item.amount];
        if (type === "gainChoice") return [type, item.amount, item.resources];
        if (type === "influence" || type === "gainPoints") return [type, item.amount];
        if (["lantern", "wellAction", "domainAction"].includes(type)) return [type];
        if (type === "castleTileAction") return [type, item.color];
        if (type === "gardenActivation") return [type, item.gardens];
        if (type === "majorAction") return [type, item.action];
        if (type === "castleRefresh") return [type, item.room];
        if (type === "pay") {
          return [type, item.resource, item.amount, Boolean(item.optional), compactEffect(item.effects)];
        }
        if (type === "chooseOne") {
          return [type, item.labels, compactEffect(item.options)];
        }
        if (type === "effectOrder") return [type, compactEffect(item.effects)];
        if (type === "actionOrder") {
          return [type, compactEffect(item.groups)];
        }
        if (type === "daimyoReward") {
          const card = DAIMYO_CARDS.find((candidate) => candidate.id === this.state.board.daimyoCard);
          const positions = Array.isArray(item.positions) ? item.positions : [];
          return [type, positions.map((position) => {
            const index = Number(position);
            return [index, compactEffect(materialEffects(card?.rewards[index] ?? []))];
          })];
        }
        return [type];
      };
      const compactEffectText = (value:unknown):string => {
        if (!Array.isArray(value)) return String(value ?? "none");
        if (typeof value[0] !== "string") {
          return value.map(compactEffectText).join(" -> ");
        }
        const [type, ...fields] = value;
        if (type === "gain") return `+${String(fields[1])} ${String(fields[0])}`;
        if (type === "gainChoice") {
          const resources = fields[1] as unknown[];
          return `choose ${String(fields[0])}x[${resources.map((resource, index) => (
            `${index}=${String(resource)}`
          )).join("|")}]`;
        }
        if (type === "influence") {
          const amount = Number(fields[0] ?? 0);
          const crosses = Boolean(
            nextInfluenceCheckpoint
            && player.influence < nextInfluenceCheckpoint.position
            && player.influence + amount >= nextInfluenceCheckpoint.position
          );
          return `+${String(fields[0])} influence` + (crosses && nextInfluenceCheckpoint
            ? `; checkpoint ${nextInfluenceCheckpoint.position} requires ${nextInfluenceCheckpoint.cost} seals: choice=0 pay and cross, choice=1 stop before checkpoint`
            : "");
        }
        if (type === "gainPoints") return `+${String(fields[0])} points`;
        if (type === "lantern") {
          return `lantern[${compactEffectText(compactEffect(player.lanternEffects))}]`;
        }
        if (type === "wellAction") return "well rewards";
        if (type === "domainAction") return "choose domain row";
        if (type === "castleTileAction") return describeCastleRowSelection(String(fields[0]));
        if (type === "gardenActivation") return `activate gardens ${JSON.stringify(fields[0])}`;
        if (type === "majorAction") return `start ${String(fields[0])} action`;
        if (type === "castleRefresh") return `refresh ${String(fields[0])}`;
        if (type === "pay") {
          const payment = `pay ${String(fields[1])} ${String(fields[0])}`;
          const reward = compactEffectText(fields[3]);
          return fields[2]
            ? `optional payment[choice=0:${payment} => ${reward} | choice=1:decline]`
            : `${payment} => ${reward}`;
        }
        if (type === "chooseOne") {
          const options = Array.isArray(fields[1]) ? fields[1] : [];
          return `choose one[${options.map((option, index) => (
            `${index}:${compactEffectText(option)}`
          )).join(" | ")}]`;
        }
        if (type === "effectOrder") {
          const effects = Array.isArray(fields[0]) ? fields[0] : [];
          return `choose order[${effects.map((effect, choice) => (
            `choice=${choice}:first ${compactEffectText(effect)}`
          )).join(" | ")}]`;
        }
        if (type === "actionOrder") {
          const groups = Array.isArray(fields[0]) ? fields[0] : [];
          return `choose action[${groups.map((group, choice) => (
            `choice=${choice}:${compactEffectText(group)}`
          )).join(" | ")}]`;
        }
        if (type === "daimyoReward") {
          const rewards = Array.isArray(fields[0]) ? fields[0] : [];
          return `choose daimyo[${rewards.map((reward) => {
            const pair = Array.isArray(reward) ? reward : [];
            return `choice=${String(pair[0])}:${compactEffectText(pair[1])}`;
          }).join(" | ")}]`;
        }
        if (type === "group") return `${String(fields[0])}:${compactEffectText(fields[1])}`;
        if (type === "source") return `${String(fields[0])}:${compactEffectText(fields[1])}`;
        return type;
      };
      type BoundedPlacementEntry = (
        typeof availableWorkspaces[number]["placementOutcomesByColor"][number]["entries"][number]
      );
      const reachableMemberActions = (
        workspace:string,
        entry:BoundedPlacementEntry,
        expectedFamilies:readonly string[],
      ):string[] => {
        const families = new Set<string>();
        const expected = new Set(expectedFamilies);
        const seen = new Set<string>();
        const startTurn = this.state.turn;
        let visits = 0;
        const complete = () => (
          expected.size === families.size
          && [...expected].every((family) => families.has(family))
        );
        const walk = (state:GameState, depth:number):void => {
          if (complete() || depth > 8 || state.currentPlayer !== seat || state.turn !== startTurn) return;
          stabilizeAutomaticEffects(state);
          const key = canonicalFingerprint(serializeGame(state));
          if (seen.has(key)) return;
          seen.add(key);
          const pendingFamilies = new Set(listedMajorActions(state.pendingEffects));
          if (
            !complete()
            && ![...expected].some((family) => pendingFamilies.has(family))
          ) return;
          visits += 1;
          if (visits > 256) throw modelFactsComplexityExceeded;
          const legalActions = getLegalActions(state);
          const deterministic = deterministicRouteContinuation(state, legalActions);
          for (const action of deterministic ? [deterministic] : legalActions) {
            if (action.type === "beginMajorAction") {
              families.add(
                action.mode === "gardener" ? "gardener"
                  : action.mode === "warrior" ? "warrior" : "courtier",
              );
              if (complete()) return;
              continue;
            }
            if (action.type === "draftDie") continue;
            try {
              const next = applyAction(structuredClone(state), action).state;
              walk(next, depth + 1);
            } catch (error) {
              if (error === modelFactsComplexityExceeded) throw error;
              // Authority-rejected branches are not reachable openings.
            }
          }
        };
        try {
          let projected = structuredClone(this.state);
          projected = applyAction(projected, {
            type:"draftDie", bridge:entry.bridge, end:entry.end,
          }).state;
          stabilizeAutomaticEffects(projected);
          for (let index = 0; index < entry.requiredPrePlacementSealExchanges; index += 1) {
            projected = applyAction(projected, {
              type:"exchangeSeal", receive:"coins",
            }).state;
            stabilizeAutomaticEffects(projected);
          }
          projected = applyAction(projected, {
            type:"placeDie", workspace,
          }).state;
          walk(projected, 0);
        } catch (error) {
          if (error === modelFactsComplexityExceeded) throw error;
          return [];
        }
        return [...families].sort();
      };
      const placementOptions = availableWorkspaces.flatMap((workspace) => (
        workspace.placementOutcomesByColor.map((outcome) => {
          const listed = listedMajorActions(outcome.orderedEffects);
          return {
            workspace:workspace.id,
            dieColor:outcome.dieColor,
            dice:outcome.entries.map((entry) => {
              const reached = reachableMemberActions(workspace.id, entry, listed);
              return {
                side:entry.end,
                value:entry.dieValue,
                coinChange:entry.coinDelta,
                sealsToCoins:entry.requiredPrePlacementSealExchanges,
                coinsAfter:entry.coinsAfterPlacement,
                memberActionsOpened:reached,
                memberActionsBlocked:listed.filter((family) => !reached.includes(family)),
              };
            }),
            openingReward:compactEffectText(compactEffect(outcome.orderedEffects)),
          };
        })
      ));
      const boundedFamilies = new Set(
        placementOptions.flatMap((option) => (
          option.dice.flatMap((die) => die.memberActionsOpened)
        )),
      );
      const continuationKinds = (
        value:unknown,
        resourceChoiceKind = "resource_choice",
      ):string[] => {
        if (Array.isArray(value)) {
          return value.flatMap((item) => continuationKinds(item, resourceChoiceKind));
        }
        if (!value || typeof value !== "object") return [];
        const item = value as Record<string, unknown>;
        if (item.effect) return continuationKinds(item.effect, resourceChoiceKind);
        if (!item.type && Array.isArray(item.effects)) {
          return continuationKinds(item.effects, resourceChoiceKind);
        }
        const type = String(item.type ?? "");
        if (type === "gainChoice") {
          return Array.from(
            { length:Math.max(1, Number(item.amount ?? 1)) },
            () => resourceChoiceKind,
          );
        }
        if (type === "pay") return ["optional_payment"];
        if (type === "wellAction") {
          return continuationKinds(this.state.workspaces.well.effects, "well_resource");
        }
        if (type === "castleTileAction") return ["choose_castle_row"];
        if (type === "actionOrder") return ["action_order"];
        if (type === "effectOrder") return ["effect_order"];
        if (type === "daimyoReward") return ["daimyo_reward"];
        if (type === "domainAction") return ["personal_row_choice"];
        if (type === "majorAction") return [`start_${String(item.action)}_action`];
        return [];
      };
      const promotionGroups = (() => {
        const groups = new Map<string, {
          members:string[];
          from:string;
          targets:Array<Record<string, unknown>>;
        }>();
        for (const source of promotionSources) {
          const targets = source.targets.map((target) => ({
            target:target.id,
            levels:target.levels,
            pearlCost:(target.rawCost as { pearl:number }).pearl,
            frameStartAffordable:target.affordableNow,
            gameEndScoreBefore:target.scoreBefore,
            gameEndScoreAfter:target.scoreAfter,
            gameEndScoreDelta:target.scoreAfter - target.scoreBefore,
            reward:compactEffectText(compactEffect(target.publicArrivalEffects)),
            continuationKinds:continuationKinds(target.publicArrivalEffects),
          }));
          const key = canonicalFingerprint({ from:source.location, targets });
          const group = groups.get(key);
          if (group) group.members.push(source.member);
          else groups.set(key, { members:[source.member], from:source.location, targets });
        }
        return [...groups.values()];
      })();
      const recruitPromotionSummary = courtierSources.length ? (() => {
        const recruitedMember = courtierSources[0];
        const projected = structuredClone(this.state);
        const member = projected.players[seat].members.find((candidate) => (
          candidate.id === recruitedMember
        ));
        if (!member) return "";
        member.location = "gate";
        const source = buildCourtierPromotionSources(projected, seat)
          .find((candidate) => candidate.member === recruitedMember);
        return (source?.targets ?? []).map((target) => (
          `${target.id}{晋升${target.levels}层;花${(target.rawCost as { pearl:number }).pearl}珍珠;`
          + `终局+${target.scoreAfter - target.scoreBefore};奖励=${compactEffectText(compactEffect(target.publicArrivalEffects))}}`
        )).join(" | ");
      })() : "";
      const courtierTargets = boundedFamilies.has("courtier") ? {
        recruit:{
          sourceIds:courtierSources,
          target:"gate",
          coinsCost:2,
          frameStartAffordable:player.resources.coins >= 2,
          gameEndScoreDelta:1,
        },
        promote:promotionGroups,
      } : null;
      const gardenerTargets = boundedFamilies.has("gardener")
        ? this.state.gardens
          .filter((garden) => !garden.gardeners.includes(seat))
          .map((garden) => ({
            target:garden.id,
            cost:{ food:garden.foodCost },
            affordableNow:player.resources.food >= garden.foodCost,
            gameEndScoreDeltaIfPlacedNow:garden.points,
            scoreTiming:"game_end",
            bridge:garden.bridge,
            reward:compactEffectText(compactEffect(garden.effects)),
            continuationKinds:continuationKinds(garden.effects),
          }))
        : [];
      const warriorTargets = boundedFamilies.has("warrior")
        ? this.state.trainingYards
          .filter((yard) => yard.capacity === "unlimited" || yard.warriors.length < yard.capacity)
          .map((yard) => ({
            target:yard.id,
            cost:{ iron:yard.ironCost },
            affordableNow:player.resources.iron >= yard.ironCost,
            yardValue:yard.warriorValue,
            courtierMultiplier:castleCourtierCount,
            gameEndScoreDeltaIfPlacedNow:yard.warriorValue * castleCourtierCount,
            scoreTiming:"game_end",
            reward:compactEffectText(compactEffect(yard.effects)),
            continuationKinds:continuationKinds(yard.effects),
          }))
        : [];
      const fact = (
        kind:string,
        id:string,
        title:string,
        data:Record<string, unknown>,
      ) => ({ kind, id, title, data });
      const scoreContext = {
        current:{
          total:currentScore.total,
          components:{
            duringGame:currentScore.duringGame,
            resources:currentScore.resources,
            timeTrack:currentScore.timeTrack,
            courtiers:currentScore.courtiers,
            warriors:currentScore.warriors,
            gardeners:currentScore.gardeners,
          },
        },
        caps:{ seals:5, food:7, iron:7, pearl:7, coins:"none" },
        scoringRules:{
          courtier:"仅按最终位置：gate=1, steward=3, diplomat=6, daimyo=10",
          gardener:"已放置园丁按所在园地印刷分",
          warrior:`已放置武士×训练场价值×最终城内家臣数；当前乘数=${castleCourtierCount}`,
          resources:"钱币+家纹每满5=1分；食物/铁/珍珠各自3..6=1分、7=2分。每条路线后按最终资源重算本分项，并用新总分减当前总分；不能只加目标标称正分。",
          influence:"影响力为从白鹭起点前进的格数：0..5=0分；6..10=3分；11..14=6分；15..20=10..15分（格位减5）",
        },
        influenceCheckpoint:{
          current:player.influence,
          currentScore:currentScore.timeTrack,
          nextPosition:nextInfluenceCheckpoint?.position ?? null,
          sealsCost:nextInfluenceCheckpoint?.cost ?? 0,
          sealsNow:player.resources.seals,
          affordableNow:nextInfluenceCheckpoint
            ? player.resources.seals >= nextInfluenceCheckpoint.cost
            : true,
          unpaidMeaning:nextInfluenceCheckpoint
            ? "到达检查点时 choice=0 支付所需家纹并越过，choice=1 停在检查点前；未支付时不能把被阻止的影响力计入收益。"
            : "没有剩余影响力检查点。",
        },
      };
      const opportunityFrame = buildOpportunityFrameData({
        placementOptions,
        memberActions:[
          ...(courtierTargets ? [{
            family:"courtier",
            data:{
              memberAction:"家臣",
              windowRule:(
                "每次家臣行动最多招募1次、晋升1次，可按当前合法顺序执行；每次选择后按当时资源和位置重新判断。"
                + (recruitPromotionSummary
                  ? ` 刚招募到gate的同一名家臣可在本窗口继续晋升：${recruitPromotionSummary}`
                  : "")
              ),
              recruit:courtierTargets.recruit.sourceIds.length
                ? courtierTargets.recruit : null,
              promote:courtierTargets.promote,
            },
          }] : []),
          ...(gardenerTargets.length ? [{
            family:"gardener",
            data:{
              memberAction:"园丁",
              memberIds:gardenerSources,
              targets:gardenerTargets.map((target) => ({
                target:target.target,
                cost:target.cost,
                affordableNow:target.affordableNow,
                immediateScoreDelta:0,
                gameEndScoreDelta:target.gameEndScoreDeltaIfPlacedNow,
                scoreTiming:target.scoreTiming,
                followUp:target.reward,
                continuationKinds:target.continuationKinds,
              })),
            },
          }] : []),
          ...(warriorTargets.length ? [{
            family:"warrior",
            data:{
              memberAction:"武士",
              memberIds:warriorSources,
              scoringMeaning:`武士终局分=训练场价值×最终城内家臣数；当前城内家臣数=${castleCourtierCount}。`,
              targets:warriorTargets.map((target) => ({
                target:target.target,
                cost:target.cost,
                affordableNow:target.affordableNow,
                yardValue:target.yardValue,
                courtierMultiplier:target.courtierMultiplier,
                immediateScoreDelta:0,
                gameEndScoreDelta:target.gameEndScoreDeltaIfPlacedNow,
                scoreTiming:target.scoreTiming,
                followUp:target.reward,
                continuationKinds:target.continuationKinds,
              })),
            },
          }] : []),
        ],
        scoreContext,
        endCondition:{
          gameEndsAfterRound:3,
          currentRound:this.state.round,
          actorPersonalActionsIncludingCurrent:actorTurnsRemaining,
          actorLaterPersonalActions:Math.max(0, actorTurnsRemaining - 1),
          currentActionGuaranteedEndsRound,
          currentActionGuaranteedEndsGame,
          meaning:(
            `本局没有第4轮；当前行动后本座位还有${Math.max(0, actorTurnsRemaining - 1)}`
            + "次个人行动，除非当前行动直接结束游戏。"
          ),
        },
        opponents:currentScores
          .filter((score) => score.player !== seat)
          .map((score) => ({ seat:score.player, currentTotal:score.total })),
      });
      const ordinaryOptions = semanticOptions.map((option) => {
        if (option.action !== "take_die") return clone(option);
        const declared = option.reward.declared as Record<string, unknown> | undefined;
        if (
          !declared
          || typeof declared.dieId !== "string"
          || typeof declared.value !== "number"
          || typeof declared.lanternTriggered !== "boolean"
        ) {
          throw new Error("ordinary modelFacts require typed take-die metadata");
        }
        return {
          choiceId:option.choiceId,
          action:option.action,
          args:clone(option.args),
          die:{
            id:declared.dieId,
            value:declared.value,
            lanternTriggered:declared.lanternTriggered,
            ...(declared.lanternTriggered ? {
              lantern:compactEffectText(compactEffect(player.lanternEffects)),
            } : {}),
          },
          boundary:clone(option.boundary),
        };
      });
      return {
        version:1,
        coverage:"complete-current-decision",
        phaseScope:"ordinary-turn",
        projectionMode:"bounded-summary",
        facts:[
          fact("TurnFact", "turn", "Turn", {
            round:this.state.round,
            turn:this.state.turn,
            actorSeat:seat,
            actorTurnsRemaining,
          }),
          fact("ResourceSnapshot", "actor-state", "Actor", {
            resources:{
              coins:player.resources.coins,
              seals:player.resources.seals,
              food:player.resources.food,
              iron:player.resources.iron,
              pearl:player.resources.pearl,
              influence:player.influence,
              points:player.points,
            },
            currentTotalIfScoredNow:currentScore.total,
            personalBoard:{
              lantern:compactEffectText(compactEffect(player.lanternEffects)),
            },
          }),
          fact("CurrentTargetFact", "current-semantic-options", "Options", {
            options:ordinaryOptions,
          }),
          fact("DynamicScoreFact", "score-context", "当前计分与终局", opportunityFrame.scoring),
          fact("CurrentTargetFact", "current-opportunities", "当前成员行动机会", opportunityFrame.opportunities),
          fact("CurrentTargetFact", "other-placements", "其他当前放置", opportunityFrame.otherPlacements),
          fact("CurrentTargetFact", "choice-catalog", "Continuation choices", {
            semanticAction:"choose_reward",
            castleRows:availableCastleTileActions(this.state, "any").map((target) => ({
              choice:`${target.room}/${target.rowId}`,
              reward:compactEffectText(compactEffect(target.effects)),
              selectors:castleChoiceSelectors.filter((selector) => (
                selector !== "any"
                && availableCastleTileActions(this.state, selector).some((candidate) => (
                  candidate.room === target.room && candidate.rowId === target.rowId
                ))
              )),
            })),
            domain:domainChoiceCatalog.length ? availableDomainRows(this.state).map((row, choice) => ({
              choice,
              row,
              reward:compactEffectText(compactEffect(domainRowEffects(this.state, seat, row))),
            })) : [],
          }),
          fact("AuthorityBoundaryFact", "decision-boundary", "Boundary", boundaryFactData),
        ],
      };
    };
    const narrativeSections = [
      {
        id:"turn",
        title:"轮次、回合与行动者",
        lines:[
          `round ${this.state.round}/3; turn ${this.state.turn}; phase ${this.state.phase}; actor seat ${seat} (${player.name})`,
          `actor turns remaining this round (including current turn)=${actorTurnsRemaining}; current turn is final personal turn=${actorTurnsRemaining === 1 ? "yes" : "no"}`,
          `bridgeDiceBefore=${bridgeDiceBefore}; currentActionGuaranteedEndsRound=${currentActionGuaranteedEndsRound ? "yes" : "no"}; currentActionGuaranteedEndsGame=${currentActionGuaranteedEndsGame ? "yes" : "no"}.`,
          currentActionGuaranteedEndsGame
            ? "successorBoundary=game finished after this action resolves; there is no later player DecisionFrame."
            : currentActionGuaranteedEndsRound
              ? "successorBoundary=roundEnd and then next round; ordinary cyclic nextActor does not apply, and next-round order is determined only after current effects and round-end resolution."
              : `cyclicTurnOrder=${this.state.turnOrder.join("→")}→repeat; after the current DecisionFrame nextActorSeat=${nextActorSeat}; opponentActionsBeforeActorReturns=${Math.max(0, this.state.turnOrder.length - 1)}.`,
          "the current DecisionFrame ends after this die placement and every effect it triggers; dice and shared workspaces for a later personal turn are not guaranteed until a new Frame arrives.",
        ],
      },
      {
        id:"state",
        title:"当前游戏状态",
        lines:[
          `actor seat ${seat}: ${resourceLine}`,
          `courtiers: ${memberLine("courtier")}`,
          `gardeners: ${memberLine("gardener")}`,
          `warriors: ${memberLine("warrior")}`,
          ...this.state.players
            .filter((candidate) => candidate.id !== seat)
            .map((candidate) => {
              const candidateScore = currentScores.find((score) => score.player === candidate.id)!;
              return `opponent seat ${candidate.id}: coins=${candidate.resources.coins}; `
              + `food=${candidate.resources.food}; iron=${candidate.resources.iron}; `
              + `pearl=${candidate.resources.pearl}; influence=${candidate.influence}; points=${candidate.points}; `
              + `currentTotalIfScoredNow=${candidateScore.total}`;
            }),
          `pending effects before this decision: ${this.state.pendingEffects.length === 0 ? "none" : this.state.pendingEffects.map((pending) => describeEffect(pending.effect)).join(" | ")}`,
        ],
      },
      {
        id:"dice",
        title:"骰子区可选骰子",
        lines:diceLines,
      },
      {
        id:"placements",
        title:"可放置位置（奖励与钱币差）",
        lines:[
          "coinDelta = die value - referenceValue; positive gains coins, negative pays coins. Reference is the top die if occupied, otherwise printedValue; the well always uses printedValue.",
          "A requiredPrePlacementSealExchanges value above 0 means exchange exactly that many seals for coins before placing the die; this exchange and placement remain in the same current DecisionFrame.",
          "A workspace label identifies its personal-board row; it never grants a courtier, gardener, or warrior action unless the listed reward explicitly starts that action.",
          ...placementLines,
        ],
      },
      {
        id:"member-actions",
        title:"三类家族行动的当前花费、得分与奖励",
        lines:memberActionLines,
      },
      {
        id:"personal-board",
        title:"个人版图（为规划而重复列出）",
        lines:personalBoardLines,
      },
      {
        id:"resources-and-scoring",
        title:"资源、声誉与计分",
        lines:[
          `current resources: ${resourceLine}`,
          "caps: seals=5; food=7; iron=7; pearl=7; coins=no cap.",
          `influence=${player.influence}; current influence score=${currentScore.timeTrack}; current total if scored now=${currentScore.total}.`,
          `influence checkpoints=${INFLUENCE_CHECKPOINTS.map((checkpoint) => `${checkpoint.position}:pay ${checkpoint.cost} seals`).join(" | ")}; crossing one requires an immediate pay-or-stop choice; if unpaid, influence stops immediately before that checkpoint.`,
          "influence checkpoint choiceIds=[0=pay and cross,1=stop before checkpoint].",
          nextInfluenceCheckpoint
            ? `next influence checkpoint: position=${nextInfluenceCheckpoint.position}; sealsCost=${nextInfluenceCheckpoint.cost}; sealsNow=${player.resources.seals}; affordableAtFrameStart=${player.resources.seals >= nextInfluenceCheckpoint.cost}.`
            : "next influence checkpoint: none; influence may advance to the track cap.",
          "each courtier scores only its final location, never cumulative locations along its path: gate=1; steward=3; diplomat=6; daimyo=10.",
          "gardeners score each occupied garden's printed points only if the game ended in that state; deployment itself adds 0 immediate points before the printed reward resolves, and rounds 1-2 may later reactivate eligible gardens.",
          "garden occupancy: each player may occupy a given garden at most once; different players may occupy the same garden, so an opponent does not remove or reserve that garden for this seat.",
          `warriors score only warriors already placed in yards × yard value × castle courtiers; each source member still in the domain scores 0 until placed; current castle courtiers=${castleCourtierCount}.`,
          "resource score: each complete 5 coins+seals=1; food/iron/pearl each score 1 at 3..6 and 2 at 7.",
        ],
      },
      {
        id:"continuation-choice-catalog",
        title:"Continuation Choice Catalog（精确选择标识）",
        lines:[
          "This catalog is factual reference, not an assertion that every selector occurs on every route. Use only the selector or domain action actually reached by the selected current chain.",
          ...castleChoiceCatalog,
          ...domainChoiceCatalog,
          ...(domainChoiceCatalog.length ? [
            "kind=personal_row_choice is used only when a listed reward literally says to choose a personal-board row; never add it merely because place_die already selected a p*-domain-* workspace.",
          ] : []),
          "choose_castle_row uses only visible room and one-based Row N; the package resolves internal card and row identifiers. personal_row_choice numeric options are recalculated after a row is used.",
        ],
      },
    ];
    // Complete legal states can exceed the old 8 KiB sampled optimization
    // target. Keep that target in focused size tests, not in snapshot creation.
    // The host enforces the common 24 KiB modelFacts admission boundary.
    const modelFacts = buildMinimalBoardFrame(this.state, seat, {
      actorTurnsRemaining,
      laterTurnsRemaining:Math.max(0, actorTurnsRemaining - 1),
      endsRound:currentActionGuaranteedEndsRound,
      endsGame:currentActionGuaranteedEndsGame,
      nextActor:currentActionGuaranteedEndsRound ? null : nextActorSeat,
    });
    return {
      schemaVersion:"natural-decision-surface-v2",
      currentActions,
      modelFacts,
      narrativeSections,
      scoringDecisionFacts:clone(decisionFacts.scoringDecisionFacts),
      informationBoundaries:[
        "Unlisted effects/choices stay unknown until authority execution.",
        "Facts only; no route is ranked.",
      ],
    };
  }

  strategicOpportunityCatalog(): Array<Record<string, unknown>> {
    return [
      {
        family:"recruit_courtier", variant:"recruit", reachability:"unknown",
        routeQuery:{ stepsContain:[{ op:"beginMajorAction", mode:"recruit" }] },
        factualCosts:{ coins:{ min:2, max:2 } }, factualImmediateEffects:["MemberMoved"],
        scoringEngineChanges:[{ ruleId:"courtier-location", inputId:"courtier.location", label:"courtier location", before:"domain", after:"gate", timing:"game_end", enables:["promote"] }],
      },
      {
        family:"promote_courtier", variant:"promote", reachability:"unknown",
        routeQuery:{ stepsContain:[{ op:"beginMajorAction", mode:"promote" }] },
        factualCosts:{ pearl:{ min:2, max:5 } }, factualImmediateEffects:["MemberMoved"],
        scoringEngineChanges:[{ ruleId:"courtier-location", inputId:"courtier.location", label:"courtier destination", before:"gate", after:"castle", timing:"game_end", formula:"first_level=3; second_level=6; third_level=10" }],
      },
      {
        family:"deploy_gardener", variant:"gardener", reachability:"unknown",
        routeQuery:{ stepsContain:[{ op:"beginMajorAction", mode:"gardener" }] },
        factualCosts:{ food:{ min:Math.min(...this.state.gardens.map((garden) => garden.foodCost)), max:Math.max(...this.state.gardens.map((garden) => garden.foodCost)) } }, factualImmediateEffects:["MemberMoved"],
        scoringEngineChanges:[{ ruleId:"gardener-printed", inputId:"garden.points", label:"printed garden points", before:"unoccupied", after:"occupied", timing:"game_end" }],
      },
      {
        family:"deploy_warrior", variant:"warrior", reachability:"unknown",
        routeQuery:{ stepsContain:[{ op:"beginMajorAction", mode:"warrior" }] },
        factualCosts:{ iron:{ min:Math.min(...this.state.trainingYards.map((yard) => yard.ironCost)), max:Math.max(...this.state.trainingYards.map((yard) => yard.ironCost)) } }, factualImmediateEffects:["MemberMoved"],
        scoringEngineChanges:[{ ruleId:"warrior-yard", inputId:"trainingYard.warriorValue", label:"yard warrior value", before:"unowned", after:"owned", timing:"game_end", formula:"ownedWarriorsInYard * yardValue * castleCourtiersExcludingGate" }],
      },
    ];
  }

  private causalTrace(events: GameEvent[]): TurnProgram["causalTrace"] {
    return events.map((event, index) => {
      if (event.type === "ResourceChanged") {
        return { step:index + 1, source:"resource", effect:"resource", delta:{ [event.resource]:event.amount } };
      }
      if (event.type === "InfluenceChanged") {
        return { step:index + 1, source:"influence", effect:"influence", delta:{ influence:event.amount } };
      }
      if (event.type === "ChoiceRequired") {
        return {
          step:index + 1,
          source:event.effect.source,
          effect:event.effect.effect.type,
          ...(event.effect.effect.type === "majorAction"
            ? { majorAction:event.effect.effect.action }
            : {}),
        };
      }
      if (event.type === "EffectResolved") {
        return {
          step:index + 1,
          source:event.source,
          effect:event.effect.type,
          ...(event.effect.type === "majorAction"
            ? { majorAction:event.effect.action }
            : {}),
        };
      }
      return { step:index + 1, source:event.type, effect:event.type };
    });
  }

  private causalTraceForAction(
    before: GameState,
    action: GameAction,
    events: GameEvent[],
  ): TurnProgram["causalTrace"] {
    const trace = this.causalTrace(events);
    if (action.type !== "chooseEffectOption") return trace;
    const pending = before.pendingEffects[0];
    if (!pending || pending.id !== action.effectId) return trace;
    const choice = {
      choiceType:pending.effect.type,
      choiceSource:pending.source,
    };
    if (trace.length === 0) {
      return [{ step:1, source:pending.source, effect:pending.effect.type, ...choice }];
    }
    return trace.map((event, index) => index === 0 ? { ...event, ...choice } : event);
  }

  private factualResourceFlow(
    causalEvents: TurnProgram["causalTrace"],
  ): Pick<TurnProgram, "factualCosts" | "factualGains"> {
    const factualCosts: Record<string, number> = {};
    const factualGains: Record<string, number> = {};
    for (const event of causalEvents) {
      for (const [resource, delta] of Object.entries(event.delta ?? {})) {
        if (delta < 0) {
          factualCosts[resource] = (factualCosts[resource] ?? 0) - delta;
        }
        if (delta > 0) {
          factualGains[resource] = (factualGains[resource] ?? 0) + delta;
        }
      }
    }
    return { factualCosts, factualGains };
  }

  private programScoringFacts(
    seat: number,
    before: GameState,
    after: GameState,
    causalEvents: TurnProgram["causalTrace"],
  ): Pick<TurnProgram, "factualCosts" | "factualGains" | "immediateEffects" | "immediateScoreDelta" | "endNowScoreDelta" | "scoringEngineChanges"> {
    const scoreBefore = scoreGame(before).find((item) => item.player === seat)!;
    const scoreAfter = scoreGame(after).find((item) => item.player === seat)!;
    const beforePlayer = before.players[seat];
    const afterPlayer = after.players[seat];
    const resourceFlow = this.factualResourceFlow(causalEvents);
    const changes: Array<Record<string, unknown>> = [];
    for (const member of beforePlayer.members) {
      const moved = afterPlayer.members.find((candidate) => candidate.id === member.id);
      if (!moved || moved.location === member.location) continue;
      changes.push({
        ruleId: member.type === "courtier" ? "courtier-location"
          : member.type === "gardener" ? "gardener-printed" : "warrior-yard",
        inputId: `member.${member.id}.location`,
        label: `${member.type} location`,
        before: member.location,
        after: moved.location,
        timing: "game_end",
      });
    }
    const castleCourtierCount = (state: GameState) => state.players[seat].members.filter((member) => (
      member.type === "courtier"
      && (member.location.startsWith("steward-")
        || member.location.startsWith("diplomat-")
        || member.location === "daimyo")
    )).length;
    const multiplierBefore = castleCourtierCount(before);
    const multiplierAfter = castleCourtierCount(after);
    if (multiplierBefore !== multiplierAfter) {
      changes.push({
        ruleId: "warrior-yard",
        inputId: "castleCourtiersExcludingGate",
        label: "warrior multiplier",
        before: multiplierBefore,
        after: multiplierAfter,
        timing: "game_end",
        formula: "ownedWarriorsInYard * yardValue * castleCourtiersExcludingGate",
      });
    }
    return {
      factualCosts: resourceFlow.factualCosts,
      factualGains: resourceFlow.factualGains,
      immediateEffects: [...new Set(causalEvents.map((event) => event.effect))].sort(),
      immediateScoreDelta: scoreAfter.duringGame - scoreBefore.duringGame,
      endNowScoreDelta: scoreAfter.total - scoreBefore.total,
      scoringEngineChanges: changes,
    };
  }

  private decisionExplorer(
    seat = this.state.currentPlayer,
    initialState:GameState = this.state,
    authorityBase:GameState = initialState,
    pageSizeOverride?:number,
    maxTimeMsOverride?:number,
  ): ReturnType<typeof createDecisionExplorer> {
    const decisionId = this.decisionId();
    const initial = structuredClone(initialState);
    return createDecisionExplorer(initial, seat, {
      decisionId: () => decisionId,
      cloneState: (state: GameState) => structuredClone(state),
      canonicalAuthorityKey: (state: GameState) => this.canonicalStateKey(state),
      getLegalActions: (state: GameState) => {
        stabilizeAutomaticEffects(state);
        const actions = getLegalActions(state);
        // Seal exchange is legal during the turn, but moving it inside the
        // reversible source/target/confirm sequence creates equivalent path
        // permutations and breaks the semantic action's atomic boundary.
        // The same exchange remains available immediately before the member
        // action, so the explorer keeps every outcome while choosing one
        // canonical semantic ordering.
        return state.actionFlow
          ? actions.filter((action) => action.type !== "exchangeSeal")
          : actions;
      },
      applyAction: (state: GameState, action: GameAction) => applyAction(state, action),
      classifyState: (_before: GameState, current: GameState, legalActions: GameAction[]) => {
        const boundaryReason = decisionBoundary(authorityBase, current);
        if (boundaryReason) return { status:"boundary", boundaryReason };
        const pending = current.pendingEffects[0];
        const mayDecline = pending?.effect.type === "pay" && pending.effect.optional
          && legalActions.some((action) => action.type === "chooseEffectOption" && action.effectId === pending.id && action.option === 1);
        return { status:mayDecline ? "may_stop" : "must_continue" };
      },
      isDeclineAction: (action: GameAction, state: GameState) => {
        if (!state) return false;
        const pending = state.pendingEffects[0];
        return action.type === "chooseEffectOption" && action.option === 1
          && pending?.effect.type === "pay" && pending.effect.optional && action.effectId === pending.id;
      },
      routeFilterForAction: (action: GameAction) => {
        const step = actionToStep(action);
        return [{
          op: step.op,
          ...(typeof step.mode === "string" ? { mode: step.mode } : {}),
        }];
      },
      strategicOpportunityForAction: (action: GameAction) => {
        if (action.type !== "beginMajorAction") return null;
        const semanticFamily = action.mode === "recruit" ? "recruit_courtier"
          : action.mode === "promote" ? "promote_courtier"
            : action.mode === "gardener" ? "deploy_gardener"
              : "deploy_warrior";
        const range = (values: number[]) => ({ min:Math.min(...values), max:Math.max(...values) });
        const factualCosts = action.mode === "recruit" ? { coins:{ min:2, max:2 } }
          : action.mode === "promote" ? { pearl:{ min:2, max:5 } }
            : action.mode === "gardener" ? { food:range(this.state.gardens.map((garden) => garden.foodCost)) }
              : { iron:range(this.state.trainingYards.map((yard) => yard.ironCost)) };
        return {
          family: semanticFamily,
          variant: action.mode,
          routeQuery: { stepsContain: [{ op: "beginMajorAction", mode: action.mode }] },
          factualCosts,
          factualImmediateEffects: ["MemberMoved"],
          scoringEngineChanges: action.mode === "recruit"
            ? [{ ruleId:"courtier-location", inputId:"courtier.location", label:"courtier location", before:"domain", after:"gate", timing:"game_end", enables:["promote"] }]
            : action.mode === "promote"
              ? [{ ruleId:"courtier-location", inputId:"courtier.location", label:"courtier destination", before:"gate", after:"castle", timing:"game_end", formula:"first_level=3; second_level=6; third_level=10" }, { ruleId:"warrior-yard", inputId:"castleCourtiersExcludingGate", label:"warrior multiplier", before:"current", after:"current_plus_one_when_promoted", timing:"game_end" }]
              : action.mode === "gardener"
                ? [{ ruleId:"gardener-printed", inputId:"garden.points", label:"printed garden points", before:"unoccupied", after:"occupied", timing:"game_end" }, { ruleId:"gardener-round-end", inputId:"bridge.dieRemaining", label:"round-end reactivation", before:"unknown", after:"depends_on_remaining_bridge_die", timing:"round_end" }]
                : [{ ruleId:"warrior-yard", inputId:"trainingYard.warriorValue", label:"yard warrior value", before:"unowned", after:"owned", timing:"game_end", formula:"ownedWarriorsInYard * yardValue * castleCourtiersExcludingGate" }],
        };
      },
      strategicOpportunityCatalog: () => this.strategicOpportunityCatalog(),
      actionFingerprint: (action: GameAction) => canonicalFingerprint(action),
      traceTransition: (before: GameState, action: GameAction, _after: GameState, events: GameEvent[]) => this.causalTraceForAction(before, action, events),
      projectAuthorityOutcome: (_seat: number, _before: GameState, boundary: GameState) => this.outcomeFor(authorityBase, boundary),
      projectProgramFacts: (
        projectedSeat: number,
        _before: GameState,
        boundary: GameState,
        _actions: GameAction[],
        causalEvents: TurnProgram["causalTrace"],
      ) => this.programScoringFacts(projectedSeat, authorityBase, boundary, causalEvents),
      projectSeatView: () => ({}),
      projectPublicView: () => ({}),
    }, {
      maxNodes:explorerLimit("BGLAB_WHITE_EXPLORER_MAX_NODES", 20_000),
      maxTimeMs:maxTimeMsOverride
        ?? explorerLimit("BGLAB_WHITE_EXPLORER_MAX_TIME_MS", 5_000),
      pageSize:pageSizeOverride
        ?? explorerLimit("BGLAB_WHITE_EXPLORER_PAGE_SIZE", 12),
    });
  }


  private createDecisionMapForFrame(
    frame: { gameId: string; decisionId: string; seat: number; stateHash: string },
    seat = this.state.currentPlayer,
    decisionMapPageSize = 20,
    explorer = this.decisionExplorer(seat),
  ): ReturnType<typeof createDecisionMap> {
    const loaded = new Map<string, Record<string, unknown>>();
    let graphCoverageComplete = false;
    const source = ({ proposal = {}, pageSize = 20 }: { proposal?: Record<string, unknown>; pageSize?: number } = {}) => {
      const requested = Array.isArray(proposal.steps) ? proposal.steps as ActionStep[] : [];
      const target = requested[0]?.op === BEGIN_STEP.op ? requested.slice(1) : requested;
      const targetFilter = target.map((step) => {
        const { type, ...fields } = stepToAction(step);
        return { op:type, ...fields };
      });
      const matchesTarget = (candidate: Record<string, unknown>) => {
        const steps = candidate.steps as ActionStep[];
        return steps.length === target.length + 1
          && canonicalFingerprint(steps.slice(1)) === canonicalFingerprint(target);
      };
      const limit = Math.max(1, Math.min(Number(pageSize) || 20, 20));
      const maxPages = target.length ? 8 : 2;
      let pages = 0;
      let queryExhausted = false;
      while (!queryExhausted && pages < maxPages && (target.length === 0 || ![...loaded.values()].some(matchesTarget))) {
        const result = explorer.enumerateRoutes({
          offset:target.length ? pages * limit : loaded.size,
          limit,
          ...(targetFilter.length ? { stepsContain:targetFilter } : {}),
        }) as Record<string, unknown>;
        const programs = Array.isArray(result.programs) ? result.programs as Array<Record<string, unknown>> : [];
        for (const program of programs) {
          const internalSteps = Array.isArray(program.steps) ? program.steps as Array<Record<string, unknown>> : [];
          const finishActionEndedChain = internalSteps.some((step) => (
            (step.action as GameAction | undefined)?.type === "finishMajorAction"
          ));
          const endedMajorAction = endedMajorActionFromTrace(
            internalSteps.flatMap((step) => (
              Array.isArray(step.causalEvents) ? step.causalEvents : []
            )),
          );
          const outcome = {
            ...clone(program.outcome as Record<string, unknown>),
            ...(finishActionEndedChain
              ? {
                finishActionEndedChain:true,
                unexecutedActionAbandoned:true,
                ...(endedMajorAction ? { endedMajorAction } : {}),
              }
              : {}),
          };
          const candidate = {
            ...clone(program),
            steps: [clone(BEGIN_STEP), ...internalSteps.map((step) => actionToStep(step.action as GameAction))],
            causalTrace: internalSteps
              .flatMap((step) => Array.isArray(step.causalEvents) ? step.causalEvents : [])
              .map((event, index) => ({ ...clone(event), step:index + 1 })),
            outcome,
            netOutcome: clone(outcome),
            complete: true,
          };
          loaded.set(canonicalFingerprint(candidate.steps), candidate);
        }
        pages += 1;
        graphCoverageComplete = result.coverageStatus === "complete";
        // Explorer coverage describes whether the authority graph is fully
        // enumerated.  It does not mean this paginated response contained all
        // programs.  Keep loading while a next page exists; otherwise an early
        // coverage-complete page can falsely erase a legal proposal prefix.
        queryExhausted = !result.nextCursor || programs.length === 0;
      }
      return {
        programs: [...loaded.values()],
        coverageStatus: queryExhausted && graphCoverageComplete ? "complete" : "unknown",
      };
    };
    const decisionMap = createDecisionMap({ frame, pageSize:decisionMapPageSize, programs:source });
    return {
      ...decisionMap,
      enumerateRoutes:(request: Record<string, unknown> = {}) => {
        const proposal = request.proposal && typeof request.proposal === "object"
          ? request.proposal as Record<string, unknown>
          : {};
        const rawSteps = Array.isArray(proposal.steps) ? proposal.steps as ActionStep[] : [];
        const withBegin = rawSteps[0]?.op === BEGIN_STEP.op
          ? rawSteps
          : [clone(BEGIN_STEP), ...rawSteps];
        const steps = withBegin.map((step) => actionToStep(stepToAction(step)));
        const preview = this.validateTransaction(this.decisionId(), { steps });
        const validatedPrefix = Array.isArray(preview.validatedPrefix)
          ? preview.validatedPrefix
          : [];
        if (preview.ok && preview.complete && validatedPrefix.length > 0) {
          const outcome = clone(preview.outcome ?? {});
          decisionMap.issueComplete({
            intent:typeof proposal.intent === "string" ? proposal.intent : "",
            steps:clone(validatedPrefix),
            outcome,
            netOutcome:clone(outcome),
            causalTrace:clone(preview.causalTrace ?? []),
            factualCosts:clone(preview.factualCosts ?? {}),
            factualGains:clone(preview.factualGains ?? {}),
            immediateEffects:clone(preview.immediateEffects ?? []),
            immediateScoreDelta:preview.immediateScoreDelta ?? 0,
            endNowScoreDelta:preview.endNowScoreDelta ?? 0,
            scoringEngineChanges:clone(preview.scoringEngineChanges ?? []),
            complete:true,
          });
        }
        return decisionMap.enumerateRoutes({
          proposal:{ ...proposal, steps },
          page:request.page,
          pageSize:request.pageSize,
        });
      },
    };
  }

  private semanticChoiceKind(state:GameState, action:GameAction):string | null {
    const pending = action.type === "chooseEffectOption"
      ? state.pendingEffects.find((item) => item.id === action.effectId)
      : undefined;
    if (action.type === "chooseEffectOption" && !pending) return null;
    return semanticChoiceKindForEngineStep(
      actionToStep(action),
      pending?.effect.type,
    );
  }

  private semanticChoiceDetails(state:GameState, action:GameAction):Record<string, unknown> {
    if (action.type === "selectCastleTileAction") {
      const room = state.board.castleRooms.find((candidate) => candidate.id === action.room);
      const row = room?.slots.find((candidate) => candidate.rowId === action.rowId);
      return {
        choiceId:`${action.room}:row-${castleRowNumber(action.room, action.rowId)}`,
        floor:action.room.startsWith("diplomat-") ? "diplomat" : "steward",
        room:Number(action.room.split("-").at(-1)),
        row:castleRowNumber(action.room, action.rowId),
        reward:clone(row?.effects ?? []),
      };
    }
    if (action.type === "refreshCastleRoom") {
      return { choiceId:action.room, room:action.room };
    }
    if (action.type !== "chooseEffectOption") return {};
    const pending = state.pendingEffects.find((item) => item.id === action.effectId);
    if (!pending) return { choiceId:action.option, orderedIndex:action.option };
    const effect = pending.effect;
    if (effect.type === "gainChoice") {
      return {
        choiceId:effect.resources[action.option],
        orderedIndex:action.option,
        reward:{ type:"gain", resource:effect.resources[action.option], amount:1 },
        selectionsRemainingAfter:Math.max(0, effect.amount - 1),
      };
    }
    if (effect.type === "effectOrder") {
      return {
        choiceId:`effect-${action.option}`,
        orderedIndex:action.option,
        reward:clone(effect.effects[action.option]),
      };
    }
    if (effect.type === "actionOrder") {
      const group = effect.groups[action.option];
      return {
        choiceId:group?.id ?? `group-${action.option}`,
        orderedIndex:action.option,
        reward:clone(group?.effects ?? []),
      };
    }
    if (effect.type === "chooseOne") {
      return {
        choiceId:effect.labels[action.option] ?? `choice-${action.option}`,
        orderedIndex:action.option,
        reward:clone(effect.options[action.option] ?? []),
      };
    }
    if (effect.type === "daimyoReward") {
      const position = effect.positions[action.option];
      const card = DAIMYO_CARDS.find((candidate) => candidate.id === state.board.daimyoCard);
      return {
        choiceId:`position-${position}`,
        orderedIndex:action.option,
        position,
        reward:clone(materialEffects(card?.rewards[position] ?? [])),
      };
    }
    if (effect.type === "domainAction") {
      const row = availableDomainRows(state)[action.option];
      return {
        choiceId:row,
        orderedIndex:action.option,
        row,
        reward:clone(row ? domainRowEffects(state, state.currentPlayer, row) : []),
      };
    }
    if (effect.type === "gardenActivation") {
      const gardenId = effect.gardens[action.option];
      const garden = state.gardens.find((candidate) => candidate.id === gardenId);
      return {
        choiceId:gardenId,
        orderedIndex:action.option,
        garden:gardenId,
        reward:clone(garden?.effects ?? []),
      };
    }
    if (effect.type === "pay") {
      return {
        choiceId:action.option === 0 ? "pay" : "decline",
        orderedIndex:action.option,
        payment:action.option === 0 ? { [effect.resource]:effect.amount } : {},
        reward:action.option === 0 ? clone(effect.effects) : [],
      };
    }
    if (effect.type === "influence") {
      const player = state.players[state.currentPlayer];
      const checkpoint = INFLUENCE_CHECKPOINTS.find((candidate) => (
        player.influence < candidate.position
        && player.influence + effect.amount >= candidate.position
      ));
      return {
        choiceId:action.option === 0 ? "pay" : "stop",
        orderedIndex:action.option,
        checkpoint:checkpoint ? clone(checkpoint) : null,
      };
    }
    return { choiceId:action.option, orderedIndex:action.option };
  }

  private semanticPlan(
    action:string,
    args:Record<string, unknown>,
    choiceId:string,
    engineActions:GameAction[],
    choiceDetails:Record<string, unknown>,
    projectedState?:GameState,
    events?:GameEvent[],
  ):SemanticOptionPlan {
    return {
      choiceId,
      action,
      args:clone(args),
      engineActions:engineActions.map((engineAction) => clone(engineAction)),
      choiceDetails:clone(choiceDetails),
      ...(projectedState ? { projectedState } : {}),
      ...(events ? { events:events.map((event) => clone(event)) } : {}),
    };
  }

  private semanticOptionFromActions(
    state:GameState,
    action:string,
    args:Record<string, unknown>,
    choiceId:string,
    engineActions:GameAction[],
    choiceDetails:Record<string, unknown>,
  ):SemanticOptionProjection {
    let projected = state;
    const events:GameEvent[] = [];
    for (const engineAction of engineActions) {
      const transition = applyEnumeratedAction(projected, engineAction);
      projected = transition.state;
      events.push(...transition.events);
    }
    const cost:Record<string, number> = {};
    const gains:Record<string, number> = {};
    for (const event of events) {
      if (event.type !== "ResourceChanged") continue;
      if (event.amount < 0) cost[event.resource] = (cost[event.resource] ?? 0) - event.amount;
      if (event.amount > 0) gains[event.resource] = (gains[event.resource] ?? 0) + event.amount;
    }
    const beforePlayer = state.players[state.currentPlayer];
    const afterPlayer = projected.players[state.currentPlayer];
    const stateChanges = events.flatMap((event):Array<Record<string, unknown>> => {
      if (event.type === "DieDrafted") {
        if (action === "take_die") return [];
        return [{
          kind:"die_drafted", dieId:event.die.id, value:event.die.value,
          lanternTriggered:event.lanternTriggered,
        }];
      }
      if (event.type === "DiePlaced") return [{
        kind:"die_placed", dieId:event.die.id, value:event.die.value,
        location:event.workspace,
      }];
      if (event.type === "MemberMoved") return [{
        kind:"member_moved", member:event.member, from:event.from, to:event.to,
      }];
      if (event.type === "CardMoved") return [{
        kind:"card_moved", card:event.card, from:event.from, to:event.to,
      }];
      if (event.type === "EffectResolved") return [{
        kind:"effect_resolved", effect:clone(event.effect),
      }];
      if (event.type === "TurnStarted") return [{ kind:"turn_started", actorSeat:event.player, turn:event.turn }];
      if (event.type === "RoundEnded") return [{ kind:"round_ended", round:event.round }];
      if (event.type === "RoundStarted") return [{ kind:"round_started", round:event.round, actorSeat:event.firstPlayer }];
      if (event.type === "GameScored") return [{ kind:"game_scored", winner:event.winner }];
      return [];
    });
    const boundaryReason = decisionBoundary(state, projected);
    const influenceGain = Math.max(0, afterPlayer.influence - beforePlayer.influence);
    const pointGain = Math.max(0, afterPlayer.points - beforePlayer.points);
    return {
      choiceId,
      action,
      args:clone(args),
      cost,
      reward:{
        ...(Object.keys(gains).length ? { resources:gains } : {}),
        ...(influenceGain ? { influence:influenceGain } : {}),
        ...(pointGain ? { points:pointGain } : {}),
        ...(Object.hasOwn(choiceDetails, "reward")
          ? { declared:clone(choiceDetails.reward) }
          : {}),
        ...(stateChanges.length ? { stateChanges } : {}),
      },
      boundary:{
        status:boundaryReason ?? "same_decision",
        ...(projected.phase !== state.phase ? { phase:projected.phase } : {}),
        ...(projected.currentPlayer !== state.currentPlayer
          ? { actorSeat:projected.currentPlayer }
          : {}),
      },
    };
  }

  private promotionSemanticArgs(
    target:string,
    levels:number | undefined,
  ):Record<string, unknown> {
    // The destination room changes arrival rewards and the acquired domain card.
    // It is player intent, not an internal distinction to hide during ranking.
    return {
      target,
      levels,
    };
  }

  private projectMajorActionPlans(
    state:GameState,
    begin:Extract<GameAction, { type:"beginMajorAction" }>,
  ):SemanticOptionPlan[] {
    const startTransition = applyEnumeratedAction(state, begin);
    const started = startTransition.state;
    const sources = this.semanticMajorActionSources(started, begin.mode);
    return sources.flatMap((source) => {
      const sourceTransition = applyEnumeratedAction(started, source);
      const selected = sourceTransition.state;
      const targets = getLegalActions(selected).filter((action):action is Extract<GameAction, { type:"selectMajorActionTarget" }> => (
        action.type === "selectMajorActionTarget"
      ));
      return targets.flatMap((target) => {
        const targetTransition = applyEnumeratedAction(selected, target);
        const preview = targetTransition.state;
        const confirm = getLegalActions(preview).find((action):action is Extract<GameAction, { type:"confirmMajorAction" }> => (
          action.type === "confirmMajorAction"
        ));
        if (!confirm) return [];
        const confirmTransition = applyEnumeratedAction(preview, confirm);
        const semantic = begin.mode === "gardener" ? "deploy_gardener"
          : begin.mode === "warrior" ? "deploy_warrior"
            : begin.mode === "recruit" ? "recruit_courtier" : "promote_courtier";
        const args:Record<string, unknown> = semantic === "recruit_courtier"
          ? {}
          : semantic === "promote_courtier"
            ? this.promotionSemanticArgs(
              target.target,
              target.levels,
            )
            : { target:target.target };
        return [this.semanticPlan(
          semantic,
          args,
          `${semantic}:${target.target}:${target.levels ?? 1}`,
          [begin, source, target, confirm],
          { target:target.target, ...(target.levels ? { levels:target.levels } : {}) },
          confirmTransition.state,
          [
            ...startTransition.events,
            ...sourceTransition.events,
            ...targetTransition.events,
            ...confirmTransition.events,
          ],
        )];
      });
    });
  }

  private semanticMajorActionSources(
    state:GameState,
    mode:MajorActionMode,
  ):Array<Extract<GameAction, { type:"selectMajorActionSource" }>> {
    const rawSources = getLegalActions(state).filter((action):action is Extract<GameAction, { type:"selectMajorActionSource" }> => (
      action.type === "selectMajorActionSource"
    ));
    return mode !== "promote"
      ? rawSources
      : [...rawSources.reduce((unique, source) => {
        const member = state.players[state.currentPlayer].members.find((candidate) => (
          candidate.id === source.member
        ));
        // The semantic action chooses destination and levels. Starting rooms
        // on the same floor have identical costs and destination effects.
        const semanticSource = member ? courtierFloor(member.location) : source.member;
        if (!unique.has(semanticSource)) unique.set(semanticSource, source);
        return unique;
      }, new Map<number | string, Extract<GameAction, { type:"selectMajorActionSource" }>>()).values()];
  }

  private semanticOptionPlans(
    state:GameState,
    legal:GameAction[],
    deduplicate = true,
  ):SemanticOptionPlan[] {
    const options = legal.flatMap((engineAction):SemanticOptionPlan[] => {
      if (engineAction.type === "chooseStartingPair") {
        const offer = state.startingOffers[engineAction.offer];
        return [this.semanticPlan(
          "choose_starting_pair",
          { offer:engineAction.offer },
          `offer-${engineAction.offer}`,
          [engineAction],
          {
            choiceId:`offer-${engineAction.offer}`,
            offer:engineAction.offer,
            reward:{
              resourceCard:startingResourceFacts(offer.resourceCard),
              actionCard:clone(startingActionCard(offer.actionCard)),
            },
          },
        )];
      }
      if (engineAction.type === "draftDie") {
        const dice = state.bridges[engineAction.bridge];
        const die = engineAction.end === "left" ? dice[0] : dice[dice.length - 1];
        return [this.semanticPlan(
          "take_die",
          { color:engineAction.bridge, side:engineAction.end },
          `${engineAction.bridge}:${engineAction.end}`,
          [engineAction],
          {
            choiceId:`${engineAction.bridge}:${engineAction.end}`,
            reward:{
              dieId:die.id,
              value:die.value,
              lanternTriggered:engineAction.end === "left",
            },
          },
        )];
      }
      if (engineAction.type === "placeDie") {
        const workspace = state.workspaces[engineAction.workspace];
        const personal = /^p[0-9]+-domain-(courtier|gardener|warrior)$/.exec(engineAction.workspace);
        const castle = /^castle-(steward|diplomat)-([0-9]+)$/.exec(engineAction.workspace);
        const semanticAction = personal ? "place_die_personal"
          : castle ? "place_die_castle" : "place_die_public";
        const semanticArgs = personal ? { row:personal[1] }
          : castle ? { floor:castle[1], room:Number(castle[2]) }
            : { workspace:engineAction.workspace };
        return [this.semanticPlan(
          semanticAction,
          semanticArgs,
          `${semanticAction}:${Object.values(semanticArgs).join(":")}`,
          [engineAction],
          {
            choiceId:engineAction.workspace,
            location:engineAction.workspace,
            printedValue:workspace.printedValue,
            referenceValue:placementReferenceValue(workspace),
            coinDelta:placementCoinDelta(workspace, state.draftedDie!.die),
            occupied:workspace.dice.length,
            capacity:workspace.capacity,
            reward:clone(
              workspace.effectsByColor?.[state.draftedDie!.die.color]
              ?? workspace.effects
            ),
          },
        )];
      }
      if (engineAction.type === "exchangeSeal") {
        return [this.semanticPlan(
          "exchange_seal",
          { receive:engineAction.receive },
          engineAction.receive,
          [engineAction],
          { choiceId:engineAction.receive, receive:engineAction.receive },
        )];
      }
      if (
        engineAction.type === "chooseEffectOption"
        || engineAction.type === "refreshCastleRoom"
      ) {
        const kind = this.semanticChoiceKind(state, engineAction);
        if (!kind) return [];
        const details = this.semanticChoiceDetails(state, engineAction);
        const choice = engineAction.type === "chooseEffectOption"
          ? engineAction.option
          : engineAction.room;
        return [this.semanticPlan(
          "choose_reward",
          { kind, choice },
          `${kind}:${String(details.choiceId ?? choice)}`,
          [engineAction],
          details,
        )];
      }
      if (engineAction.type === "selectCastleTileAction") {
        const row = castleRowNumber(engineAction.room, engineAction.rowId);
        const floor = engineAction.room.startsWith("diplomat-") ? "diplomat" : "steward";
        const room = Number(engineAction.room.split("-").at(-1));
        return [this.semanticPlan(
          "choose_castle_row",
          { floor, room, row },
          `castle-row:${floor}:${room}:${row}`,
          [engineAction],
          this.semanticChoiceDetails(state, engineAction),
        )];
      }
      if (engineAction.type === "beginMajorAction") {
        return this.projectMajorActionPlans(
          state,
          engineAction,
        );
      }
      if (engineAction.type === "finishMajorAction") {
        return [this.semanticPlan(
          "finish_action",
          {},
          "finish_action",
          [engineAction],
          { choiceId:"finish-action", forfeitsUnexecutedAction:true },
        )];
      }
      if (engineAction.type === "finishResolution") {
        return [this.semanticPlan(
          "pass_turn",
          {},
          "pass_turn",
          [engineAction],
          { choiceId:"pass-turn" },
        )];
      }
      // Source/target/preview/cancel steps are reversible internal drafting,
      // not fresh DecisionFrame semantic actions.
      return [];
    });
    if (!deduplicate) return options;
    const unique = new Map<string, SemanticOptionPlan>();
    for (const option of options) {
      unique.set(canonicalFingerprint({ action:option.action, args:option.args }), option);
    }
    return [...unique.values()];
  }

  private projectSemanticOptions(
    state:GameState,
    legal:GameAction[],
  ):SemanticOptionProjection[] {
    return this.semanticOptionPlans(state, legal).map((plan) => (
      this.semanticOptionFromActions(
        state,
        plan.action,
        plan.args,
        plan.choiceId,
        plan.engineActions,
        plan.choiceDetails,
      )
    ));
  }

  private semanticRoutePlans(state:GameState):SemanticOptionPlan[] {
    stabilizeAutomaticEffects(state);
    const legal = getLegalActions(state).filter((action) => (
      action.type !== "cancelMajorActionSelection"
      && (!state.actionFlow || action.type !== "exchangeSeal")
    ));
    return this.semanticOptionPlans(state, legal, false)
      .sort((left, right) => canonicalFingerprint({ action:left.action, args:left.args })
        .localeCompare(canonicalFingerprint({ action:right.action, args:right.args })));
  }

  private semanticPlanState(state:GameState, plan:SemanticOptionPlan):GameState {
    if (plan.projectedState) return plan.projectedState;
    let projected = state;
    for (const action of plan.engineActions) {
      projected = applyEnumeratedAction(projected, action).state;
    }
    return projected;
  }

  private semanticRouteSuccessors(
    state:GameState,
  ):Array<{ token:SemanticRouteToken; state:GameState }> {
    stabilizeAutomaticEffects(state);
    const legal = getLegalActions(state).filter((action) => (
      action.type !== "cancelMajorActionSelection"
      && (!state.actionFlow || action.type !== "exchangeSeal")
    ));
    return legal.flatMap((action):Array<{ token:SemanticRouteToken; state:GameState }> => {
      if (action.type !== "beginMajorAction") {
        const token = this.semanticTokenForEngineAction(state, action);
        return token ? [{ token, state:applyEnumeratedAction(state, action).state }] : [];
      }
      const started = applyEnumeratedAction(state, action).state;
      return this.semanticMajorActionSources(started, action.mode).flatMap((source) => {
        const selected = applyEnumeratedAction(started, source).state;
        return getLegalActions(selected)
          .filter((target):target is Extract<GameAction, { type:"selectMajorActionTarget" }> => (
            target.type === "selectMajorActionTarget"
          ))
          .flatMap((target) => {
            const preview = applyEnumeratedAction(selected, target).state;
            const confirm = getLegalActions(preview).find((candidate):candidate is Extract<GameAction, { type:"confirmMajorAction" }> => (
              candidate.type === "confirmMajorAction"
            ));
            if (!confirm) return [];
            const semantic = action.mode === "gardener" ? "deploy_gardener"
              : action.mode === "warrior" ? "deploy_warrior"
                : action.mode === "recruit" ? "recruit_courtier" : "promote_courtier";
            const args:Record<string, unknown> = semantic === "recruit_courtier"
              ? {}
              : semantic === "promote_courtier"
                ? this.promotionSemanticArgs(target.target, target.levels)
                : { target:target.target };
            return [{
              token:{ action:semantic, args },
              state:applyEnumeratedAction(preview, confirm).state,
            }];
          });
      });
    });
  }

  private semanticTokenForEngineAction(
    state:GameState,
    engineAction:GameAction,
  ):SemanticRouteToken | null {
    if (engineAction.type === "chooseStartingPair") {
      return { action:"choose_starting_pair", args:{ offer:engineAction.offer } };
    }
    if (engineAction.type === "draftDie") {
      return { action:"take_die", args:{ color:engineAction.bridge, side:engineAction.end } };
    }
    if (engineAction.type === "placeDie") {
      const personal = /^p[0-9]+-domain-(courtier|gardener|warrior)$/.exec(engineAction.workspace);
      const castle = /^castle-(steward|diplomat)-([0-9]+)$/.exec(engineAction.workspace);
      return personal
        ? { action:"place_die_personal", args:{ row:personal[1] } }
        : castle
          ? { action:"place_die_castle", args:{ floor:castle[1], room:Number(castle[2]) } }
          : { action:"place_die_public", args:{ workspace:engineAction.workspace } };
    }
    if (engineAction.type === "exchangeSeal") {
      return { action:"exchange_seal", args:{ receive:engineAction.receive } };
    }
    if (engineAction.type === "chooseEffectOption" || engineAction.type === "refreshCastleRoom") {
      const kind = this.semanticChoiceKind(state, engineAction);
      if (!kind) return null;
      const choice = engineAction.type === "chooseEffectOption"
        ? engineAction.option
        : engineAction.room;
      return { action:"choose_reward", args:{ kind, choice } };
    }
    if (engineAction.type === "selectCastleTileAction") {
      const floor = engineAction.room.startsWith("diplomat-") ? "diplomat" : "steward";
      return {
        action:"choose_castle_row",
        args:{
          floor,
          room:Number(engineAction.room.split("-").at(-1)),
          row:castleRowNumber(engineAction.room, engineAction.rowId),
        },
      };
    }
    if (engineAction.type === "finishMajorAction") return { action:"finish_action", args:{} };
    if (engineAction.type === "finishResolution") return { action:"pass_turn", args:{} };
    return null;
  }

  private semanticPrefixProjection(
    seat:number,
    rawPrefix:ActionStep[],
  ):SemanticPrefixProjection {
    if (seat !== this.state.currentPlayer) throw new Error("SEMANTIC_PREFIX_WRONG_SEAT");
    if (!rawPrefix.length || canonicalFingerprint(rawPrefix[0]) !== canonicalFingerprint(BEGIN_STEP)) {
      throw new Error("INVALID_PREFIX_BEGIN");
    }
    const prefixSteps = rawPrefix.map((step) => clone(step));
    const rawActions = prefixSteps.slice(1).map(stepToAction);
    const authorityBase = structuredClone(this.state);
    let candidate = structuredClone(authorityBase);
    const semanticActions:SemanticRouteToken[] = [];
    let rawIndex = 0;
    while (rawIndex < rawActions.length) {
      const plans = this.semanticRoutePlans(candidate);
      const remaining = rawActions.length - rawIndex;
      const fullMatches = plans.filter((plan) => (
        plan.engineActions.length <= remaining
        && plan.engineActions.every((action, offset) => (
          canonicalFingerprint(action) === canonicalFingerprint(rawActions[rawIndex + offset])
        ))
      ));
      if (fullMatches.length > 1) {
        const tokens = new Set(fullMatches.map((plan) => canonicalFingerprint({
          action:plan.action,
          args:plan.args,
        })));
        if (tokens.size > 1) throw new Error("AMBIGUOUS_SEMANTIC_PREFIX");
      }
      const plan = fullMatches[0];
      if (!plan) {
        const partial = plans.some((candidatePlan) => (
          candidatePlan.engineActions.length > remaining
          && rawActions.slice(rawIndex).every((action, offset) => (
            canonicalFingerprint(action) === canonicalFingerprint(candidatePlan.engineActions[offset])
          ))
        ));
        if (partial) {
          return {
            status:"partial",
            authorityBase,
            state:candidate,
            semanticActions,
            prefixSteps,
          };
        }
        throw new Error(`SEMANTIC_PREFIX_ACTION_NOT_FOUND:${rawIndex + 1}`);
      }
      semanticActions.push({ action:plan.action, args:clone(plan.args) });
      if (plan.action === "pass_turn" && semanticActions.length > 1) {
        semanticActions.pop();
      }
      candidate = this.semanticPlanState(candidate, plan);
      rawIndex += plan.engineActions.length;
    }
    return {
      status:"complete",
      authorityBase,
      state:candidate,
      semanticActions,
      prefixSteps,
    };
  }

  private semanticRouteAutomatonForPrefix(
    seat:number,
    rawPrefix:ActionStep[],
  ):SemanticRouteAutomatonResult {
    const projection = this.semanticPrefixProjection(seat, rawPrefix);
    const cacheKey = stableSha256({
      decisionId:this.decisionId(),
      seat,
      prefixSteps:projection.prefixSteps,
      projection:"semantic-route-automaton-v1",
    });
    const cached = this.semanticRouteAutomatonCache.get(cacheKey);
    if (cached) return clone(cached);
    const enumerationId = `semantic:${cacheKey}`;
    if (projection.status === "partial") {
      const result:SemanticRouteAutomatonResult = {
        status:"semantic-route-automaton",
        schemaVersion:1,
        decisionId:this.decisionId(),
        enumerationId,
        coverageStatus:"bounded",
        enumerationComplete:false,
        code:"SEMANTIC_PREFIX_NOT_AT_BOUNDARY",
        startState:null,
        routeCount:null,
        prefixActionCount:projection.semanticActions.length,
        states:[],
        routeDiagnostics:{ boundedReason:"semantic_prefix_not_at_boundary" },
      };
      this.semanticRouteAutomatonCache.set(cacheKey, result);
      return clone(result);
    }

    const raw = createSemanticRouteAutomaton(
      structuredClone(projection.state),
      {
        stateKey:(state) => stableSha256(this.canonicalStateKey(state)),
        boundary:(state) => decisionBoundary(projection.authorityBase, state),
        accepting:(state, depth) => (
          projection.semanticActions.length + depth > 0
          && getLegalActions(state).some((action) => action.type === "finishResolution")
        ),
        successors:(state, depth) => {
          const successors = this.semanticRouteSuccessors(state);
          return projection.semanticActions.length + depth > 0
            ? successors.filter((successor) => successor.token.action !== "pass_turn")
            : successors;
        },
      },
      {
        maxNodes:explorerLimit("BGLAB_WHITE_SEMANTIC_AUTOMATON_MAX_NODES", 100_000),
        maxTimeMs:explorerLimit("BGLAB_WHITE_SEMANTIC_AUTOMATON_MAX_TIME_MS", 90_000),
      },
    );
    if (!raw.enumerationComplete || raw.startState === null) {
      const result:SemanticRouteAutomatonResult = {
        status:"semantic-route-automaton",
        schemaVersion:1,
        decisionId:this.decisionId(),
        enumerationId,
        coverageStatus:"bounded",
        enumerationComplete:false,
        code:"INCOMPLETE_OUTCOME_INDEX",
        startState:null,
        routeCount:null,
        prefixActionCount:projection.semanticActions.length,
        states:[],
        routeDiagnostics:clone(raw.diagnostics),
      };
      this.semanticRouteAutomatonCache.set(cacheKey, result);
      return clone(result);
    }

    const states = raw.states.map((state) => clone(state));
    let startState = raw.startState;
    for (const token of [...projection.semanticActions].reverse()) {
      const next = states.length;
      states.push({ final:false, transitions:[{ ...clone(token), to:startState }] });
      startState = next;
    }
    const result:SemanticRouteAutomatonResult = {
      status:"semantic-route-automaton",
      schemaVersion:1,
      decisionId:this.decisionId(),
      enumerationId,
      coverageStatus:"complete",
      enumerationComplete:true,
      startState,
      routeCount:raw.routeCount,
      prefixActionCount:projection.semanticActions.length,
      states,
      routeDiagnostics:{
        ...clone(raw.diagnostics),
        fixedSemanticPrefixLength:projection.semanticActions.length,
      },
    };
    this.semanticRouteAutomatonCache.set(cacheKey, result);
    return clone(result);
  }

  private hydrateSemanticRoute(
    seat:number,
    request:Record<string, unknown>,
  ):RouteEnumerationResult {
    if (seat !== this.state.currentPlayer) throw new Error("SEMANTIC_ROUTE_WRONG_SEAT");
    if (!Array.isArray(request.semanticActions)) throw new Error("SEMANTIC_ROUTE_ACTIONS_REQUIRED");
    const semanticActions = request.semanticActions.map((raw, index):SemanticRouteToken => {
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
        throw new Error(`INVALID_SEMANTIC_ROUTE_ACTION:${index}`);
      }
      const item = raw as Record<string, unknown>;
      if (typeof item.action !== "string" || !item.action || !item.args
        || typeof item.args !== "object" || Array.isArray(item.args)) {
        throw new Error(`INVALID_SEMANTIC_ROUTE_ACTION:${index}`);
      }
      return { action:item.action, args:clone(item.args as Record<string, unknown>) };
    });
    const requestedPrefix = Array.isArray(request.prefixSteps)
      ? request.prefixSteps as ActionStep[]
      : [BEGIN_STEP];
    const prefix = this.semanticPrefixProjection(seat, requestedPrefix);
    if (prefix.status !== "complete") throw new Error("SEMANTIC_PREFIX_NOT_AT_BOUNDARY");
    if (prefix.semanticActions.some((token, index) => (
      canonicalFingerprint(token) !== canonicalFingerprint(semanticActions[index])
    ))) {
      throw new Error("SEMANTIC_ROUTE_PREFIX_MISMATCH");
    }

    // The checked prefix already binds concrete member/source choices.
    // Reconstructing it from semantic tokens can lose that witness and make
    // equivalent-looking promotions ambiguous. Hydrate only the suffix.
    const authorityBase = prefix.authorityBase;
    let candidate = structuredClone(prefix.state);
    const engineActions:GameAction[] = prefix.prefixSteps.slice(1).map(stepToAction);
    for (let index = prefix.semanticActions.length; index < semanticActions.length; index += 1) {
      if (decisionBoundary(authorityBase, candidate)) {
        throw new Error(`SEMANTIC_ROUTE_CONTINUES_AFTER_BOUNDARY:${index}`);
      }
      const token = semanticActions[index];
      const matches = this.semanticRoutePlans(candidate).filter((plan) => (
        plan.action === token.action
        && canonicalFingerprint(plan.args) === canonicalFingerprint(token.args)
      ));
      if (matches.length !== 1) {
        throw new Error(matches.length === 0
          ? `SEMANTIC_ROUTE_ACTION_NOT_FOUND:${index}`
          : `AMBIGUOUS_SEMANTIC_ROUTE_ACTION:${index}`);
      }
      const plan = matches[0];
      engineActions.push(...plan.engineActions.map((action) => clone(action)));
      candidate = this.semanticPlanState(candidate, plan);
    }
    let boundaryReason = decisionBoundary(authorityBase, candidate);
    if (!boundaryReason) {
      const finish = getLegalActions(candidate).find((action) => action.type === "finishResolution");
      if (finish) {
        engineActions.push(clone(finish));
        candidate = applyEnumeratedAction(candidate, finish).state;
        boundaryReason = decisionBoundary(authorityBase, candidate);
      }
    }
    if (!boundaryReason) throw new Error("INCOMPLETE_SEMANTIC_ROUTE");
    const steps = [clone(BEGIN_STEP), ...engineActions.map(actionToStep)];
    const validation = this.validateTransaction(this.decisionId(), { steps });
    if (!validation.ok || !validation.complete || (validation.autoAdvancedSteps?.length ?? 0) > 0) {
      throw new Error("SEMANTIC_ROUTE_EXACT_REPLAY_FAILED");
    }
    const routeFingerprint = stableSha256({
      decisionId:this.decisionId(),
      semanticActions,
      steps,
      outcome:validation.outcome,
    });
    const program:TurnProgram = {
      programId:`semantic-program:${routeFingerprint}`,
      decisionId:this.decisionId(),
      rootId:`semantic-root:${stableSha256(semanticActions[0] ?? BEGIN_STEP)}`,
      boundaryReason,
      steps,
      causalTrace:clone(validation.causalTrace ?? []),
      outcome:clone(validation.outcome ?? {}),
      netOutcome:clone(validation.outcome ?? {}),
      factualCosts:clone(validation.factualCosts ?? {}),
      factualGains:clone(validation.factualGains ?? {}),
      immediateEffects:clone(validation.immediateEffects ?? []),
      immediateScoreDelta:validation.immediateScoreDelta ?? 0,
      endNowScoreDelta:validation.endNowScoreDelta ?? 0,
      scoringEngineChanges:clone(validation.scoringEngineChanges ?? []),
      complete:true,
      stateKeys:[],
    };
    return {
      status:"routes",
      decisionId:this.decisionId(),
      enumerationId:`semantic-hydrate:${routeFingerprint}`,
      coverageStatus:"complete",
      enumerationComplete:true,
      programs:[program],
      nextCursor:null,
      totalMatches:1,
      routeDiagnostics:{ exactReplay:true },
    };
  }

  private semanticCurrentActions(
    options:SemanticOptionProjection[],
  ):Array<{ action:string; availableNow:boolean }> {
    return [...new Set(options.map((option) => option.action))]
      .map((action) => ({ action, availableNow:true }));
  }

  outcomeIndex(seat = this.state.currentPlayer, request: Record<string, unknown> = {}): OutcomeIndex {
    return { ...this.authorityGateway.outcomeIndex(request), seat } as OutcomeIndex;
  }

  private prefixExplorerFor(
    seat:number,
    rawPrefix:ActionStep[],
  ):{
    explorer:ReturnType<typeof createDecisionExplorer>;
    prefixSteps:ActionStep[];
    prefixTrace:TurnProgram["causalTrace"];
  } {
    if (!rawPrefix.length || canonicalFingerprint(rawPrefix[0]) !== canonicalFingerprint(BEGIN_STEP)) {
      throw new Error("INVALID_PREFIX_BEGIN");
    }
    const prefixSteps = rawPrefix.map((step) => clone(step));
    const key = canonicalFingerprint({
      decisionId:this.decisionId(),
      seat,
      prefixSteps,
    });
    const cached = this.prefixExplorerCache.get(key);
    if (cached) return cached;
    const authorityBase = structuredClone(this.state);
    let candidate = structuredClone(authorityBase);
    const prefixTrace:TurnProgram["causalTrace"] = [];
    for (let index = 1; index < prefixSteps.length; index += 1) {
      const action = stepToAction(prefixSteps[index]);
      const before = candidate;
      const transition = applyAction(candidate, action);
      for (const event of this.causalTraceForAction(
        before,
        action,
        transition.events,
      )) {
        prefixTrace.push({
          ...clone(event),
          step:prefixTrace.length + 1,
          actionStep:index,
        });
      }
      candidate = transition.state;
    }
    const value = {
      // semantic-search returns only steps, choice discriminators and IDs.
      // Its complete prefix language can therefore use one bounded page;
      // selected full route details are fetched separately by programId.
      explorer:this.decisionExplorer(seat, candidate, authorityBase, 10_000, 10_000),
      prefixSteps,
      prefixTrace,
    };
    this.prefixExplorerCache.set(key, value);
    return value;
  }

  private enumerateFiniteRoutes(
    request: Record<string, unknown>,
    seat: number,
    explorer = this.decisionExplorer(seat),
  ): RouteEnumerationResult | SemanticRouteAutomatonResult {
    if (request.projection === "semantic-route-automaton") {
      const prefixSteps = Array.isArray(request.prefixSteps)
        ? request.prefixSteps as ActionStep[]
        : [BEGIN_STEP];
      return this.semanticRouteAutomatonForPrefix(seat, prefixSteps);
    }
    if (request.projection === "semantic-route-hydrate") {
      return this.hydrateSemanticRoute(seat, request);
    }
    const requestedPrefix = Array.isArray(request.prefixSteps)
      ? request.prefixSteps as ActionStep[]
      : null;
    if (requestedPrefix) {
      const prefix = this.prefixExplorerFor(seat, requestedPrefix);
      const suffixRequest = { ...request };
      delete suffixRequest.prefixSteps;
      const result = prefix.explorer.enumerateRoutes(suffixRequest) as {
        decisionId:string;
        enumerationId:string;
        coverageStatus:string;
        enumerationComplete:boolean;
        ordering?:"coverage-diverse-not-ranked";
        totalMatches:number;
        programs:Array<{
          programId:string;
          rootId:string;
          boundaryReason:string;
          termination?:string;
          outcome:Record<string, unknown>;
          factualCosts:Record<string, number>;
          factualGains:Record<string, number>;
          immediateEffects:string[];
          immediateScoreDelta:number;
          endNowScoreDelta:number;
          scoringEngineChanges:Array<Record<string, unknown>>;
          steps:Array<{ action:GameAction; causalEvents:TurnProgram["causalTrace"] }>;
          stateKeys:string[];
        }>;
        nextCursor:string | null;
      };
      const prefixActionCount = prefix.prefixSteps.length - 1;
      return {
        status:"routes",
        decisionId:result.decisionId,
        enumerationId:result.enumerationId,
        coverageStatus:result.coverageStatus,
        enumerationComplete:result.enumerationComplete,
        ordering:result.ordering,
        routeDiagnostics:prefix.explorer.diagnostics(),
        nextCursor:result.nextCursor,
        totalMatches:result.totalMatches,
        programs:result.programs.map((program) => {
          const suffixTrace = program.steps.flatMap((step, actionIndex) => (
            step.causalEvents.map((event) => ({
              ...clone(event),
              actionStep:prefixActionCount + actionIndex + 1,
            }))
          ));
          const causalTrace = [...prefix.prefixTrace, ...suffixTrace]
            .map((event, index) => ({ ...event, step:index + 1 }));
          const resourceFlow = this.factualResourceFlow(causalTrace);
          const immediateEffects = [
            ...new Set(causalTrace.map((event) => event.effect)),
          ].sort();
          const fullSteps = [
            ...prefix.prefixSteps,
            ...program.steps.map((step) => actionToStep(step.action)),
          ];
          const finishActionEndedChain = fullSteps.some(
            (step) => step.op === "finishMajorAction",
          );
          const outcome = {
            ...clone(program.outcome),
            factualCosts:clone(resourceFlow.factualCosts),
            factualGains:clone(resourceFlow.factualGains),
            immediateScoreDelta:program.immediateScoreDelta,
            endNowScoreDelta:program.endNowScoreDelta,
            ...(finishActionEndedChain
              ? {
                finishActionEndedChain:true,
                unexecutedActionAbandoned:true,
                ...(endedMajorActionFromTrace(causalTrace) ? {
                  endedMajorAction:endedMajorActionFromTrace(causalTrace),
                } : {}),
              }
              : {}),
          };
          if (request.projection === "semantic-search") {
            return {
              programId:program.programId,
              decisionId:result.decisionId,
              rootId:program.rootId,
              boundaryReason:program.boundaryReason,
              termination:program.termination,
              steps:fullSteps,
              // Semantic projection only needs the choice discriminator.
              // Dropping unrelated causal events prevents a large complete
              // prefix language from exhausting the JSONL worker transport.
              causalTrace:causalTrace.filter((event) => (
                typeof event.choiceType === "string" && event.choiceType.length > 0
              )),
              outcome:{},
              netOutcome:{},
              factualCosts:{},
              factualGains:{},
              immediateEffects:[],
              immediateScoreDelta:program.immediateScoreDelta,
              endNowScoreDelta:program.endNowScoreDelta,
              scoringEngineChanges:[],
              complete:true,
              stateKeys:[],
              semanticSearchMeta:{
                terminalStateKey:program.stateKeys.at(-1) ?? null,
                outcomeFingerprint:canonicalFingerprint(program.outcome),
              },
            };
          }
          return {
            programId:program.programId,
            decisionId:result.decisionId,
            rootId:program.rootId,
            boundaryReason:program.boundaryReason,
            termination:program.termination,
            steps:fullSteps,
            causalTrace,
            outcome,
            netOutcome:clone(outcome),
            factualCosts:clone(resourceFlow.factualCosts),
            factualGains:clone(resourceFlow.factualGains),
            immediateEffects:clone(immediateEffects),
            immediateScoreDelta:program.immediateScoreDelta,
            endNowScoreDelta:program.endNowScoreDelta,
            scoringEngineChanges:clone(program.scoringEngineChanges),
            complete:true,
            stateKeys:[...program.stateKeys],
          };
        }),
      };
    }
    const result = explorer.enumerateRoutes(request) as {
      decisionId: string; enumerationId: string; coverageStatus: string; enumerationComplete:boolean; ordering?: "coverage-diverse-not-ranked"; totalMatches: number; programs: Array<{ programId: string; rootId: string; boundaryReason: string; termination?: string; outcome: Record<string, unknown>; steps: Array<{ action: GameAction; causalEvents: TurnProgram["causalTrace"] }>; stateKeys: string[] }>; nextCursor: string | null;
    };
    return {
      status:"routes", decisionId:result.decisionId, enumerationId:result.enumerationId, coverageStatus:result.coverageStatus, enumerationComplete:result.enumerationComplete, ordering:result.ordering,
      routeDiagnostics:explorer.diagnostics(),
      nextCursor:result.nextCursor, totalMatches:result.totalMatches,
      programs:result.programs.map((program) => {
        const programFacts = program as unknown as TurnProgram;
        const fullSteps = [
          clone(BEGIN_STEP),
          ...program.steps.map((step) => actionToStep(step.action)),
        ];
        const causalTrace = program.steps.flatMap((step, actionIndex) => (
          step.causalEvents.map((event) => ({
            ...clone(event), actionStep:actionIndex + 1,
          }))
        )).map((event, index) => ({ ...event, step:index + 1 }));
        if (request.projection === "semantic-search") {
          return {
            programId:program.programId,
            decisionId:result.decisionId,
            rootId:program.rootId,
            boundaryReason:program.boundaryReason,
            termination:program.termination,
            steps:fullSteps,
            causalTrace:causalTrace.filter((event) => (
              typeof event.choiceType === "string" && event.choiceType.length > 0
            )),
            outcome:{},
            netOutcome:{},
            factualCosts:{},
            factualGains:{},
            immediateEffects:[],
            immediateScoreDelta:programFacts.immediateScoreDelta,
            endNowScoreDelta:programFacts.endNowScoreDelta,
            scoringEngineChanges:[],
            complete:true as const,
            stateKeys:[],
            semanticSearchMeta:{
              terminalStateKey:program.stateKeys.at(-1) ?? null,
              outcomeFingerprint:canonicalFingerprint(program.outcome),
            },
          };
        }
        const finishActionEndedChain = program.steps.some(
          (step) => step.action.type === "finishMajorAction",
        );
        const endedMajorAction = endedMajorActionFromTrace(
          causalTrace,
        );
        const outcome = {
          ...clone(program.outcome),
          factualCosts:clone(programFacts.factualCosts),
          factualGains:clone(programFacts.factualGains),
          immediateScoreDelta:programFacts.immediateScoreDelta,
          endNowScoreDelta:programFacts.endNowScoreDelta,
          ...(finishActionEndedChain
            ? {
              finishActionEndedChain:true,
              unexecutedActionAbandoned:true,
              ...(endedMajorAction ? { endedMajorAction } : {}),
            }
            : {}),
        };
        return {
          programId:program.programId, decisionId:result.decisionId, rootId:program.rootId,
          boundaryReason:program.boundaryReason, termination:program.termination,
          steps:fullSteps,
          causalTrace,
          outcome, netOutcome:clone(outcome),
          factualCosts:clone(programFacts.factualCosts),
          factualGains:clone(programFacts.factualGains),
          immediateEffects:clone(programFacts.immediateEffects),
          immediateScoreDelta:programFacts.immediateScoreDelta,
          endNowScoreDelta:programFacts.endNowScoreDelta,
          scoringEngineChanges:clone(programFacts.scoringEngineChanges),
          complete:true,
          stateKeys:[...program.stateKeys],
        };
      }),
    };
  }

  enumerateRoutes(
    request:Record<string, unknown> & { projection:"semantic-route-automaton" },
  ):SemanticRouteAutomatonResult;
  enumerateRoutes(request?:Record<string, unknown>):RouteEnumerationResult;
  enumerateRoutes(
    request:Record<string, unknown> = {},
  ):RouteEnumerationResult | SemanticRouteAutomatonResult {
    return this.authorityGateway.enumerateRoutes(request) as unknown as (
      RouteEnumerationResult | SemanticRouteAutomatonResult
    );
  }

  view(seat = this.state.currentPlayer): AdapterGuidance {
    const legal = getLegalActions(this.state);
    const modelEntryPhase = ["setup", "draft", "roundEnd"].includes(this.state.phase);
    const decisionFacts = seat === this.state.currentPlayer && modelEntryPhase
      ? this.decisionFacts(seat, legal)
      : undefined;
    return {
      decisionId: this.decisionId(),
      turnGroupId:`turn:${this.state.turn}:seat:${this.state.currentPlayer}`,
      seat,
      phase: this.state.phase,
      round: this.state.round,
      turn: this.state.turn,
      currentPlayer: this.state.currentPlayer,
      actionFamilies: [...new Set(legal.map((action) => action.type))],
      nextActions: clone(legal),
      publicState: {
        phase: this.state.phase,
        currentPlayer: this.state.currentPlayer,
        round: this.state.round,
        turn: this.state.turn,
        bridges: clone(this.state.bridges),
        workspaces: clone(this.state.workspaces),
        gardens: clone(this.state.gardens),
        trainingYards: clone(this.state.trainingYards),
        castleRooms: clone(this.state.board.castleRooms),
        startingOffers: this.state.startingOffers.map((offer, index) => ({
          offer: index,
          claimedBy: offer.claimedBy,
          resourceCard: clone(startingResourceCard(offer.resourceCard)),
          actionCard: clone(startingActionCard(offer.actionCard)),
        })),
        pendingEffects: clone(this.state.pendingEffects),
        usedDomainRowsThisTurn: clone(this.state.usedDomainRowsThisTurn),
        players: this.state.players.map((player) => ({
          id: player.id,
          name: player.name,
          influence: player.influence,
          points: player.points,
          resources: clone(player.resources),
          members: clone(player.members),
          lanternCards: clone(player.lanternCards),
          lanternEffects: clone(player.lanternEffects),
          domainCard: clone(player.domainCard),
        })),
      },
      ...(decisionFacts ? {
        privateState:{ decisionFacts },
        decisionSurface:this.decisionSurface(seat, decisionFacts),
      } : {}),
      turnOutcomeSummary: { decisionId:this.decisionId(), enumerationComplete:false, coverageStatus:"not_explored" },
      constraints: [
        "steps[0] must be {op:'begin', action:'turn'}.",
        "Each later step maps op to one engine GameAction type; preserve the listed fields exactly.",
        "Submit every choice through finishResolution so the whole decision advances atomically.",
        "If a step fails, no frontend state changes; keep the validated prefix and resubmit the complete corrected chain.",
      ],
    };
  }

  authoritySnapshot(): AuthoritySnapshot {
    return {
      schemaVersion: 1,
      decisionId: this.decisionId(),
      wrapper: {
        turn: this.state.turn,
        currentPlayer: this.state.currentPlayer,
        phase: this.state.phase,
        playerCount: this.state.playerCount,
      },
      game: clone(this.state),
      committed: clone(this.committed),
    };
  }

  snapshot(): AdapterSnapshot {
    const adapterView = this.view(this.state.currentPlayer);
    adapterView.turnOutcomeSummary = { decisionId:this.decisionId(), enumerationComplete:false, code:"INCOMPLETE_OUTCOME_INDEX" };
    return {
      ...this.authoritySnapshot(),
      adapterView,
    };
  }

  finalResult() {
    if (this.state.phase !== "finished") return null;
    const scores = scoreGame(this.state);
    const bestTotal = Math.max(...scores.map((score) => score.total));
    const scoreLeaders = scores.filter((score) => score.total === bestTotal).map((score) => score.player);
    const winner = determineWinner(this.state, scores);
    return {
      schemaVersion:1,
      winner,
      winners:[winner],
      tieBreakers:[
        {
          id:"total-score",
          label:"Total score",
          values:this.state.players.map((player) => scores.find((score) => score.player === player.id)!.total),
          winner:scoreLeaders.length === 1 ? scoreLeaders[0] : null,
        },
        {
          id:"turn-order",
          label:"Turn order",
          values:this.state.players.map((player) => this.state.turnOrder.indexOf(player.id)),
          winner:scoreLeaders.length > 1 ? winner : null,
        },
      ],
      players:this.state.players.map((player) => {
        const score = scores.find((candidate) => candidate.player === player.id)!;
        return {
          seat:player.id,
          total:score.total,
          components:[
            {id:"during-game", label:"During game", value:score.duringGame, formula:"authoritative points scored during play"},
            {id:"resources", label:"Resources", value:score.resources, formula:"official coin, seal, food, iron, and pearl conversion"},
            {id:"time-track", label:"Passage of time", value:score.timeTrack, formula:"official influence-track threshold score"},
            {id:"courtiers", label:"Courtiers", value:score.courtiers, formula:"sum of final courtier-location values"},
            {id:"warriors", label:"Warriors", value:score.warriors, formula:"yard warriors × yard value × courtiers inside the castle"},
            {id:"gardeners", label:"Gardeners", value:score.gardeners, formula:"sum of occupied garden printed points"},
          ],
        };
      }),
    };
  }

  restore(snapshot: AdapterSnapshot | GameState): AdapterGuidance;
  restore(snapshot: AdapterSnapshot | GameState, options: { returnView: false }): AuthoritySnapshot;
  restore(snapshot: AdapterSnapshot | GameState, options: { returnView?: true }): AdapterGuidance;
  restore(snapshot: AdapterSnapshot | GameState, options: { returnView?: boolean } = {}): AdapterGuidance | AuthoritySnapshot {
    if ("schemaVersion" in snapshot && "game" in snapshot) {
      if (snapshot.schemaVersion !== 1) throw new Error("Unsupported White Castle adapter snapshot version.");
      this.state = deserializeGame(serializeGame(snapshot.game));
      this.committed = clone(snapshot.committed ?? {});
    } else {
      this.state = deserializeGame(serializeGame(snapshot as GameState));
      this.committed = {};
    }
    this.clearOutcomeIndex();
    this.publish([]);
    if (options.returnView === false) return this.authoritySnapshot();
    return this.view(this.state.currentPlayer);
  }

  subscribe(listener: (snapshot: AdapterSnapshot, events: GameEvent[]) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  dispatch(decisionId: string | { decisionId?: string; action?: ActionTransaction; steps?: ActionStep[] }, transaction?: ActionTransaction): DispatchResult {
    if (typeof decisionId === "object") {
      transaction = decisionId.action ?? { steps: decisionId.steps ?? [] };
      decisionId = decisionId.decisionId ?? this.decisionId();
    }
    const requestedId = decisionId || this.decisionId();
    const requested = transaction ?? { steps: [] };
    const requestFingerprint = canonicalFingerprint(requested);
    const prior = this.committed[requestedId];
    if (prior) {
      if (prior.fingerprint === requestFingerprint) return { ...clone(prior.result), duplicate: true };
      return this.reject(requestedId, 0, [], "DECISION_ALREADY_COMMITTED", "该 decisionId 已提交过不同事务。", "刷新局面并使用新的 decisionId；不要重放旧回合。" );
    }
    if (requestedId !== this.decisionId()) {
      return this.reject(requestedId, 0, [], "STALE_DECISION", `当前 decisionId 是 ${this.decisionId()}。`, "刷新局面后为当前决策重新构造完整行动链。" );
    }
    if (!Array.isArray(requested.steps) || requested.steps.length < 1) {
      return this.reject(requestedId, 0, [], "INVALID_TRANSACTION", "完整行动链至少包含 begin 和一个游戏步骤。", "以 begin 开头，并提交能结束当前决策的全部步骤。" );
    }
    if (canonicalFingerprint(requested.steps[0]) !== canonicalFingerprint(BEGIN_STEP)) {
      return this.reject(requestedId, 0, [], "INVALID_BEGIN", "第一步必须是 {op:'begin', action:'turn'}。", "修正 begin 后重新提交完整 steps。" );
    }

    const before = this.state;
    let candidate = structuredClone(before);
    const events: GameEvent[] = [];
    const validatedPrefix: ActionStep[] = [clone(requested.steps[0])];
    for (let index = 1; index < requested.steps.length; index += 1) {
      try {
        const step = requested.steps[index];
        if (!step || typeof step !== "object" || typeof step.op !== "string" || step.op === "begin") {
          throw new Error("Invalid game step: every step after begin needs one GameAction op.");
        }
        const transition = applyAction(candidate, stepToAction(step));
        candidate = transition.state;
        events.push(...transition.events);
        validatedPrefix.push(clone(step));
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        return this.reject(requestedId, index, validatedPrefix, "ILLEGAL_STEP", message, correctionFor(error), candidate);
      }
    }
    if (!decisionComplete(before, candidate)) {
      return this.reject(
        requestedId,
        requested.steps.length,
        validatedPrefix,
        "INCOMPLETE_TURN",
        "行动链在当前玩家决策结束前停止。",
        "从已验证前缀继续模拟，补齐所有选择并以 finishResolution 结束，然后重新提交完整 steps。",
        candidate,
      );
    }

    this.state = candidate;
    this.clearOutcomeIndex();
    const result: DispatchResult = {
      ok: true,
      decisionId: requestedId,
      boundaryReason: decisionBoundary(before, candidate) ?? undefined,
      action: clone(requested),
      events: clone(events),
    };
    this.committed = {
      ...this.committed,
      [requestedId]: { fingerprint: requestFingerprint, result: clone(result) },
    };
    this.publish(events);
    return result;
  }

  validateTransaction(decisionId: string, transaction: ActionTransaction): TransactionValidationResult {
    const requestedId = decisionId || this.decisionId();
    const requested = transaction ?? { steps: [] };
    if (requestedId !== this.decisionId()) {
      return {
        ...validationFrontier(this.reject(requestedId, 0, [], "STALE_DECISION", `当前 decisionId 是 ${this.decisionId()}。`, "刷新局面后重新预览当前决策。")),
        complete: false,
        stateChanged: false,
      };
    }
    if (!Array.isArray(requested.steps) || requested.steps.length < 1) {
      return {
        ...validationFrontier(this.reject(requestedId, 0, [], "INVALID_TRANSACTION", "行动链至少包含 begin。", "以 begin 开头后重新预览。")),
        complete: false,
        stateChanged: false,
      };
    }
    if (canonicalFingerprint(requested.steps[0]) !== canonicalFingerprint(BEGIN_STEP)) {
      return {
        ...validationFrontier(this.reject(requestedId, 0, [], "INVALID_BEGIN", "第一步必须是 {op:'begin', action:'turn'}。", "修正 begin 后重新预览。")),
        complete: false,
        stateChanged: false,
      };
    }

    const before = this.state;
    let candidate = structuredClone(before);
    const events: GameEvent[] = [];
    const causalEvents: TurnProgram["causalTrace"] = [];
    const appendCausalEvents = (
      trace: TurnProgram["causalTrace"],
      actionStep: number,
    ) => {
      for (const event of trace) {
        causalEvents.push({
          ...clone(event),
          step:causalEvents.length + 1,
          actionStep,
        });
      }
    };
    const validatedPrefix: ActionStep[] = [clone(requested.steps[0])];
    for (let index = 1; index < requested.steps.length; index += 1) {
      try {
        const step = requested.steps[index];
        if (!step || typeof step !== "object" || typeof step.op !== "string" || step.op === "begin") {
          throw new Error("Invalid game step: every step after begin needs one GameAction op.");
        }
        const action = stepToAction(step);
        const transition = applyAction(candidate, action);
        appendCausalEvents(
          this.causalTraceForAction(candidate, action, transition.events),
          index,
        );
        candidate = transition.state;
        events.push(...transition.events);
        validatedPrefix.push(clone(step));
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        return {
          ...validationFrontier(this.reject(requestedId, index, validatedPrefix, "ILLEGAL_STEP", message, correctionFor(error), candidate)),
          complete: false,
          stateChanged: false,
          events: clone(events),
          causalTrace: clone(causalEvents),
        };
      }
    }

    const autoAdvancedSteps: ActionStep[] = [];
    const seen = new Set<string>();
    while (!decisionComplete(before, candidate)) {
      const legal = getLegalActions(candidate);
      const action = deterministicRouteContinuation(candidate, legal);
      if (!action) break;
      const stateKey = canonicalFingerprint(candidate);
      if (seen.has(stateKey) || autoAdvancedSteps.length >= 64) break;
      seen.add(stateKey);
      const actionStep = validatedPrefix.length;
      const transition = applyAction(candidate, action);
      appendCausalEvents(
        this.causalTraceForAction(candidate, action, transition.events),
        actionStep,
      );
      candidate = transition.state;
      events.push(...transition.events);
      const step = actionToStep(action);
      validatedPrefix.push(step);
      autoAdvancedSteps.push(step);
    }
    const finalCausalTrace = causalEvents;
    const scoring = this.programScoringFacts(before.currentPlayer, before, candidate, finalCausalTrace);
    const complete = decisionComplete(before, candidate);
    const nextActions = complete ? [] : getLegalActions(candidate);
    const finishActionEndedChain = validatedPrefix.some(
      (step) => step.op === "finishMajorAction",
    );
    const explicitFinishAction = requested.steps.some(
      (step) => step.op === "finishMajorAction",
    );
    const endedMajorAction = endedMajorActionFromTrace(finalCausalTrace);
    const outcome = {
      ...this.outcomeFor(before, candidate),
      factualCosts:clone(scoring.factualCosts),
      factualGains:clone(scoring.factualGains),
      immediateScoreDelta:scoring.immediateScoreDelta,
      endNowScoreDelta:scoring.endNowScoreDelta,
      ...(finishActionEndedChain
        ? {
          finishActionEndedChain:true,
          ...(endedMajorAction ? { endedMajorAction } : {}),
          ...(explicitFinishAction ? { unexecutedActionAbandoned:true } : {}),
        }
        : {}),
    };
    return {
      ok: true,
      decisionId: requestedId,
      action: clone(requested),
      events: clone(events),
      validatedPrefix,
      nextActions: nextActions.map((action) => actionToValidationStep(candidate, action)),
      continuationChoices: continuationChoices(candidate, nextActions),
      complete,
      stateChanged: false,
      autoAdvancedSteps,
      causalTrace: finalCausalTrace,
      outcome,
      ...scoring,
    };
  }

  private publish(events: GameEvent[]) {
    if (this.listeners.size === 0) return;
    const snapshot = this.snapshot();
    for (const listener of this.listeners) listener(snapshot, clone(events));
  }

  private reject(decisionId: string, failedStep: number, validatedPrefix: ActionStep[], code: string, message: string, correction: string, state = this.state): DispatchResult {
    const nextActions = getLegalActions(state);
    const pending = state.pendingEffects[0];
    const declineActions = pending?.effect.type === "pay" && pending.effect.optional
      ? nextActions.filter((action) => action.type === "chooseEffectOption" && action.effectId === pending.id && action.option === 1)
      : [];
    return {
      ok: false,
      decisionId,
      failedStep,
      validatedPrefix: clone(validatedPrefix),
      code,
      message,
      correction,
      nextActions: clone(nextActions),
      continuationChoices: continuationChoices(state, nextActions),
      ...(code === "INCOMPLETE_TURN" ? {continuationStatus:declineActions.length ? "may_stop" : "must_continue"} : {}),
      ...(declineActions.length ? {declineActions:clone(declineActions)} : {}),
    };
  }
}

declare global {
  interface Window {
    BGLabGameAdapter?: WhiteCastleBGLabAdapter;
  }
}

if (typeof window !== "undefined") window.BGLabGameAdapter = new WhiteCastleBGLabAdapter();
