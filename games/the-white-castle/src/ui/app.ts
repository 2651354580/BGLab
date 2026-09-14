import "./style.css";
import {
  placementCoinDelta, placementReferenceValue, applyAction, castleCardDomainRowGroups, castleCardRowData, createGame, deserializeGame, gardenCardIdsBySpace, getLegalActions, serializeGame,
  DIPLOMAT_CARDS, STEWARD_CARDS,
  type CappedResource, type DieColor, type Effect, type GameAction, type GameEvent, type GameState, type PlayerState, type PlayerCount,
} from "../core";
import { BOARD_CARDS, CARD_ATLASES, boardCardStyle, cardRowRects, lightCardRowIndex, type BoardCard, type CardAtlas } from "./card-layout";
import { castleRoomName, castleTileActionLabel } from "./board-action-labels";
import { bridgeDieActionIndex, bridgeDieRects, bridgeDieSlots } from "./bridge-dice-layout";
import { assignDaimyoClaims, daimyoClaimPiecePosition, daimyoRewardRects } from "./daimyo-layout";
import { materialAssetCard } from "./material-assets";
import { memberPlacement } from "./member-layout";
import { lanternLayout, lanternCardStyle, lanternShelfStyle, LANTERN_CARD_RATIO } from "./lantern-layout";
import {
  deterministicGardenActivation,
  deterministicMajorActionStart,
  daimyoRewardActionIndex,
  domainRewardActionIndex,
  gardenActivationActionIndex,
  isBoardNativeRewardAction,
  payActionLabel,
  influencePaymentLabel,
  lanternCollectionStep,
} from "./reward-interaction";
import { addResourcePick, isResourceChoiceComplete, type ResourceChoiceDraft } from "./resource-choice";
import type { ActionStep, AdapterSnapshot } from "../adapter/bglab-adapter";
import { ManualGameSession } from "./manual-session";
import { Bridge } from "../adapter/ws-bridge";
import { records as authoritativeHistory, type BglabActionRecord } from "../../../shared/ui/action-history.js";
import type { GameShellSlots, GameShellView } from "../../../shared/ui/game-shell.js";
import { buildWhiteCastleShellView, whiteCastleSkipAction } from "./game-shell-presenter";
import {
  DIE_TILES, PLAYER_STYLES, PLAYER_BOARD, PUBLIC_COMPONENTS,
  dieTileFaceStyle, dieTilePositionStyle, placementStyle, playerBoardStyle,
  playerPieceStyle, resourceMarkerStyle, sealMarkerStyle, startingCardStyle, yardTileSpriteCell, type DieTileColor, type DieTileReward, type PlayerSetup,
} from "./setup-layout";

const app = document.querySelector<HTMLDivElement>("#app")!;
if (!app) throw new Error("App root is missing.");

type SharedGameShell = {
  renderShell(view: GameShellView, slots?: GameShellSlots): string;
  bindActions(
    root: ParentNode,
    handlers: Partial<Record<"cancel" | "skip" | "confirm", () => void>>,
  ): void;
};

declare global {
  interface Window {
    BGLabGameShell?: SharedGameShell;
  }
}

const STORAGE_KEY = "the-white-castle-save-v2";
const COLORS: Record<DieColor, string> = { coral: "红", black: "黑", white: "白" };
const MEMBER_NAMES = { courtier: "家臣", gardener: "园丁", warrior: "武士" } as const;
const RESOURCE_NAMES = { coins: "钱币", seals: "家纹", food: "食物", iron: "铁", pearl: "珍珠母" } as const;
let history: BglabActionRecord[] = [];

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]!);
}

function newGame(playerCount: PlayerCount = 2): GameState {
  const seed = crypto.getRandomValues(new Uint32Array(1))[0];
  return createGame({ seed, playerCount, manualSetup: true, playerNames: PLAYER_STYLES.slice(0, playerCount).map((player) => player.name) });
}

function loadGame(): GameState {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      const loaded = deserializeGame(saved);
      if (loaded.startingOffers && loaded.board.gardenCardIds) return loaded;
    }
  } catch (error) { console.warn("Ignoring incompatible save", error); }
  return newGame();
}

async function loadHostedAdapter() {
  const { WhiteCastleBGLabAdapter } = await import("../adapter/bglab-adapter");
  // Import registers the shared browser adapter. Read it after import so the
  // frontend and websocket bridge cannot end up with different authorities.
  return window.BGLabGameAdapter ?? new WhiteCastleBGLabAdapter();
}

const adapter = window.BG_GAME_ID || window.BG_REPLAY_MODE ? await loadHostedAdapter() : null;
let manualSession = adapter ? null : new ManualGameSession(loadGame());
if (adapter) adapter.restore(newGame());
function authorityState(): GameState { return adapter ? adapter.snapshot().game : manualSession!.snapshot(); }
function authorityRevision(): string { return adapter ? adapter.decisionId() : manualSession!.revision(); }
function authorityHistory(): BglabActionRecord[] {
  return adapter ? hydrateAuthoritativeHistory(adapter.snapshot())
    : manualSession!.history().map((record) => ({ ...record, text: formatAuthoritativeHistory(record) }));
}
let state = authorityState();
let legalActions: GameAction[] = [];
let busy = false;
let announcement: string | null = null;
let resourceChoiceDraft: ResourceChoiceDraft | null = null;
let sealMenuOpen = false;

interface UndoEntry { snapshot: string; history: BglabActionRecord[]; actionCount: number }
let turnStartCheckpoint: UndoEntry | null = null;
let undoStack: UndoEntry[] = [];
let undoLockedReason: string | null = null;
let decisionStartId = authorityRevision();
let decisionStartState = structuredClone(state);
let draftActions: GameAction[] = [];
let setupDraftAction: Extract<GameAction, { type: "chooseStartingPair" }> | null = null;
let playerTypes: string[] = state.players.map(() => "human");
let manualTest = !adapter;
let selectedPlayerCount: PlayerCount = state.playerCount;
let aiRequestId: string | null = null;
let pausedReason: string | null = null;

function atlasStyle(atlasName: CardAtlas, card: number) {
  const atlas = CARD_ATLASES[atlasName];
  const index = card - 1;
  const column = index % atlas.columns;
  const row = Math.floor(index / atlas.columns);
  const x = atlas.columns === 1 ? 0 : (column / (atlas.columns - 1)) * 100;
  const y = atlas.rows === 1 ? 0 : (row / (atlas.rows - 1)) * 100;
  return `background-image:url('${atlas.file}');background-size:${atlas.columns * 100}% ${atlas.rows * 100}%;background-position:${x}% ${y}%`;
}

function dynamicBoardCards(): BoardCard[] {
  const cards = BOARD_CARDS.map((card) => ({ ...card }));
  const set = (id: string, card: number) => { const target = cards.find((item) => item.id === id); if (target) target.card = card; };
  const setMaterial = (id: string, materialId: string) => set(id, materialAssetCard(materialId));
  setMaterial("daimyo", `daimyo-card-${state.board.daimyoCard}`);
  for (const room of state.board.castleRooms) setMaterial(room.id, `${room.floor}-card-${room.cardId}`);
  const gardenCards = gardenCardIdsBySpace(state.board);
  for (const [gardenId, boardId] of Object.entries(GARDEN_CARD_BY_SPACE)) setMaterial(boardId, `garden-card-${gardenCards[gardenId]}`);
  return cards;
}

function rewardFromEffect(effect: Effect): DieTileReward {
  if (effect.type === "gainChoice") return "resource";
  if (effect.type === "gain" && effect.resource === "coins") return "coin";
  if (effect.type === "gain" && ["food", "iron", "pearl"].includes(effect.resource)) return effect.resource as DieTileReward;
  return "resource";
}

function renderDieTiles() {
  const [s1, s2, s3, d1, d2] = state.board.castleRooms;
  const slots = [s1.slots[0], s2.slots[0], s3.slots[0], d1.slots[0], d2.slots[0], s1.slots[1], s2.slots[1], s3.slots[1], d1.slots[1], d2.slots[1], s1.slots[2], s2.slots[2], s3.slots[2]];
  return DIE_TILES.map((template, index) => {
    const slot = slots[index];
    const tile = slot ?? state.board.wellTiles[index - 13];
    const color: DieTileColor = tile.color === "coral" ? "red" : tile.color;
    const reward = rewardFromEffect(tile.reward);
    const renderedTile = { ...template, color, reward };
    const face = index >= 13 ? "reward" : "color";
    return `<div class="die-tile" data-face="${face}" style="${dieTilePositionStyle(renderedTile)}"><div class="die-tile-sides"><div class="die-tile-face die-tile-face--color" style="${dieTileFaceStyle(renderedTile, "color")}"></div><div class="die-tile-face die-tile-face--reward" style="${dieTileFaceStyle(renderedTile, "reward")}"></div></div></div>`;
  }).join("");
}

function directActionAttributes(index: number, label: string) {
  return index < 0 || !canHumanAct() ? "" : `data-action-index="${index}" data-action-key="action-${index}" role="button" tabindex="0" aria-label="${label}"`;
}

function canHumanAct() {
  return !window.BG_REPLAY_MODE
    && !pausedReason
    && !busy
    && playerTypes[state.currentPlayer] === "human";
}

function renderBridgeDice() {
  const visualColor: Record<DieColor, string> = { coral: "red", black: "black", white: "white" };
  return (["coral", "black", "white"] as DieColor[]).flatMap((color) => {
    const reference = PUBLIC_COMPONENTS.filter((item) => item.kind === "die" && item.id.startsWith(`${visualColor[color]}-`));
    const count = state.playerCount + 1;
    const dice = state.bridges[color];
    const removedCount = Math.max(0, count - dice.length);
    const draftedEnds = state.actionHistory
      .filter((action): action is Extract<GameAction, { type: "draftDie" }> => action.type === "draftDie" && action.bridge === color)
      .slice(-removedCount)
      .map((action) => action.end);
    const initialIds = Array.from({ length: count }, (_, index) => index + 1).map((index) => `r${state.round}-${color}-${index}`);
    const slots = bridgeDieSlots(initialIds, dice.map((die) => die.id), draftedEnds);
    const rects = bridgeDieRects(slots, count, reference[0].y);
    return dice.map((die, index) => {
      const template = { ...reference[0], ...rects[index] };
      const row = color === "coral" ? 0 : color === "white" ? 1 : 2;
      const actionIndex = bridgeDieActionIndex(legalActions, color, index, dice.length);
      const label = actionIndex >= 0 ? actionLabel(legalActions[actionIndex]) : `${COLORS[color]}色 ${die.value} 点`;
      return `<div class="board-piece board-piece--die ${actionIndex >= 0 ? "is-selectable" : ""}" data-die-id="${die.id}" ${directActionAttributes(actionIndex, label)} style="${placementStyle({ ...template, file: "/twc-dice.png", columns: 6, rows: 3, column: die.value - 1, row })}" aria-label="${label}"></div>`;
    });
  }).join("");
}

