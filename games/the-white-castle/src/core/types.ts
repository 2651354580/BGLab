export type PlayerCount = 2 | 3 | 4;
export type DieColor = "black" | "white" | "coral";
export type CastleActionSelector = DieColor | "any" | "light";
export type BridgeEnd = "left" | "right";
export type Phase = "setup" | "draft" | "place" | "resolve" | "roundEnd" | "finished";
export type Resource = "coins" | "seals" | "food" | "iron" | "pearl";
export type CappedResource = Exclude<Resource, "coins">;
export type FamilyMemberType = "courtier" | "gardener" | "warrior";
export type MajorAction = FamilyMemberType;
export type MajorActionMode = "recruit" | "promote" | "gardener" | "warrior";
export type ActionFlowStage = "chooseSource" | "chooseTarget" | "preview";
export type WorkspaceKind = "well" | "outside" | "domain" | "castle";
export type WorkspaceId = string;

export interface Die { id: string; color: DieColor; value: number; }

export interface Resources {
  coins: number;
  seals: number;
  food: number;
  iron: number;
  pearl: number;
}

export interface FamilyMember {
  id: string;
  type: FamilyMemberType;
  location: string;
}

export interface PlayerState {
  id: number;
  name: string;
  resources: Resources;
  influence: number;
  points: number;
  actionCards: Record<MajorAction, string>;
  domainCard: { kind: "starting" | "steward" | "diplomat"; id: number };
  lanternCards: string[];
  lanternEffects: Effect[];
  members: FamilyMember[];
}

export interface EffectGroup { id: string; effects: Effect[]; }

export type Effect =
  | { type: "gain"; resource: Resource; amount: number }
  | { type: "gainChoice"; resources: CappedResource[]; amount: number }
  | { type: "influence"; amount: number }
  | { type: "gainPoints"; amount: number }
  | { type: "lantern" }
  | { type: "wellAction" }
  | { type: "domainAction" }
  | { type: "castleTileAction"; color: CastleActionSelector }
  | { type: "gardenActivation"; gardens: string[] }
  | { type: "effectOrder"; effects: Effect[] }
  | { type: "actionOrder"; groups: EffectGroup[] }
  | { type: "daimyoReward"; positions: number[] }
  | { type: "pay"; resource: Resource; amount: number; effects: Effect[]; optional: boolean }
  | { type: "majorAction"; action: MajorAction }
  | { type: "castleRefresh"; room: string }
  | { type: "chooseOne"; options: Effect[][]; labels: string[] };

export interface ActionFlow {
  id: string;
  kind: MajorAction;
  mode: MajorActionMode;
  stage: ActionFlowStage;
  selectedSource?: string;
  selectedTarget?: string;
  levels?: 1 | 2;
  costs: Partial<Record<Resource, number>>;
  reversible: boolean;
  lockedReason?: string;
}

export interface PendingEffect {
  id: string;
  source: string;
  effect: Effect;
  completedSubactions?: ("recruit" | "promote")[];
  owner?: number;
}

export interface Workspace {
  id: WorkspaceId;
  kind: WorkspaceKind;
  label: string;
  printedValue: number;
  allowedColors: DieColor[];
  capacity: number | "unlimited";
  active: boolean;
  owner?: number;
  dice: Die[];
  effects: Effect[];
  effectsByColor?: Partial<Record<DieColor, Effect[]>>;
  effectGroupsByColor?: Partial<Record<DieColor, EffectGroup[]>>;
}

export interface GardenSpace {
  id: string;
  bridge: DieColor;
  foodCost: number;
  points: number;
  effects: Effect[];
  gardeners: number[];
}

export interface TrainingYard {
  id: string;
  ironCost: number;
  warriorValue: number;
  capacity: number | "unlimited";
  effects: Effect[];
  warriors: number[];
  tileIds: number[];
  tileFaces: ("blue" | "gold")[];
}

export interface DraftedDie {
  die: Die;
  fromBridge: DieColor;
  fromEnd: BridgeEnd;
  lanternTriggered: boolean;
}

export interface SetupRecord {
  seed: number;
  initialBridgeValues: Record<DieColor, number[]>;
  startingSetIds: string[];
  turnOrder: number[];
  stewardDeck: number[];
  diplomatDeck: number[];
  daimyoCard: number;
  yardTiles: number[];
  gardenCards: number[];
}