function renderPublicComponents() {
  const fixed = PUBLIC_COMPONENTS.filter((item) => !["die", "yard-tile", "round-marker", "heron", "influence"].includes(item.kind));
  const yardTemplates = PUBLIC_COMPONENTS.filter((item) => item.kind === "yard-tile");
  const placedYardTiles = state.trainingYards.flatMap((yard) => yard.tileIds.map((id, index) => ({ id, face: yard.tileFaces?.[index] ?? "blue" })));
  const yards = yardTemplates.map((template, index) => {
    const tileId = state.board.yardTileIds[index];
    const face = placedYardTiles.find((tile) => tile.id === tileId)?.face ?? "blue";
    const sprite = yardTileSpriteCell(tileId, face);
    return {
      ...template,
      id: `yard-tile-${tileId}`,
      label: `训练场板块 ${tileId}（${face === "gold" ? "金底" : "蓝底"}）`,
      ...sprite,
    };
  });
  const roundMarker = { id: `round-${state.round}`, file: "/twc-round-marker.png", x: 827, y: -1.5 + (state.round - 1) * 23, width: 23.50242, height: 35, label: `第 ${state.round} 轮标记`, kind: "round-marker", z: 10 };
  const heronSlots = [[104, -6.5], [71, 9.5], [38, 25.5], [5, 41.5]] as const;
  const herons = state.turnOrder.map((playerId, order) => {
    const config = PLAYER_STYLES[playerId];
    return { id: `heron-p${playerId}`, file: "/twc-herons.png", columns: 4, rows: 1, column: config.pieceColumn, row: 0, x: heronSlots[order][0], y: heronSlots[order][1], width: 39, height: 35, label: `${state.players[playerId].name}白鹭顺位 ${order + 1}`, kind: "heron", z: 10 + order };
  });
  const influenceOffsets = [-24, 16, 40, 64, 88, 112, 172, 196, 220, 244, 268, 330, 354, 378, 402, 460, 484, 508, 532, 556, 580];
  const influenceMarkers = state.players.map((player) => {
    const sameSpace = state.influenceStackOrder.filter((id) => state.players[id].influence === player.influence);
    const stack = sameSpace.indexOf(player.id);
    const config = PLAYER_STYLES[player.id];
    const position = Math.max(0, Math.min(influenceOffsets.length - 1, player.influence));
    return { id: `influence-p${player.id}`, file: "/twc-influence-markers.png", columns: 4, rows: 1, column: config.pieceColumn, row: 0, x: 170 + influenceOffsets[position], y: 41 - stack * 6.5, width: 25, height: 24.07407, label: `${player.name}影响力 ${player.influence}`, kind: "influence", z: 10 + stack };
  });
  return [...fixed, ...yards, roundMarker, ...herons, ...influenceMarkers].map((item) => {
    const candidateActionIndex = "actionIndex" in item ? item.actionIndex : undefined;
    const actionIndex = typeof candidateActionIndex === "number" ? candidateActionIndex : -1;
    const label = actionIndex >= 0 ? actionLabel(legalActions[actionIndex]) : item.label;
    return `<div class="board-piece board-piece--${item.kind} ${actionIndex >= 0 ? "is-selectable" : ""}" ${actionIndex >= 0 ? directActionAttributes(actionIndex, label) : ""} style="${placementStyle(item)}" aria-label="${escapeHtml(label)}"></div>`;
  }).join("") + renderBridgeDice();
}

interface BoardRect { x: number; y: number; width: number; height: number }

interface ActionHotspot {
  actionIndex: number;
  rect: BoardRect;
  label: string;
  kind: "destination" | "reward";
}

const BOARD_WORKSPACE_RECTS: Record<string, BoardRect> = {
  "castle-diplomat-1": { x: 346, y: 216, width: 42, height: 42 },
  "castle-diplomat-2": { x: 543, y: 216, width: 42, height: 42 },
  "castle-steward-1": { x: 308, y: 314, width: 42, height: 42 },
  "castle-steward-2": { x: 504, y: 314, width: 42, height: 42 },
  "castle-steward-3": { x: 702, y: 314, width: 42, height: 42 },
  well: { x: 357, y: 404, width: 95, height: 56 },
  "outside-left": { x: 729, y: 412, width: 42, height: 42 },
  "outside-right": { x: 808, y: 412, width: 42, height: 42 },
};

const TRAINING_YARD_RECTS: Record<string, BoardRect> = {
  "yard-1": { x: 656, y: 99, width: 112, height: 82 },
  "yard-2": { x: 779, y: 99, width: 112, height: 82 },
  "yard-3": { x: 751, y: 188, width: 112, height: 82 },
};

const CASTLE_DESTINATION_RECTS: Record<string, BoardRect> = {
  gate: { x: 460, y: 392, width: 226, height: 68 },
  "steward-1": { x: 303, y: 290, width: 182, height: 95 },
  "steward-2": { x: 499, y: 290, width: 182, height: 95 },
  "steward-3": { x: 697, y: 290, width: 182, height: 95 },
  "diplomat-1": { x: 341, y: 191, width: 182, height: 95 },
  "diplomat-2": { x: 537, y: 191, width: 182, height: 95 },
  daimyo: { x: 365, y: 93, width: 257, height: 95 },
};

const GARDEN_CARD_BY_SPACE: Record<string, string> = {
  "garden-coral-1": "garden-red-plant", "garden-coral-2": "garden-red-rock",
  "garden-black-1": "garden-black-plant", "garden-black-2": "garden-black-rock",
  "garden-white-1": "garden-white-plant", "garden-white-2": "garden-white-rock",
};

function boardRectStyle(rect: BoardRect) {
  return `left:${(rect.x / 900) * 100}%;top:${(rect.y / 460) * 100}%;width:${(rect.width / 900) * 100}%;height:${(rect.height / 460) * 100}%`;
}

function dieSpriteStyle(die: { color: DieColor; value: number }) {
  const row = die.color === "coral" ? 0 : die.color === "white" ? 1 : 2;
  return `background-image:url('/twc-dice.png');background-size:600% 300%;background-position:${(die.value - 1) * 20}% ${row * 50}%`;
}

function majorActionHotspots(cards: BoardCard[]): ActionHotspot[] {
  const candidates = legalActions.flatMap((action, actionIndex): ActionHotspot[] => {
    let rect: BoardRect | undefined;
    if (action.type === "selectMajorActionTarget") {
      if (action.target.startsWith("garden-")) rect = cards.find((card) => card.id === GARDEN_CARD_BY_SPACE[action.target]);
      else if (action.target.startsWith("yard-")) rect = TRAINING_YARD_RECTS[action.target];
      else rect = CASTLE_DESTINATION_RECTS[action.target];
    }
    return rect ? [{ actionIndex, rect, label: actionLabel(action), kind: "destination" }] : [];
  });
  const rendered = new Set<string>();
  return candidates.filter((hotspot) => {
    const key = JSON.stringify(hotspot.rect);
    if (rendered.has(key)) return false;
    rendered.add(key);
    return true;
  });
}

function castleActionHotspots(cards: BoardCard[]): ActionHotspot[] {
  return legalActions.flatMap((action, actionIndex): ActionHotspot[] => {
    if (action.type !== "selectCastleTileAction") return [];
    const room = state.board.castleRooms.find((candidate) => candidate.id === action.room);
    const visible = cards.find((card) => card.id === action.room);
    if (!room || !visible) return [];
    const material = (room.floor === "steward" ? STEWARD_CARDS : DIPLOMAT_CARDS)
      .find((card) => card.id === room.cardId);
    const rowIndex = room.slots.findIndex((slot) => slot.rowId === action.rowId);
    if (!material || rowIndex < 0) return [];
    const rect = cardRowRects(visible, castleCardDomainRowGroups(material).map((group) => group.length))[rowIndex];
    return rect ? [{ actionIndex, rect, label: actionLabel(action), kind: "reward" }] : [];
  });
}

function rewardActionHotspots(cards: BoardCard[]): ActionHotspot[] {
  const pending = state.pendingEffects[0];
  if (!pending || (pending.effect.type !== "actionOrder" && pending.effect.type !== "chooseOne")) return [];
  const roomId = pending.source.replace(/^castle-/, "");
  const room = state.board.castleRooms.find((candidate) => candidate.id === roomId);
  const visible = cards.find((card) => card.id === roomId);
  if (!room || !visible) return [];
  const material = (room.floor === "steward" ? STEWARD_CARDS : DIPLOMAT_CARDS).find((card) => card.id === room.cardId);
  if (!material) return [];
  const rows = castleCardRowData(material);
  const rowRects = cardRowRects(visible, castleCardDomainRowGroups(material).map((group) => group.length));
  const candidates = legalActions.flatMap((action, actionIndex): ActionHotspot[] => {
    if (action.type !== "chooseEffectOption" || action.effectId !== pending.id) return [];
    if (pending.effect.type === "chooseOne") {
      const rowIndex = lightCardRowIndex(rows.map((row) => row.tone), action.option);
      return rowIndex >= 0 ? [{ actionIndex, rect: rowRects[rowIndex], label: actionLabel(action), kind: "reward" }] : [];
    }
    if (pending.effect.type !== "actionOrder") return [];
    const selected = pending.effect.groups[action.option];
    return rows.flatMap((row, rowIndex) => row.id === selected.id
      ? [{ actionIndex, rect: rowRects[rowIndex], label: actionLabel(action), kind: "reward" }]
      : []);
  });
  const counts = new Map<string, Set<number>>();
  for (const hotspot of candidates) {
    const key = JSON.stringify(hotspot.rect);
    const actions = counts.get(key) ?? new Set<number>();
    actions.add(hotspot.actionIndex);
    counts.set(key, actions);
  }
  const rendered = new Set<string>();
  return candidates.filter((hotspot) => {
    const rectKey = JSON.stringify(hotspot.rect);
    const hotspotKey = `${hotspot.actionIndex}:${rectKey}`;
    if (counts.get(rectKey)?.size !== 1 || rendered.has(hotspotKey)) return false;
    rendered.add(hotspotKey);
    return true;
  });
}

function gardenActivationHotspots(cards: BoardCard[]): ActionHotspot[] {
  return state.gardens.flatMap((garden): ActionHotspot[] => {
    const actionIndex = gardenActivationActionIndex(state, legalActions, garden.id);
    if (actionIndex < 0) return [];
    const boardId = GARDEN_CARD_BY_SPACE[garden.id];
    const rect = cards.find((card) => card.id === boardId);
    return rect ? [{ actionIndex, rect, label: actionLabel(legalActions[actionIndex]), kind: "destination" }] : [];
  });
}

function daimyoRewardHotspots(cards: BoardCard[]): ActionHotspot[] {
  const pending = state.pendingEffects[0];
  if (!pending || pending.effect.type !== "daimyoReward") return [];
  const card = cards.find((candidate) => candidate.id === "daimyo");
  if (!card) return [];
  return daimyoRewardRects(card).flatMap((rect, position): ActionHotspot[] => {
    const actionIndex = daimyoRewardActionIndex(state, legalActions, position);
    return actionIndex >= 0 ? [{ actionIndex, rect, label: actionLabel(legalActions[actionIndex]), kind: "reward" }] : [];
  });
}

function renderActionHotspot(hotspot: ActionHotspot) {
  if (!canHumanAct()) return "";
  return `<button type="button" class="action-hotspot action-hotspot--${hotspot.kind}" data-action-index="${hotspot.actionIndex}" data-action-key="action-${hotspot.actionIndex}" style="${boardRectStyle(hotspot.rect)}" aria-label="${hotspot.label}"><span>${hotspot.label}</span></button>`;
}

function placedDieRect(workspaceId: string, rect: BoardRect, index: number, count: number): BoardRect {
  if (workspaceId !== "well") return { x: rect.x + 3.5 + index * 7, y: rect.y + 3.5 - index * 7, width: 35, height: 35 };
  const size = 22;
  const gap = 2;
  const columns = Math.min(4, count);
  const row = Math.floor(index / columns);
  const column = index % columns;
  const rows = Math.ceil(count / columns);
  const rowCount = row === rows - 1 ? count - row * columns : columns;
  const rowWidth = rowCount * size + Math.max(0, rowCount - 1) * gap;
  return {
    x: rect.x + (rect.width - rowWidth) / 2 + column * (size + gap),
    y: rect.y + (rect.height - (rows * size + Math.max(0, rows - 1) * gap)) / 2 + row * (size + gap),
    width: size,
    height: size,
  };
}

function renderBoardInteractionLayer(cards: BoardCard[]) {
  const targets = canHumanAct() ? legalActions.flatMap((action, index) => {
    if (action.type !== "placeDie" || !BOARD_WORKSPACE_RECTS[action.workspace]) return [];
    const label = actionLabel(action);
    return [`<button type="button" class="board-target" data-action-index="${index}" data-action-key="action-${index}" style="${boardRectStyle(BOARD_WORKSPACE_RECTS[action.workspace])}" aria-label="${label}"><span>${workspaceLabel(action.workspace)}</span></button>`];
  }) : [];
  const placed = Object.entries(BOARD_WORKSPACE_RECTS).flatMap(([workspaceId, rect]) => state.workspaces[workspaceId]?.dice.map((die, index, dice) => {
    return `<div class="placed-die" style="${boardRectStyle(placedDieRect(workspaceId, rect, index, dice.length))};${dieSpriteStyle(die)}" title="${index + 1 === dice.length ? "顶层" : "下层"}：${COLORS[die.color]}色 ${die.value} 点"></div>`;
  }) ?? []);
  const hotspots = [
    ...majorActionHotspots(cards),
    ...castleActionHotspots(cards),
    ...rewardActionHotspots(cards),
    ...gardenActivationHotspots(cards),
    ...daimyoRewardHotspots(cards),
  ].map(renderActionHotspot);
  return `<div class="board-interactions">${targets.join("")}${hotspots.join("")}${placed.join("")}</div>`;
}

function renderDeployedMembers(cards: BoardCard[]) {
  const daimyoCard = cards.find((card) => card.id === "daimyo");
  const claims = assignDaimyoClaims(state.players, state.board.daimyoTaken);
  const claimByMember = new Map(claims.map((claim) => [claim.memberId, claim]));
  const deployed = state.players.flatMap((player) => player.members
    .filter((member) => member.location !== "domain")
    .map((member) => ({ member, player })))
    .sort((left, right) => left.member.location.localeCompare(right.member.location) || left.player.id - right.player.id || left.member.id.localeCompare(right.member.id));
  const byLocation = new Map<string, typeof deployed>();
  for (const entry of deployed.filter(({ member }) => !claimByMember.has(member.id))) {
    byLocation.set(entry.member.location, [...(byLocation.get(entry.member.location) ?? []), entry]);
  }
  return deployed.map(({ member, player }) => {
    const claim = claimByMember.get(member.id);
    const occupants = byLocation.get(member.location) ?? [];
    const claimedPosition = claim && daimyoCard ? daimyoClaimPiecePosition(daimyoCard, claim.position) : undefined;
    const ownOccupants = occupants.filter((entry) => entry.player.id === player.id);
    const position = claimedPosition ? { ...claimedPosition, width: 25, height: 27, z: 20 } : memberPlacement(
      member.location, occupants.findIndex((entry) => entry.member.id === member.id), occupants.length,
      { seat: player.id, indexInSeat: ownOccupants.findIndex((entry) => entry.member.id === member.id) },
    );
    const config = PLAYER_STYLES[player.id];
    const file = member.type === "courtier" ? "/twc-courtiers.png" : member.type === "gardener" ? "/twc-gardeners.png" : "/twc-warriors.png";
    const sourceIndex = legalActions.findIndex((action) => action.type === "selectMajorActionSource" && action.member === member.id);
    const cancelIndex = state.actionFlow?.selectedSource === member.id ? legalActions.findIndex((action) => action.type === "cancelMajorActionSelection") : -1;
    const actionIndex = cancelIndex >= 0 ? cancelIndex : sourceIndex;
    const actionAttributes = actionIndex >= 0 ? directActionAttributes(actionIndex, actionLabel(legalActions[actionIndex])) : "";
    return `<div id="member-${member.id}" class="deployed-member ${actionIndex >= 0 ? "is-selectable" : ""} ${state.actionFlow?.selectedSource === member.id ? "is-selected" : ""}" ${actionAttributes} style="${placementStyle({ id: member.id, file, columns: 4, rows: 1, column: config.pieceColumn, row: 0, ...position, label: member.id, kind: "member" })}" title="${escapeHtml(player.name)}·${MEMBER_NAMES[member.type]}"></div>`;
  }).join("");
}

function renderStartingCard(kind: "resource" | "action", id: number, className: string) {
  const label = kind === "resource" ? "资源" : "行动";
  return `<span class="starting-card starting-card--${kind} ${className}" style="${startingCardStyle(kind, id)}" aria-label="初始${label}卡 ${id}"></span>`;
}

function renderDomainCard(player: PlayerState) {
  const card = player.domainCard;
  if (card.id === 0) return `<div class="domain-card domain-card--empty">等待选择初始卡</div>`;
  if (card.kind === "starting") return `<div class="domain-card domain-card--starting">${renderStartingCard("action", card.id, "domain-card__starting")}</div>`;
  return `<div class="domain-card" style="${atlasStyle(card.kind, card.id)}"></div>`;
}

function lanternBackStyle(card: string): string {
  let column = 2;
  if (card.startsWith("starting-resource-")) {
    const id = Number(card.split("-").at(-1));
    column = id <= 3 ? 3 : id <= 6 ? 4 : 5;
  } else if (card === "decree-coin") column = 6;
  else if (card === "decree-seal") column = 7;
  else if (card === "decree-points") column = 8;
  else if (card.startsWith("steward-")) column = 0;
  else if (card.startsWith("diplomat-")) column = 1;
  return `background-image:url('/twc-card-backs.png');background-size:900% 100%;background-position:${(column / 8) * 100}% 0`;
}

function lanternActionIndex() {
  const pending = state.pendingEffects[0]?.effect;
  if (!pending) return -1;
  return legalActions.findIndex((action) => {
    if (action.type !== "chooseEffectOption") return false;
    if (pending.type === "chooseOne") return pending.options[action.option]?.some((effect) => effect.type === "lantern");
    if (pending.type === "effectOrder") return pending.effects[action.option]?.type === "lantern";
    if (pending.type === "actionOrder") return pending.groups[action.option]?.effects.some((effect) => effect.type === "lantern");
    return false;
  });
}

function renderLanternCards(player: PlayerState) {
  if (!player.lanternCards.length) return `<div class="lantern-empty" style="${lanternShelfStyle(0)}">等待初始资源卡</div>`;
  const actionIndex = player.id === state.currentPlayer ? lanternActionIndex() : -1;
  const actionAttributes = actionIndex >= 0 ? directActionAttributes(actionIndex, "获得灯笼区全部奖励") : "";
  const cards = player.lanternCards.map((card, index) => `<div class="lantern-back-wrap" style="${lanternCardStyle(player.lanternCards.length, index)}" title="${card}（背面）"><div class="lantern-back" style="${lanternBackStyle(card)}"></div></div>`).join("");
  return `<div class="lantern-stack ${actionIndex >= 0 ? "is-selectable" : ""}" style="${lanternShelfStyle(player.lanternCards.length)};--card-ratio:${LANTERN_CARD_RATIO}" ${actionAttributes}>${cards}<b>${player.lanternCards.length}</b></div>`;
}