export interface DieTileState { color: DieColor; reward: Effect; }
export interface CastleActionSlot extends DieTileState { rowId: string; effects: Effect[]; }
export interface CastleRoomState {
  id: string;
  floor: "steward" | "diplomat";
  cardId: number;
  slots: CastleActionSlot[];
}
export interface BoardState {
  castleRooms: CastleRoomState[];
  stewardDeck: number[];
  diplomatDeck: number[];
  daimyoCard: number;
  daimyoTaken: (number | null)[];
  wellTiles: DieTileState[];
  yardTileIds: number[];
  gardenCardIds: number[];
}

export interface ScoreBreakdown {
  player: number;
  duringGame: number;
  resources: number;
  timeTrack: number;
  courtiers: number;
  warriors: number;
  gardeners: number;
  total: number;
}

export interface GameState {
  version: 1;
  playerCount: PlayerCount;
  setup: SetupRecord;
  round: number;
  turn: number;
  turnsThisRound: number;
  currentPlayer: number;
  turnOrder: number[];
  /** Influence-track markers from bottom to top; the top marker wins ties. */
  influenceStackOrder: number[];
  phase: Phase;
  bridges: Record<DieColor, Die[]>;
  board: BoardState;
  workspaces: Record<WorkspaceId, Workspace>;
  gardens: GardenSpace[];
  trainingYards: TrainingYard[];
  players: PlayerState[];
  pendingEffects: PendingEffect[];
  actionFlow?: ActionFlow;
  usedDomainRowsThisTurn: MajorAction[];
  draftedDie?: DraftedDie;
  scores?: ScoreBreakdown[];
  winner?: number;
  actionHistory: GameAction[];
  startingOffers: { resourceCard: number; actionCard: number; claimedBy: number | null }[];
  setupOrder: number[];
  setupIndex: number;
}

export type GameAction =
  | { type: "chooseStartingPair"; offer: number }
  | { type: "draftDie"; bridge: DieColor; end: BridgeEnd }
  | { type: "placeDie"; workspace: WorkspaceId }
  | { type: "chooseEffectOption"; effectId: string; option: number }
  | { type: "selectCastleTileAction"; effectId: string; room: string; rowId: string }
  | { type: "exchangeSeal"; receive: "coins" | CappedResource }
  | { type: "beginMajorAction"; mode: MajorActionMode }
  | { type: "selectMajorActionSource"; member: string }
  | { type: "selectMajorActionTarget"; target: string; levels?: 1 | 2 }
  | { type: "cancelMajorActionSelection" }
  | { type: "confirmMajorAction" }
  | { type: "refreshCastleRoom"; room: string }
  | { type: "finishMajorAction" }
  | { type: "finishResolution" };

/** A human UI may submit these steps one by one; an AI adapter may submit the same validated steps as one plan. */
export interface ActionPlan { actions: GameAction[]; }

export type GameEvent =
  | { type: "DieDrafted"; player: number; die: Die; bridge: DieColor; end: BridgeEnd; lanternTriggered: boolean }
  | { type: "DiePlaced"; player: number; die: Die; workspace: WorkspaceId }
  | { type: "ResourceChanged"; player: number; resource: Resource; amount: number; total: number }
  | { type: "InfluenceChanged"; player: number; amount: number; total: number }
  | { type: "MemberMoved"; player: number; member: string; from: string; to: string }
  | { type: "CardMoved"; player: number; card: string; from: string; to: string }
  | { type: "ChoiceRequired"; player: number; effect: PendingEffect }
  | { type: "EffectResolved"; player: number; source: string; effect: Effect }
  | { type: "TurnStarted"; player: number; turn: number }
  | { type: "RoundEnded"; round: number }
  | { type: "RoundStarted"; round: number; firstPlayer: number }
  | { type: "GameScored"; scores: ScoreBreakdown[]; winner: number };

export interface Transition { state: GameState; events: GameEvent[]; }

export interface CreateGameOptions {
  seed?: number;
  playerCount?: PlayerCount;
  playerNames?: string[];
  manualSetup?: boolean;
}