function renderPlayerPieces(player: PlayerState, config: PlayerSetup) {
  return player.members.filter((member) => member.location === "domain").map((member) => {
    const index = Number(member.id.split("-").at(-1)) - 1;
    const sourceIndex = legalActions.findIndex((action) => action.type === "selectMajorActionSource" && action.member === member.id);
    const cancelIndex = state.actionFlow?.selectedSource === member.id ? legalActions.findIndex((action) => action.type === "cancelMajorActionSelection") : -1;
    const actionIndex = cancelIndex >= 0 ? cancelIndex : sourceIndex;
    const actionAttributes = actionIndex >= 0 ? directActionAttributes(actionIndex, actionLabel(legalActions[actionIndex])) : "";
    return `<div id="member-${member.id}" class="player-piece player-piece--${member.type} ${actionIndex >= 0 ? "is-selectable" : ""} ${state.actionFlow?.selectedSource === member.id ? "is-selected" : ""}" ${actionAttributes} style="${playerPieceStyle(config, member.type, index)}" title="${MEMBER_NAMES[member.type]} ${index + 1}"></div>`;
  }).join("");
}

const PLAYER_WORKSPACE_RECTS = {
  courtier: { x: 238, y: 84, width: 57, height: 57 },
  gardener: { x: 238, y: 160, width: 57, height: 57 },
  warrior: { x: 238, y: 236, width: 57, height: 57 },
} as const;

function playerRectStyle(rect: { x: number; y: number; width: number; height: number }) {
  return `left:${(rect.x / PLAYER_BOARD.width) * 100}%;top:${(rect.y / PLAYER_BOARD.height) * 100}%;width:${(rect.width / PLAYER_BOARD.width) * 100}%;height:${(rect.height / PLAYER_BOARD.height) * 100}%`;
}

function renderPlayerWorkspaceInteraction(player: PlayerState) {
  return (Object.entries(PLAYER_WORKSPACE_RECTS) as [keyof typeof PLAYER_WORKSPACE_RECTS, (typeof PLAYER_WORKSPACE_RECTS)[keyof typeof PLAYER_WORKSPACE_RECTS]][]).map(([row, rect]) => {
    const workspaceId = `p${player.id}-domain-${row}`;
    const placeActionIndex = canHumanAct() ? legalActions.findIndex((action) => action.type === "placeDie" && action.workspace === workspaceId) : -1;
    const rewardActionIndex = canHumanAct() && player.id === state.currentPlayer ? domainRewardActionIndex(state, legalActions, row) : -1;
    const actionIndex = rewardActionIndex >= 0 ? rewardActionIndex : placeActionIndex;
    const target = actionIndex >= 0 ? `<button type="button" class="player-board-target" data-action-index="${actionIndex}" data-action-key="action-${actionIndex}" style="${playerRectStyle(rect)}" aria-label="${actionLabel(legalActions[actionIndex])}"><span>${MEMBER_NAMES[row]}</span></button>` : "";
    const dice = state.workspaces[workspaceId]?.dice ?? [];
    const placed = dice.map((die) => `<div class="player-placed-die" style="${playerRectStyle(rect)};${dieSpriteStyle(die)}" title="${COLORS[die.color]}色 ${die.value} 点"></div>`).join("");
    return target + placed;
  }).join("");
}

function renderPlayer(player: PlayerState, index: number) {
  const config = { ...PLAYER_STYLES[player.id], name: player.name } as PlayerSetup;
  const r = player.resources;
  const lantern = lanternLayout(player.lanternCards.length);
  return `<article class="player-area ${state.currentPlayer === player.id ? "is-current" : ""}"><header class="player-heading"><div><span>顺位 ${state.turnOrder.indexOf(player.id) + 1}</span><h2>${escapeHtml(player.name)}</h2></div><div class="player-counters"><b>钱币 ${r.coins}</b><b>家纹 ${r.seals}</b><b>影响 ${player.influence}</b><b>分数 ${player.points}</b></div></header>
    <div class="player-table ${state.playerCount === 4 ? "player-table--four" : ""}" style="aspect-ratio:${PLAYER_BOARD.width}/${lantern.canvasHeight}"><div class="player-board" style="${playerBoardStyle(config)}">${renderPlayerPieces(player, config)}${renderPlayerWorkspaceInteraction(player)}${Array.from({ length: Math.min(5, Math.max(0, r.seals)) }, (_, index) => `<div class="seal-marker" data-seal-index="${index}" style="${sealMarkerStyle(index)}" title="家纹 ${index + 1}"></div>`).join("")}
      <div class="resource-marker resource-marker--food" style="${resourceMarkerStyle("food", r.food)}"></div><div class="resource-marker resource-marker--iron" style="${resourceMarkerStyle("iron", r.iron)}"></div><div class="resource-marker resource-marker--pearl" style="${resourceMarkerStyle("pearl", r.pearl)}"></div>
      <div class="player-action-slot">${renderDomainCard(player)}</div>
    </div>${renderLanternCards(player)}</div></article>`;
}

function formatAuthoritativeHistory(record: BglabActionRecord): string {
  const events = record.events as Array<Record<string, unknown>>;
  const action = (record.action ?? {}) as { steps?: Array<Record<string, unknown>> };
  const actorId = record.actor ?? (events.find((event) => Number.isInteger(event.player))?.player as number | undefined) ?? state.currentPlayer;
  const actor = state.players[actorId]?.name ?? `玩家 ${actorId + 1}`;
  const drafted = events.find((event) => event.type === "DieDrafted");
  const placed = events.find((event) => event.type === "DiePlaced");
  const parts: string[] = [];
  if (drafted) {
    const bridge = COLORS[String(drafted.bridge) as DieColor] ?? String(drafted.bridge ?? "");
    const end = drafted.end === "left" ? "左侧" : drafted.end === "right" ? "右侧" : "";
    const value = drafted.die && typeof drafted.die === "object" ? (drafted.die as { value?: number }).value : undefined;
    parts.push(`取${bridge}桥${end} ${value ?? ""}点骰`);
  }
  if (placed) parts.push(`放入${workspaceLabel(String(placed.workspace ?? "版图"))}`);
  const gains = new Map<string, number>();
  for (const event of events) {
    if (event.type !== "ResourceChanged" || event.player !== actorId || Number(event.amount ?? 0) <= 0) continue;
    const resource = String(event.resource) as keyof typeof RESOURCE_NAMES;
    gains.set(RESOURCE_NAMES[resource] ?? resource, (gains.get(RESOURCE_NAMES[resource] ?? resource) ?? 0) + Number(event.amount));
  }
  if (gains.size) parts.push(`获得${[...gains].map(([name, amount]) => `${name} ${amount}`).join("、")}`);
  const major = action.steps?.find((step) => step.op === "beginMajorAction");
  if (major?.mode) parts.push(`开始${MEMBER_NAMES[String(major.mode) as keyof typeof MEMBER_NAMES] ?? String(major.mode)}行动`);
  if (!parts.length) {
    const moved = events.find((event) => event.type === "MemberMoved");
    if (moved) parts.push(`执行${String(moved.to ?? "版图")}行动`);
  }
  return `${actor} ${parts.join("，") || "完成行动"}`;
}

function hydrateAuthoritativeHistory(snapshot: AdapterSnapshot): BglabActionRecord[] {
  return authoritativeHistory(snapshot, formatAuthoritativeHistory, 64);
}

function effectName(effect: Effect): string {
  if (effect.type === "gain") return `获得 ${effect.amount} ${RESOURCE_NAMES[effect.resource]}`;
  if (effect.type === "gainChoice") return `选择获得 ${effect.amount} 个资源`;
  if (effect.type === "gainPoints") return `获得 ${effect.amount} 分`;
  if (effect.type === "influence") return `影响力前进 ${effect.amount}`;
  if (effect.type === "lantern") return "触发灯笼";
  if (effect.type === "wellAction") return "执行水井行动";
  if (effect.type === "domainAction") return "执行个人领地行动";
  if (effect.type === "castleTileAction") return "执行城堡行动行";
  if (effect.type === "majorAction") return `执行${MEMBER_NAMES[effect.action]}行动`;
  if (effect.type === "pay") return `支付 ${effect.amount} ${RESOURCE_NAMES[effect.resource]}${effect.effects.length ? `后${effect.effects.map(effectName).join("并")}` : ""}`;
  return "执行此效果";
}

function effectMarkup(effect: Effect): string {
  if (effect.type === "gain") return `<span class="effect-chip" aria-label="获得 ${effect.amount} ${RESOURCE_NAMES[effect.resource]}" title="获得 ${effect.amount} ${RESOURCE_NAMES[effect.resource]}"><i class="status-icon status-icon--${effect.resource}"></i><b>${effect.amount}</b></span>`;
  if (effect.type === "gainPoints") return `<span class="effect-chip effect-chip--text">${effect.amount} 分</span>`;
  if (effect.type === "influence") return `<span class="effect-chip effect-chip--text">影响力 ${effect.amount}</span>`;
  if (effect.type === "lantern") return `<span class="effect-chip" title="获得灯笼区全部奖励"><i class="status-icon status-icon--lantern" aria-hidden="true"></i><span>灯笼奖励</span></span>`;
  if (effect.type === "wellAction") return `<span class="effect-chip effect-chip--text">水井行动</span>`;
  if (effect.type === "majorAction") return `<span class="effect-chip effect-chip--text">${MEMBER_NAMES[effect.action]}行动</span>`;
  if (effect.type === "pay") return `<span class="effect-chip effect-chip--text">支付 ${effect.amount} ${RESOURCE_NAMES[effect.resource]}</span>${effect.effects.length ? `<span class="effect-plus">→</span>${effect.effects.map(effectMarkup).join(`<span class="effect-plus">＋</span>`)}` : ""}`;
  return `<span class="effect-chip effect-chip--text">${effectName(effect)}</span>`;
}

function actionGroupMarkup(group: Effect[]): string {
  return group.map(effectMarkup).join(`<span class="effect-plus">＋</span>`);
}

function effectChoiceLabel(action: Extract<GameAction, { type: "chooseEffectOption" }>) {
  const pending = state.pendingEffects[0];
  if (!pending) return "选择";
  const effect = pending.effect;
  if (effect.type === "gainChoice") return `获得 1 ${["食物", "铁", "珍珠母"][action.option]}`;
  if (effect.type === "effectOrder") return `先${effectName(effect.effects[action.option])}`;
  if (effect.type === "actionOrder") return effect.groups[action.option].effects.map(effectName).join(" ＋ ");
  if (effect.type === "chooseOne") {
    const room = state.board.castleRooms.find((candidate) => candidate.id === pending.source);
    const result = effect.options[action.option]?.map(effectName).join(" ＋ ");
    return room ? `${castleRoomName(room.id, room.floor)}：${result}` : effect.labels[action.option] ?? result ?? "选择奖励";
  }
  if (effect.type === "pay") return payActionLabel(effect, action.option);
  if (effect.type === "influence") return influencePaymentLabel(state, action.option);
  if (effect.type === "daimyoReward") return `选择大名卡第 ${effect.positions[action.option] + 1} 格`;
  if (effect.type === "domainAction") return `个人领地：${MEMBER_NAMES[(["courtier", "gardener", "warrior"] as const).filter((row) => !state.usedDomainRowsThisTurn.includes(row))[action.option]]}`;
  if (effect.type === "castleTileAction") {
    const selector = String(effect.color) === "light" ? "浅色背景" : effect.color === "any" ? "任意" : `${COLORS[effect.color as DieColor]}色`;
    return `执行城内${selector}城堡行动行 ${action.option + 1}`;
  }
  if (effect.type === "gardenActivation") return `激活 ${effect.gardens[action.option].replace("garden-", "庭园 ")}`;
  return `选择 ${action.option + 1}`;
}

function workspaceLabel(workspaceId: string): string {
  const workspace = state.workspaces[workspaceId];
  if (!workspace) return workspaceId;
  const castle = workspaceId.match(/^castle-(steward|diplomat)-(\d)$/);
  if (castle) return `城堡${castle[1] === "steward" ? "家臣层" : "使节层"}·${castle[2] === "1" ? "左" : castle[2] === "2" ? "中" : "右"}`;
  return workspace.label;
}

function majorActionTargetLabel(target: string): string {
  if (target === "gate") return "城门";
  if (target === "daimyo") return "大名房间";
  const garden = target.match(/^garden-(black|white|coral)-(\d+)$/);
  if (garden) return `${COLORS[garden[1] as DieColor]}桥庭园 ${garden[2]}`;
  const yard = target.match(/^yard-(\d+)$/);
  if (yard) return `训练场 ${yard[1]}`;
  return target.replace("steward-", "家臣层 ").replace("diplomat-", "使节层 ");
}

function actionLabel(action: GameAction): string {
  if (action.type === "chooseStartingPair") { const offer = state.startingOffers[action.offer]; return `选择组合 ${action.offer + 1}（资源卡 ${offer.resourceCard} ＋ 行动卡 ${offer.actionCard}）`; }
  if (action.type === "draftDie") { const dice = state.bridges[action.bridge]; const die = action.end === "left" ? dice[0] : dice.at(-1)!; return `拿取${COLORS[action.bridge]}桥${action.end === "left" ? "左端" : "右端"} ${die.value} 点骰${action.end === "left" ? "（触发灯笼）" : ""}`; }
  if (action.type === "placeDie") {
    const workspace = state.workspaces[action.workspace];
    const die = state.draftedDie?.die;
    if (!die) return `放到 ${workspaceLabel(action.workspace)}`;
    const delta = placementCoinDelta(workspace, die);
    const reference = placementReferenceValue(workspace);
    return `放到 ${workspaceLabel(action.workspace)}（参照 ${reference} 点，${delta === 0 ? "无需钱币" : `${delta > 0 ? "获得" : "支付"} ${Math.abs(delta)} 钱币`}）`;
  }
  if (action.type === "chooseEffectOption") return effectChoiceLabel(action);
  if (action.type === "selectCastleTileAction") {
    const room = state.board.castleRooms.find((candidate) => candidate.id === action.room);
    const slot = room?.slots.find((candidate) => candidate.rowId === action.rowId);
    const rowIndex = room?.slots.findIndex((candidate) => candidate.rowId === action.rowId) ?? -1;
    const pending = state.pendingEffects[0]?.effect;
    const selector = pending?.type === "castleTileAction" && pending.color === "light" ? "light" : undefined;
    return room && slot ? castleTileActionLabel(room.id, room.floor, slot.color, rowIndex, selector) : "选择城堡行动行";
  }
  if (action.type === "exchangeSeal") return action.receive === "coins" ? "1 家纹换 1 钱币" : `2 家纹换 1 ${RESOURCE_NAMES[action.receive]}`;
  if (action.type === "beginMajorAction") return action.mode === "recruit" ? "请求觐见：家臣进入城门" : action.mode === "promote" ? "提升层级：选择城堡中的家臣" : `执行${MEMBER_NAMES[action.mode]}行动`;
  if (action.type === "selectMajorActionSource") {
    const member = state.players[state.currentPlayer].members.find((candidate) => candidate.id === action.member);
    return `选择${member ? MEMBER_NAMES[member.type] : "米宝"} ${Number(action.member.split("-").at(-1))}`;
  }
  if (action.type === "selectMajorActionTarget") {
    const target = majorActionTargetLabel(action.target);
    return `${action.levels ? `晋升 ${action.levels} 层至` : "放置到"}${target}`;
  }
  if (action.type === "cancelMajorActionSelection") return "取消选择";
  if (action.type === "confirmMajorAction") return "确认花费并执行";
  if (action.type === "refreshCastleRoom") return `确认奖励并为${action.room.startsWith("steward") ? "家臣层" : "使节层"}补充卡牌`;
  if (action.type === "finishMajorAction") return "结束本次人物行动";
  return state.phase === "roundEnd" ? "完成庭园结算" : "结束本回合";
}

function actionGroup(action: GameAction) {
  if (action.type === "exchangeSeal") return "随时兑换";
  if (action.type === "chooseStartingPair") return "选择初始组合";
  if (action.type === "draftDie") return "选择骰子";
  if (action.type === "placeDie") return "选择工位";
  if (action.type === "beginMajorAction") return "选择子行动";
  if (action.type === "selectMajorActionSource") return "选择米宝";
  if (action.type === "selectMajorActionTarget") return "选择目的地";
  if (action.type === "confirmMajorAction") return "确认行动";
  return "处理行动";
}

function formatAction(actions: GameAction[], events: GameEvent[], actor: string, sourceState: GameState): string {
  const steps = actions;
  const draft = steps.find((action): action is Extract<GameAction, { type: "draftDie" }> => action.type === "draftDie");
  const placement = steps.find((action): action is Extract<GameAction, { type: "placeDie" }> => action.type === "placeDie");
  const parts: string[] = [];
  if (draft) {
    const dice = sourceState.bridges[draft.bridge] ?? [];
    const die = draft.end === "left" ? dice[0] : dice.at(-1);
    parts.push(`取${COLORS[draft.bridge]}桥${draft.end === "left" ? "左侧" : "右侧"} ${die?.value ?? "?"} 点骰`);
  }
  if (placement) parts.push(`放入${workspaceLabel(placement.workspace)}`);
  const gains = new Map<string, number>();
  const payments = new Map<string, number>();
  for (const event of events) {
    if (event.type !== "ResourceChanged" || !event.amount) continue;
    const target = event.amount > 0 ? gains : payments;
    target.set(event.resource, (target.get(event.resource) ?? 0) + Math.abs(event.amount));
  }
  if (gains.size) parts.push(`获得${[...gains].map(([resource, amount]) => `${RESOURCE_NAMES[resource as keyof typeof RESOURCE_NAMES]} ${amount}`).join("、")}`);
  if (payments.size) parts.push(`支付${[...payments].map(([resource, amount]) => `${RESOURCE_NAMES[resource as keyof typeof RESOURCE_NAMES]} ${amount}`).join("、")}`);
  const moved = events.find((event) => event.type === "MemberMoved");
  if (moved) {
    const type = moved.member.split("-")[1] as keyof typeof MEMBER_NAMES;
    parts.push(`并开始${MEMBER_NAMES[type] ?? type}行动`);
  }
  const pendingMajor = state.pendingEffects[0]?.effect;
  if (!moved && pendingMajor?.type === "majorAction") parts.push(`并开始${MEMBER_NAMES[pendingMajor.action]}行动`);
  if (events.some((event) => event.type === "CardMoved" && event.from.endsWith("-deck"))) parts.push("公开并刷新卡牌");
  if (events.some((event) => event.type === "RoundStarted")) parts.push("进入新一轮");
  if (!parts.length && steps.length) parts.push(actionLabel(steps.at(-1)!));
  return `${actor} ${parts.join("，")}。`;
}

function renderActions() {
  const startingChoices = legalActions.map((action, index) => ({ action, index })).filter((entry): entry is { action: Extract<GameAction, { type: "chooseStartingPair" }>; index: number } => entry.action.type === "chooseStartingPair");
  if (startingChoices.length) {
    // Starting offers are public even while the other seat is choosing.
    // Keep engine turn ownership: only the active human can select a pair.
    const canChoose = canHumanAct();
    const selectedDraft = canChoose ? setupDraftAction : null;
    const visibleStartingChoices = selectedDraft
      ? startingChoices.filter(({ action }) => action.offer === selectedDraft.offer)
      : startingChoices;
    return `<div class="starting-choice-grid${selectedDraft ? " has-selection" : ""}">${visibleStartingChoices.map(({ action, index }) => {
      const offer = state.startingOffers[action.offer];
      const selected = selectedDraft?.offer === action.offer ? " is-selected" : "";
      return `<button type="button" data-setup-choice-index="${index}"${canChoose ? "" : " disabled"} class="starting-pair${selected}" aria-label="${actionLabel(action)}">${renderStartingCard("resource", offer.resourceCard, "pair-card pair-card--resource")}${renderStartingCard("action", offer.actionCard, "pair-card pair-card--action")}</button>`;
    }).join("")}</div>`;
  }
  if (!canHumanAct()) return `<p class="read-only-hint">${window.BG_REPLAY_MODE ? "历史回放只读" : "等待当前玩家行动"}</p>`;
  const pending = state.pendingEffects[0];
  if (pending?.effect.type === "gainChoice") {
    if (!resourceChoiceDraft || resourceChoiceDraft.effectId !== pending.id) resourceChoiceDraft = { effectId: pending.id, total: pending.effect.amount, picks: [] };
    const selected = resourceChoiceDraft.picks.map((resource) => effectMarkup({ type: "gain", resource, amount: 1 })).join("");
    const remaining = Array.from({ length: resourceChoiceDraft.total - resourceChoiceDraft.picks.length }, () => `<span class="resource-choice-slot" aria-label="尚未选择的资源"><i class="status-icon status-icon--any-resource"></i></span>`).join("");
    const complete = isResourceChoiceComplete(resourceChoiceDraft);
    const choices = complete ? "" : pending.effect.resources.map((resource, option) => `<button type="button" class="resource-choice-button" data-resource-option="${option}">${effectMarkup({ type: "gain", resource, amount: 1 })}</button>`).join("");
    const reset = resourceChoiceDraft.picks.length ? '<button type="button" id="reset-resource-choice" class="utility-button">重置</button>' : "";
    return `<div class="resource-choice-bar"><strong>获得资源 ${resourceChoiceDraft.total}</strong><span class="resource-choice-summary ${complete ? "is-complete" : ""}" aria-label="${complete ? `已选齐 ${resourceChoiceDraft.total} 个资源` : `已选择 ${resourceChoiceDraft.picks.length} 个资源，还需选择 ${resourceChoiceDraft.total - resourceChoiceDraft.picks.length} 个`}">${selected}${remaining}</span>${choices}${reset}</div>`;
  }
  resourceChoiceDraft = null;
  const skipAction = whiteCastleSkipAction(state, legalActions);
  const sharedActionTypes = new Set<GameAction["type"]>(["finishResolution", "finishMajorAction", "cancelMajorActionSelection", "confirmMajorAction", "refreshCastleRoom"]);
  const actionable = legalActions.map((action, index) => ({ action, index })).filter(({ action }) => (
    action.type !== "draftDie"
    && action.type !== "placeDie"
    && !sharedActionTypes.has(action.type)
    && action !== skipAction
    && !isBoardNativeRewardAction(state, action)
  ));
  const buttonActions = actionable.filter(({ action }) => action.type !== "exchangeSeal");
  const groups = [...new Set(buttonActions.map(({ action }) => actionGroup(action)))];
  const settleAll = pending?.effect.type === "effectOrder" || pending?.effect.type === "actionOrder" ? `<button type="button" id="settle-all" class="settle-all">按印刷顺序结算全部</button>` : "";
  const disabled: string[] = [];
  if (pending?.effect.type === "pay" && !legalActions.some((action) => action.type === "chooseEffectOption" && action.effectId === pending.id && action.option === 0)) {
    const current = state.players[state.currentPlayer].resources[pending.effect.resource];
    disabled.push(`<button type="button" disabled title="资源不足">支付 ${pending.effect.amount} ${RESOURCE_NAMES[pending.effect.resource]}<small>缺少 ${Math.max(0, pending.effect.amount - current)}</small></button>`);
  }
  if (pending?.effect.type === "majorAction" && state.actionFlow?.stage === "chooseTarget" && (pending.effect.action === "warrior" || pending.effect.action === "gardener")) {
    const legalTargets = new Set(legalActions.flatMap((action) => action.type === "selectMajorActionTarget" ? [action.target] : []));
    const resource = pending.effect.action === "warrior" ? "iron" : "food";
    const targets = pending.effect.action === "warrior" ? state.trainingYards.map((yard) => ({ id: yard.id, cost: yard.ironCost, name: `训练场 ${yard.id.at(-1)}` })) : state.gardens.map((garden) => ({ id: garden.id, cost: garden.foodCost, name: garden.id.replace("garden-", "庭园 ") }));
    for (const target of targets.filter((candidate) => !legalTargets.has(candidate.id))) {
      const missing = Math.max(0, target.cost - state.players[state.currentPlayer].resources[resource]);
      disabled.push(`<button type="button" disabled>${target.name}<small>${missing ? `缺少 ${missing} ${RESOURCE_NAMES[resource]}` : "位置不可用"}</small></button>`);
    }
  }
  return groups.map((group) => `<div class="action-group"><h3>${group}</h3><div class="action-buttons">${buttonActions.map(({ action, index }) => {
    if (actionGroup(action) !== group) return "";
    const markup = action.type === "chooseEffectOption" && pending?.effect.type === "actionOrder" ? actionGroupMarkup(pending.effect.groups[action.option].effects)
      : action.type === "chooseEffectOption" && pending?.effect.type === "chooseOne" ? actionGroupMarkup(pending.effect.options[action.option])
        : actionLabel(action);
    return `<button type="button" data-action-index="${index}" data-action-key="action-${index}">${markup}</button>`;
  }).join("")}${group === "处理行动" || group === "选择目的地" ? disabled.join("") : ""}</div></div>`).join("") + settleAll;
}

function renderSealActions() {
  const exchangeActions = legalActions
    .map((action, index) => ({ action, index }))
    .filter(({ action }) => action.type === "exchangeSeal");
  if (!canHumanAct() || exchangeActions.length === 0) return "";
  return `<div class="seal-action-menu"><button type="button" id="toggle-seal-menu" class="utility-button seal-action-toggle" aria-expanded="${sealMenuOpen}">大名家纹行动</button>${sealMenuOpen ? `<div class="seal-action-options">${exchangeActions.map(({ action, index }) => `<button type="button" class="utility-button" data-action-index="${index}" data-action-key="action-${index}">${actionLabel(action)}</button>`).join("")}</div>` : ""}</div>`;
}

function renderTurnControls() {
  if (state.phase === "setup" || state.phase === "finished" || !canHumanAct() || undoStack.length === 0) return "";
  const undoDisabled = Boolean(undoLockedReason) || undoStack.length === 0;
  const restartDisabled = Boolean(undoLockedReason) || !turnStartCheckpoint || undoStack.length === 0;
  const rollbackHint = undoLockedReason ?? (undoStack.length > 0 ? `可撤回 ${undoStack.length} 步` : "完成一个暂存操作后可以撤回");
  return `<section class="turn-controls" aria-label="回合控制">
    <button type="button" id="undo-last" ${undoDisabled ? "disabled" : ""} title="${rollbackHint}">↶ 撤回上一步</button>
    <button type="button" id="restart-turn" ${restartDisabled ? "disabled" : ""} title="${rollbackHint}">↶ 撤回全部</button>
    <span class="rollback-hint ${undoLockedReason ? "is-locked" : ""}">${rollbackHint}</span>
  </section>`;
}

function phaseName() {
  return { setup: "选择初始组合", draft: "选择骰子", place: "放置骰子", resolve: "执行行动", roundEnd: "轮末庭园", finished: "游戏结束" }[state.phase];
}

function render() {
  const cards = dynamicBoardCards();
  legalActions = getLegalActions(state);
  const actionsMarkup = renderActions();
  const auxiliaryActions = renderSealActions();
  const turnControls = renderTurnControls();
  const standaloneControls = adapter ? "" : `<div class="manual-game-controls"><span>全席位手动 · ${state.playerCount} 人局</span><label>新局人数 <select id="player-count" aria-label="新局人数">${([2, 3, 4] as const).map((count) => `<option value="${count}" ${count === selectedPlayerCount ? "selected" : ""}>${count} 人</option>`).join("")}</select></label><button id="new-game" type="button">开始新局</button></div>`;
  const shellView = buildWhiteCastleShellView(state, {
    busy,
    announcement: pausedReason ?? (undoLockedReason ? `当前操作不可撤回：${undoLockedReason}` : announcement),
    pausedReason,
    manualTest,
    playerTypes,
    legalActions,
    draftActions,
    resourceChoiceDraft,
    setupDraftAction,
    hasUndo: undoStack.length > 0,
  }, history);
  const turnSurfaceSlot = state.phase === "setup"
    ? `<section class="setup-choice-surface" aria-label="起始卡牌选择">${actionsMarkup}</section>`
    : "";
  const currentActionContent = state.phase === "setup"
    ? ""
    : `<div class="white-castle-current-actions" aria-label="白城堡当前合法操作">${actionsMarkup}</div>`;
  const boardSlot = `<div class="board-column"><section class="board-frame"><section class="board-canvas"><img class="board-base" src="/twc-main-board.png" alt="姬路城公共版图"/><div class="card-layer">${cards.map((card) => `<div class="board-card board-card--${card.atlas}" style="${boardCardStyle(card)}" title="${card.label} · 卡 ${card.card}"></div>`).join("")}</div><div class="component-layer">${renderPublicComponents()}${renderDieTiles()}<div class="deployed-members">${renderDeployedMembers(cards)}</div></div>${renderBoardInteractionLayer(cards)}</section></section>
    <section class="setup-summary"><span>桥上剩余骰：${Object.values(state.bridges).reduce((sum, dice) => sum + dice.length, 0)}</span><span>当前待处理：${state.pendingEffects.length}</span><span>庭园：${state.gardens.length}</span><span>训练场：${state.trainingYards.length}</span></section><section class="player-areas">${state.players.map(renderPlayer).join("")}</section></div>`;
  const sharedShell = window.BGLabGameShell;
  if (!sharedShell) throw new Error("Shared GameShell renderer is missing.");
  app.innerHTML = sharedShell.renderShell(shellView, {
    turnSurfaceHtml: turnSurfaceSlot,
    boardHtml: boardSlot,
    actionPrimaryHtml: currentActionContent,
    actionAuxiliaryHtml: auxiliaryActions,
    actionRollbackHtml: `${turnControls}${standaloneControls}`,
    overlayHtml: '<div id="animation-layer" aria-hidden="true"></div>',
  });
  sharedShell.bindActions(app, {
    cancel:() => {
      if (setupDraftAction) {
        setupDraftAction = null;
        render();
        return;
      }
      if (resourceChoiceDraft && resourceChoiceDraft.picks.length > 0) {
        resourceChoiceDraft.picks = [];
        render();
        return;
      }
      const cancellation = legalActions.find((action) => action.type === "cancelMajorActionSelection");
      if (cancellation) void submitAction(cancellation);
      else restartTurn();
    },
    skip:() => {
      const skip = whiteCastleSkipAction(state, legalActions);
      if (skip) void submitAction(skip);
    },
    confirm:() => {
      if (setupDraftAction) return void submitAction(setupDraftAction);
      if (resourceChoiceDraft && isResourceChoiceComplete(resourceChoiceDraft)) {
        return void submitResourceChoice([...resourceChoiceDraft.picks]);
      }
      const confirmation = legalActions.find((action) => action.type === "finishResolution")
        ?? legalActions.find((action) => action.type === "confirmMajorAction")
        ?? legalActions.find((action) => action.type === "refreshCastleRoom");
      if (confirmation) void submitAction(confirmation);
    },
  });
  bind();
}

function eventText(event: GameEvent) {
  if (event.type === "DieDrafted") return `${state.players[event.player].name}拿取${COLORS[event.bridge]}色 ${event.die.value} 点骰`;
  if (event.type === "DiePlaced") return `骰子放到${workspaceLabel(event.workspace)}`;
  if (event.type === "MemberMoved") {
    const type = event.member.split("-")[1] as keyof typeof MEMBER_NAMES;
    const destination = event.to === "gate" ? "城门" : event.to === "daimyo" ? "大名处" : event.to.replace("garden-", "庭园 ").replace("yard-", "训练场 ").replace("steward-", "家臣层 ").replace("diplomat-", "使节层 ");
    return `${state.players[event.player].name}的${MEMBER_NAMES[type] ?? type}移动到${destination}`;
  }
  if (event.type === "ResourceChanged" && event.amount !== 0) return `${state.players[event.player].name}${event.amount > 0 ? "获得" : "支付"}${Math.abs(event.amount)} ${RESOURCE_NAMES[event.resource]}`;
  if (event.type === "InfluenceChanged" && event.amount !== 0) return `${state.players[event.player].name}的影响力${event.amount > 0 ? "前进" : "后退"}${Math.abs(event.amount)}格`;
  if (event.type === "CardMoved") return `卡牌 ${event.card}：${event.from} → ${event.to}`;
  if (event.type === "RoundStarted") return `第 ${event.round} 轮开始`;
  if (event.type === "GameScored") return `游戏结束，${state.players[event.winner].name}获胜`;
  return "";
}

function memberOrigins() {
  return new Map(state.players.flatMap((player) => player.members.map((member) => [member.id, document.getElementById(`member-${member.id}`)?.getBoundingClientRect()])).filter((entry): entry is [string, DOMRect] => Boolean(entry[1])));
}

function saveState() {
  if (manualSession) localStorage.setItem(STORAGE_KEY, serializeGame(state));
  if (window.BG_GAME_ID) Bridge.persist();
}

function checkpoint(): UndoEntry {
  return { snapshot: serializeGame(state), history: [...history], actionCount: draftActions.length };
}

function beginTurnTransaction() {
  decisionStartId = authorityRevision();
  decisionStartState = structuredClone(authorityState());
  state = structuredClone(decisionStartState);
  draftActions = [];
  setupDraftAction = null;
  turnStartCheckpoint = checkpoint();
  undoStack = [];
  undoLockedReason = null;
}

function clearTurnTransaction() {
  turnStartCheckpoint = null;
  undoStack = [];
  undoLockedReason = null;
  draftActions = [];
  setupDraftAction = null;
}

function resetTransactionForLoadedState() {
  clearTurnTransaction();
  if (state.phase === "setup" || state.phase === "draft") beginTurnTransaction();
}

function irreversibleReason(events: GameEvent[]): string | null {
  if (events.some((event) => event.type === "CardMoved" && event.from.endsWith("-deck"))) return "已经公开并刷新卡牌，不能撤回";
  if (events.some((event) => event.type === "RoundStarted")) return "已经掷出下一轮骰子，不能撤回";
  if (events.some((event) => event.type === "GameScored")) return "游戏已经完成终局结算";
  return null;
}

function applyIrreversibleBoundary(events: GameEvent[]) {
  const reason = irreversibleReason(events);
  if (!reason) return false;
  undoLockedReason = reason;
  undoStack = [];
  return true;
}

function restoreCheckpoint(entry: UndoEntry) {
  state = deserializeGame(entry.snapshot);
  history = [...entry.history];
  draftActions = draftActions.slice(0, entry.actionCount);
  announcement = null;
  if (manualSession) saveState();
  render();
}

function undoLastAction() {
  if (busy || undoLockedReason) return;
  const entry = undoStack.pop();
  if (entry) restoreCheckpoint(entry);
}

function restartTurn() {
  if (busy || undoLockedReason || !turnStartCheckpoint) return;
  const entry = turnStartCheckpoint;
  restoreCheckpoint(entry);
  beginTurnTransaction();
}

function actionStep(action: GameAction): ActionStep {
  const { type, ...fields } = action;
  return { op: type, ...fields };
}

function applyDeterministicPresentationSteps(events: GameEvent[]): void {
  while (true) {
    const actions = getLegalActions(state);
    const next = deterministicMajorActionStart(state, actions)
      ?? deterministicGardenActivation(state, actions)
      ?? lanternCollectionStep(state, actions);
    if (!next) return;
    const transition = applyAction(state, next);
    state = transition.state;
    draftActions.push(structuredClone(next));
    events.push(...transition.events);
  }
}

function decisionEnded(candidate: GameState): boolean {
  if (candidate.phase === "finished") return true;
  if (decisionStartState.phase === "setup") {
    return candidate.phase !== "setup" || candidate.setupIndex > decisionStartState.setupIndex;
  }
  return candidate.turn > decisionStartState.turn || candidate.currentPlayer !== decisionStartState.currentPlayer;
}

function commitDraft(): void {
  const transaction = {
    steps: [{ op: "begin", action: "turn" }, ...draftActions.map(actionStep)],
  };
  if (manualSession) {
    manualSession.commit(decisionStartId, draftActions);
  } else {
    const result = adapter!.dispatch(decisionStartId, transaction);
    if (!result.ok) {
      const failed = result.failedStep ?? 0;
      throw new Error(`整轮提交失败（步骤 ${failed}）：${result.message ?? result.code}\n${result.correction ?? ""}`);
    }
  }
  state = authorityState();
  history = authorityHistory();
  announcement = history[0]?.text ?? null;
  clearTurnTransaction();
  if (state.phase !== "finished") beginTurnTransaction();
  saveState();
  window.setTimeout(() => void requestCurrentAI(), 0);
}

async function requestCurrentAI() {
  if (!adapter || pausedReason || state.phase === "finished" || playerTypes[state.currentPlayer] !== "ai") return;
  const turnId = adapter.decisionId();
  if (aiRequestId === turnId) return;
  aiRequestId = turnId;
  busy = true;
  announcement = `${state.players[state.currentPlayer].name}正在思考…`;
  render();
  try {
    const response = await Bridge.requestAITurn(state.currentPlayer) as {
      retry?: boolean;
      transaction?: { steps?: ActionStep[] };
      action?: { steps?: ActionStep[] };
      canonicalAction?: { steps?: ActionStep[] };
      effects?: GameEvent[];
    };
    state = adapter.snapshot().game;
    history = hydrateAuthoritativeHistory(adapter.snapshot());
    const aiMessage = !response.retry ? history[0]?.text ?? "" : "";
    beginTurnTransaction();
    saveState();
    if (aiMessage) await playAnnouncements([aiMessage]);
  } catch (error) {
    pausedReason = error instanceof Error ? error.message : "AI API error; game paused";
  } finally {
    aiRequestId = null;
    busy = false;
    announcement = pausedReason;
    render();
    if (!pausedReason) window.setTimeout(() => void requestCurrentAI(), 0);
  }
}

async function playAnnouncements(messages: string[]) {
  for (const message of messages) {
    announcement = message;
    render();
    await new Promise((resolve) => window.setTimeout(resolve, 700));
  }
  announcement = null;
  render();
}

async function submitAction(action: GameAction) {
  if (busy || !canHumanAct()) return;
  busy = true;
  const origins = memberOrigins();
  const previousPhase = state.phase;
  const before = previousPhase !== "finished" ? checkpoint() : null;
  const sourceState = structuredClone(decisionStartState);
  const actor = state.players[state.currentPlayer].name;
  try {
    const transition = applyAction(state, action);
    state = transition.state;
    const events = [...transition.events];
    if (action.type === "chooseStartingPair") setupDraftAction = null;
    draftActions.push(structuredClone(action));
    applyDeterministicPresentationSteps(events);
    const actionChain = [...draftActions];
    const messages = [formatAction(actionChain, events, actor, sourceState)];
    if (!decisionEnded(state)) announcement = messages[0] ?? null;
    if (!applyIrreversibleBoundary(events) && before) undoStack.push(before);
    if (decisionEnded(state)) commitDraft();
    if (manualSession) saveState();
    render();
    await animateMoves(events, origins);
    await playAnnouncements(messages);
  } catch (error) {
    console.error(error);
    alert(error instanceof Error ? error.message : "操作失败");
  } finally {
    busy = false;
    announcement = null;
    render();
  }
}

async function submitResourceChoice(picks: CappedResource[]) {
  if (busy || !canHumanAct() || picks.length === 0) return;
  busy = true;
  const events: GameEvent[] = [];
  const before = !undoLockedReason && state.phase !== "setup" ? checkpoint() : null;
  const sourceState = structuredClone(decisionStartState);
  const actor = state.players[state.currentPlayer].name;
  try {
    for (const resource of picks) {
      const pending = state.pendingEffects[0];
      if (!pending || pending.effect.type !== "gainChoice") throw new Error("资源选择已发生变化。");
      const option = pending.effect.resources.indexOf(resource);
      const action = getLegalActions(state).find((candidate): candidate is Extract<GameAction, { type: "chooseEffectOption" }> => candidate.type === "chooseEffectOption" && candidate.effectId === pending.id && candidate.option === option);
      if (!action) throw new Error(`${RESOURCE_NAMES[resource]}当前不可选择。`);
      const transition = applyAction(state, action);
      state = transition.state;
      draftActions.push(structuredClone(action));
      events.push(...transition.events);
    }
    resourceChoiceDraft = null;
    applyDeterministicPresentationSteps(events);
    const messages = [formatAction([...draftActions], events, actor, sourceState)];
    if (!decisionEnded(state)) announcement = messages[0] ?? null;
    if (!applyIrreversibleBoundary(events) && before) undoStack.push(before);
    if (decisionEnded(state)) commitDraft();
    if (manualSession) saveState();
    render();
    await playAnnouncements(messages);
  } catch (error) {
    console.error(error);
    alert(error instanceof Error ? error.message : "资源选择失败");
  } finally {
    busy = false;
    announcement = null;
    render();
  }
}

async function settleAll() {
  if (busy || !canHumanAct()) return;
  busy = true;
  const events: GameEvent[] = [];
  const before = !undoLockedReason && state.phase !== "setup" ? checkpoint() : null;
  const sourceState = structuredClone(decisionStartState);
  const actor = state.players[state.currentPlayer].name;
  try {
    while (state.pendingEffects[0]?.effect.type === "effectOrder" || state.pendingEffects[0]?.effect.type === "actionOrder") {
      const next = getLegalActions(state).find((action): action is Extract<GameAction, { type: "chooseEffectOption" }> => action.type === "chooseEffectOption" && action.option === 0);
      if (!next) break;
      const transition = applyAction(state, next);
      state = transition.state;
      draftActions.push(structuredClone(next));
      events.push(...transition.events);
    }
    applyDeterministicPresentationSteps(events);
    const messages = events.length ? [formatAction([...draftActions], events, actor, sourceState)] : [];
    if (!decisionEnded(state)) announcement = messages[0] ?? null;
    if (events.length > 0 && !applyIrreversibleBoundary(events) && before) undoStack.push(before);
    if (decisionEnded(state)) commitDraft();
    if (manualSession) saveState();
    render();
    await playAnnouncements(messages);
  } catch (error) {
    console.error(error);
    alert(error instanceof Error ? error.message : "结算失败");
  } finally {
    busy = false;
    announcement = null;
    render();
  }
}

async function animateMoves(events: GameEvent[], origins: Map<string, DOMRect>) {
  for (const event of events) {
    if (event.type !== "MemberMoved") continue;
    const target = document.getElementById(`member-${event.member}`);
    const from = origins.get(event.member);
    if (!target || !from) continue;
    const to = target.getBoundingClientRect();
    const clone = target.cloneNode(true) as HTMLElement;
    Object.assign(clone.style, { position: "fixed", left: `${from.left}px`, top: `${from.top}px`, width: `${from.width}px`, height: `${from.height}px`, zIndex: "2000", margin: "0" });
    document.body.append(clone); target.style.visibility = "hidden";
    await clone.animate([{ transform: "translate(0,0)" }, { transform: `translate(${to.left - from.left}px,${to.top - from.top}px)` }], { duration: 650, easing: "cubic-bezier(.2,.75,.2,1)" }).finished;
    clone.remove(); target.style.visibility = "";
  }
}

function bind() {
  document.querySelectorAll<HTMLButtonElement>("[data-setup-choice-index]").forEach((control) => control.addEventListener("click", () => {
    if (!canHumanAct()) return;
    const action = legalActions[Number(control.dataset.setupChoiceIndex)];
    if (action?.type === "chooseStartingPair") {
      setupDraftAction = setupDraftAction?.offer === action.offer
        ? null
        : structuredClone(action);
      render();
    }
  }));
  document.querySelectorAll<HTMLElement>("[data-action-index]").forEach((control) => {
    const activate = () => {
      const action = legalActions[Number(control.dataset.actionIndex)];
      if (action) void submitAction(action);
    };
    control.addEventListener("click", activate);
    if (!(control instanceof HTMLButtonElement)) control.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activate(); }
    });
  });
  const setLinkedHighlight = (key: string | undefined, active: boolean) => {
    if (!key) return;
    document.querySelectorAll<HTMLElement>(`[data-action-key="${key}"]`).forEach((control) => control.classList.toggle("is-linked-hover", active));
  };
  document.querySelectorAll<HTMLElement>("[data-action-key]").forEach((control) => {
    control.addEventListener("pointerenter", () => setLinkedHighlight(control.dataset.actionKey, true));
    control.addEventListener("pointerleave", () => setLinkedHighlight(control.dataset.actionKey, false));
    control.addEventListener("focusin", () => setLinkedHighlight(control.dataset.actionKey, true));
    control.addEventListener("focusout", () => setLinkedHighlight(control.dataset.actionKey, false));
  });
  document.querySelector("#settle-all")?.addEventListener("click", () => void settleAll());
  document.querySelector("#toggle-seal-menu")?.addEventListener("click", () => { sealMenuOpen = !sealMenuOpen; render(); });
  document.querySelectorAll<HTMLButtonElement>("[data-resource-option]").forEach((control) => control.addEventListener("click", () => {
    const pending = state.pendingEffects[0];
    if (!pending || pending.effect.type !== "gainChoice" || busy) return;
    const option = Number(control.dataset.resourceOption);
    const resource = pending.effect.resources[option];
    if (!resourceChoiceDraft || resourceChoiceDraft.effectId !== pending.id) resourceChoiceDraft = { effectId: pending.id, total: pending.effect.amount, picks: [] };
    resourceChoiceDraft = addResourcePick(resourceChoiceDraft, resource);
    render();
  }));
  document.querySelector("#reset-resource-choice")?.addEventListener("click", () => {
    if (resourceChoiceDraft) resourceChoiceDraft.picks = [];
    render();
  });
  document.querySelector("#undo-last")?.addEventListener("click", undoLastAction);
  document.querySelector("#restart-turn")?.addEventListener("click", restartTurn);
  document.querySelector<HTMLSelectElement>("#player-count")?.addEventListener("change", (event) => {
    selectedPlayerCount = Number((event.target as HTMLSelectElement).value) as PlayerCount;
  });
  document.querySelector("#new-game")?.addEventListener("click", () => startFrontend({ playerCount: selectedPlayerCount }));
}

interface FrontendConfig {
  playerCount?: PlayerCount;
  playerTypes?: string[];
  names?: string[];
  seed?: number;
  mode?: string;
  manualTest?: boolean;
}

function resolvePlayerTypes(config: FrontendConfig, count: number): string[] {
  if (config.playerTypes && (config.playerTypes.length !== count || config.playerTypes.some((type) => type !== "human" && type !== "ai"))) {
    throw new Error("玩家席位配置与当前对局不一致。");
  }
  return config.playerTypes ? [...config.playerTypes] : Array.from({ length: count }, () => "human");
}

function resetUiState() {
  busy = false;
  announcement = null;
  resourceChoiceDraft = null;
  sealMenuOpen = false;
  pausedReason = null;
  aiRequestId = null;
  history = [];
  manualTest = false;
}

function randomSeed() {
  return crypto.getRandomValues(new Uint32Array(1))[0];
}

function startFrontend(config: FrontendConfig = {}) {
  resetUiState();
  manualTest = Boolean(config.manualTest || config.mode === "manual-test");
  if (adapter) {
    adapter.start({ playerCount: config.playerCount, names: config.names, seed: config.seed ?? randomSeed() });
  } else {
    const count = config.playerCount ?? config.names?.length ?? selectedPlayerCount;
    manualSession = new ManualGameSession(createGame({ seed: config.seed ?? randomSeed(), playerCount: count as PlayerCount,
      playerNames: config.names ?? PLAYER_STYLES.slice(0, count).map((player) => player.name), manualSetup: true }));
  }
  state = authorityState();
  playerTypes = resolvePlayerTypes(config, state.playerCount);
  if (manualSession) { playerTypes = state.players.map(() => "human"); manualTest = true; }
  selectedPlayerCount = state.playerCount;
  history = authorityHistory();
  beginTurnTransaction();
  if (window.BG_GAME_ID) Bridge.init();
  saveState();
  render();
  window.setTimeout(() => void requestCurrentAI(), 0);
}

function restoreFrontend(snapshot: AdapterSnapshot, config: FrontendConfig = {}) {
  if (!adapter) throw new Error("本地手动游戏使用浏览器存档恢复。");
  // The page bootstrap and the authenticated websocket can deliver the same
  // confirmed snapshot back-to-back.  Keep the in-flight AI admission for
  // that decision so the second restore cannot start a duplicate request.
  const pendingAiDecision = aiRequestId === snapshot.decisionId ? aiRequestId : null;
  resetUiState();
  manualTest = Boolean(config.manualTest || config.mode === "manual-test");
  adapter.restore(snapshot);
  state = adapter.snapshot().game;
  playerTypes = resolvePlayerTypes(config, state.playerCount);
  selectedPlayerCount = state.playerCount;
  history = hydrateAuthoritativeHistory(adapter.snapshot());
  beginTurnTransaction();
  if (window.BG_GAME_ID) Bridge.init();
  if (pendingAiDecision) {
    aiRequestId = pendingAiDecision;
    busy = true;
    announcement = `${state.players[state.currentPlayer].name}正在思考…`;
  }
  render();
  if (!pendingAiDecision) window.setTimeout(() => void requestCurrentAI(), 0);
}

window.BGLabFrontend = {
  start: startFrontend,
  restore: restoreFrontend,
  bridgeReady() {
    if (pausedReason !== "Bridge not connected") return;
    pausedReason = null;
    busy = false;
    announcement = null;
    render();
    window.setTimeout(() => void requestCurrentAI(), 0);
  },
  pause(message: string) {
    pausedReason = message;
    busy = true;
    announcement = message;
    render();
  },
  status() {
    return adapter ? { ...adapter.snapshot().wrapper, paused: pausedReason }
      : { playerCount: state.playerCount, phase: state.phase, round: state.round, turn: state.turn, currentPlayer: state.currentPlayer, playerTypes, manualTest: true, paused: pausedReason };
  },
};

history = authorityHistory();
resetTransactionForLoadedState();
beginTurnTransaction();
render();
