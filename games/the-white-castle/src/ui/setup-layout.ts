import { materialAssetCss } from "./material-assets";

export const PUBLIC_BOARD = { width: 900, height: 460 } as const;
export const PLAYER_BOARD = { width: 700, height: 457.92019 } as const;

export interface SpritePlacement {
  id: string;
  file: string;
  columns?: number;
  rows?: number;
  column?: number;
  row?: number;
  x: number;
  y: number;
  width: number;
  height: number;
  label: string;
  kind: string;
  z?: number;
}

export interface PlayerSetup {
  id: "blue" | "red" | "yellow" | "green";
  name: string;
  boardColumn: number;
  boardRow: number;
  pieceColumn: number;
  startingSetId: string;
}

export interface StartingSet {
  id: string;
  actionCard: number;
  resourceCard: number;
  resourceBack: "food" | "iron" | "pearl";
  resources: { food: number; iron: number; pearl: number; coins: number; seals: number };
}

export type DieTileColor = "red" | "black" | "white";
export type DieTileReward = "coin" | "resource" | "food" | "pearl" | "iron";
export type DieTileFace = "color" | "reward";

export interface DieTileSetup {
  id: string;
  order: number;
  slot: number;
  color: DieTileColor;
  reward: DieTileReward;
  face: DieTileFace;
  x: number;
  y: number;
}

const dieTileIndex = {
  red: [0, 0], black: [1, 0], white: [2, 0],
  coin: [0, 1], resource: [1, 1], food: [2, 1], pearl: [3, 1], iron: [4, 1],
} as const;

const DIE_TILE_SLOT_COORDINATES = [
  { slot: -3, x: 353, y: 299 }, { slot: -2, x: 550, y: 299 }, { slot: -1, x: 748, y: 299 },
  { slot: 1, x: 392, y: 201 }, { slot: 2, x: 590, y: 200 },
  { slot: 3, x: 353, y: 326 }, { slot: 4, x: 550, y: 326 }, { slot: 5, x: 748, y: 326 },
  { slot: 6, x: 392, y: 256 }, { slot: 7, x: 590, y: 256 },
  { slot: 8, x: 353, y: 354 }, { slot: 9, x: 550, y: 354 }, { slot: 10, x: 748, y: 354 },
  { slot: 11, x: 330, y: 411 }, { slot: 12, x: 330, y: 437 },
] as const;

/**
 * All fifteen physical tiles are used. Each colour occurs five times and each
 * reward occurs three times. The deterministic order follows the rulebook:
 * first one tile of each colour in -3/-2/-1, then numbered slots 1 through 12.
 */
const DIE_TILE_PAIRS: ReadonlyArray<readonly [DieTileColor, DieTileReward]> = [
  ["red", "coin"], ["black", "resource"], ["white", "food"],
  ["red", "resource"], ["black", "coin"], ["black", "food"],
  ["white", "coin"], ["red", "food"], ["white", "resource"],
  ["red", "pearl"], ["white", "pearl"], ["red", "iron"],
  ["black", "pearl"], ["black", "iron"], ["white", "iron"],
];

export const DIE_TILES: DieTileSetup[] = DIE_TILE_SLOT_COORDINATES.map((position, index) => ({
  id: `die-tile-${index + 1}`,
  order: index + 1,
  ...position,
  color: DIE_TILE_PAIRS[index][0],
  reward: DIE_TILE_PAIRS[index][1],
  face: "color",
}));

function assertDieTileSetup(tiles: DieTileSetup[]) {
  if (tiles.length !== 15) throw new Error(`Expected 15 die tiles, received ${tiles.length}.`);
  for (const color of ["red", "black", "white"] as const) {
    if (tiles.filter((tile) => tile.color === color).length !== 5) throw new Error(`Die tile colour ${color} must occur five times.`);
  }
  for (const reward of ["coin", "resource", "food", "pearl", "iron"] as const) {
    if (tiles.filter((tile) => tile.reward === reward).length !== 3) throw new Error(`Die tile reward ${reward} must occur three times.`);
  }
}

assertDieTileSetup(DIE_TILES);

const YARD_GOLD_ATLAS_COLUMNS = [1, 0, 3, 2, 5, 4, 7, 6] as const;

export function yardTileSpriteCell(id: number, face: "blue" | "gold"): { column: number; row: number } {
  if (!Number.isInteger(id) || id < 1 || id > 8) throw new Error(`Unknown yard tile ${id}.`);
  return {
    column: face === "blue" ? id - 1 : YARD_GOLD_ATLAS_COLUMNS[id - 1],
    row: face === "gold" ? 1 : 0,
  };
}

function yard(id: number, x: number, y: number): SpritePlacement {
  return { id: `yard-${id}`, file: "/twc-yard-tiles.png", columns: 8, rows: 2, ...yardTileSpriteCell(id, "blue"), x, y, width: 46, height: 36, label: `训练场板块 ${id}`, kind: "yard-tile", z: 3 };
}

function die(id: string, color: "red" | "white" | "black", value: number, x: number, y: number, size: number): SpritePlacement {
  const row = color === "red" ? 0 : color === "white" ? 1 : 2;
  return { id, file: "/twc-dice.png", columns: 6, rows: 3, column: value - 1, row, x, y, width: size, height: size, label: `${color} ${value} 点骰子`, kind: "die", z: 8 };
}

function bridgeDice(color: "red" | "white" | "black", values: [number, number, number], y: number) {
  return [
    die(`${color}-left`, color, values[0], 28, y, 35),
    die(`${color}-center`, color, values[1], 89, y + 6.5, 22),
    die(`${color}-right`, color, values[2], 133, y, 35),
  ];
}

function playerMarker(id: string, file: string, column: number, x: number, y: number, width: number, height: number, kind: string, label: string, z = 9): SpritePlacement {
  return { id, file, columns: 4, rows: 1, column, row: 0, x, y, width, height, kind, label, z };
}

/** Deterministic two-player reference setup. Random choices are stored here, never made by the renderer. */
export const INITIAL_SETUP = {
  seed: "twc-reference-2p-001",
  playerCount: 2,
  round: 1,
  bridgeDice: {
    red: [2, 5, 5] as [number, number, number],
    black: [2, 4, 5] as [number, number, number],
    white: [1, 2, 5] as [number, number, number],
  },
  players: [
    { id: "blue", name: "蓝色家族", boardColumn: 0, boardRow: 0, pieceColumn: 1, startingSetId: "starting-set-1" },
    { id: "red", name: "红色家族", boardColumn: 1, boardRow: 1, pieceColumn: 0, startingSetId: "starting-set-2" },
  ] satisfies PlayerSetup[],
  discardedStartingSetIds: ["starting-set-3"],
} as const;

/** Verified atlas cells: blue/green upper row, yellow/red lower row. */
export const PLAYER_STYLES: readonly PlayerSetup[] = [
  ...INITIAL_SETUP.players,
  { id: "yellow", name: "黄色家族", boardColumn: 0, boardRow: 1, pieceColumn: 2, startingSetId: "" },
  { id: "green", name: "绿色家族", boardColumn: 1, boardRow: 0, pieceColumn: 3, startingSetId: "" },
];

/** Player-count + 1 paired offers. Cards in an offer are never separated. */
export const STARTING_SETS: StartingSet[] = [
  { id: "starting-set-1", actionCard: 1, resourceCard: 1, resourceBack: "pearl", resources: { food: 2, iron: 1, pearl: 0, coins: 2, seals: 1 } },
  { id: "starting-set-2", actionCard: 2, resourceCard: 6, resourceBack: "iron", resources: { food: 1, iron: 2, pearl: 1, coins: 3, seals: 1 } },
  { id: "starting-set-3", actionCard: 3, resourceCard: 4, resourceBack: "food", resources: { food: 3, iron: 0, pearl: 1, coins: 2, seals: 1 } },
];

if (STARTING_SETS.length !== INITIAL_SETUP.playerCount + 1) throw new Error("Starting offers must equal player count + 1.");
if (new Set(STARTING_SETS.flatMap((set) => [set.actionCard, set.resourceCard])).size < 3) throw new Error("Starting offers must contain card data.");

export function startingSetFor(player: PlayerSetup) {
  const set = STARTING_SETS.find((candidate) => candidate.id === player.startingSetId);
  if (!set) throw new Error(`Missing starting set ${player.startingSetId}.`);
  return set;
}

export const PUBLIC_COMPONENTS: SpritePlacement[] = [
  yard(1, 656, 100), yard(2, 709, 100), yard(3, 780, 100), yard(4, 752, 189),

  ...bridgeDice("red", INITIAL_SETUP.bridgeDice.red, 89.5),
  ...bridgeDice("black", INITIAL_SETUP.bridgeDice.black, 217),
  ...bridgeDice("white", INITIAL_SETUP.bridgeDice.white, 345),

  { id: "round-1", file: "/twc-round-marker.png", x: 827, y: -1.5, width: 23.50242, height: 35, label: "第一轮标记", kind: "round-marker", z: 10 },
  playerMarker("heron-blue", "/twc-herons.png", 1, 104, -6.5, 39, 35, "heron", "蓝色白鹭顺位 1"),
  playerMarker("heron-red", "/twc-herons.png", 0, 71, 9.5, 39, 35, "heron", "红色白鹭顺位 2"),
  playerMarker("influence-blue", "/twc-influence-markers.png", 1, 146, 34.5, 25, 24.07407, "influence", "蓝色影响力起点"),
  playerMarker("influence-red", "/twc-influence-markers.png", 0, 146, 41, 25, 24.07407, "influence", "红色影响力起点", 10),
];

export function placementStyle(item: SpritePlacement, board: { width: number; height: number } = PUBLIC_BOARD) {
  const columns = item.columns ?? 1;
  const rows = item.rows ?? 1;
  const positionX = columns === 1 ? 0 : ((item.column ?? 0) / (columns - 1)) * 100;
  const positionY = rows === 1 ? 0 : ((item.row ?? 0) / (rows - 1)) * 100;
  return [
    `left:${(item.x / board.width) * 100}%`, `top:${(item.y / board.height) * 100}%`,
    `width:${(item.width / board.width) * 100}%`, `height:${(item.height / board.height) * 100}%`,
    `background-image:url('${item.file}')`, `background-size:${columns * 100}% ${rows * 100}%`,
    `background-position:${positionX}% ${positionY}%`, `z-index:${item.z ?? 2}`,
  ].join(";");
}

export function playerBoardStyle(player: PlayerSetup) {
  return `background-position:${player.boardColumn * 100}% ${player.boardRow * 100}%`;
}

export function playerPieceStyle(player: PlayerSetup, type: "courtier" | "gardener" | "warrior", index: number) {
  const rowIndex = type === "courtier" ? 0 : type === "gardener" ? 1 : 2;
  const height = type === "warrior" ? 40 : type === "gardener" ? 35.23636 : 34.71579;
  const y = 78 + rowIndex * 76 + (68 - height) / 2;
  const x = 341 + index * 36;
  return placementStyle({ id: `${player.id}-${type}-${index}`, file: `/twc-${type === "courtier" ? "courtiers" : type === "gardener" ? "gardeners" : "warriors"}.png`, columns: 4, rows: 1, column: player.pieceColumn, row: 0, x, y, width: 34, height, label: `${player.name}${type}`, kind: "family-piece", z: 6 }, PLAYER_BOARD);
}

export function dieTileFaceStyle(tile: DieTileSetup, face: DieTileFace) {
  const type = face === "color" ? tile.color : tile.reward;
  const [column, row] = dieTileIndex[type];
  const positionX = (column / 4) * 100;
  const positionY = row * 100;
  return `background-image:url('/twc-die-tiles.png');background-size:500% 200%;background-position:${positionX}% ${positionY}%`;
}

export function dieTilePositionStyle(tile: DieTileSetup) {
  return `left:${(tile.x / PUBLIC_BOARD.width) * 100}%;top:${(tile.y / PUBLIC_BOARD.height) * 100}%;width:${(20 / PUBLIC_BOARD.width) * 100}%;height:${(20 / PUBLIC_BOARD.height) * 100}%`;
}

export function resourceMarkerStyle(type: "food" | "iron" | "pearl", value: number) {
  const x = type === "food" ? 38 : type === "iron" ? 99.5 : 161;
  const bottom = [6, 38, 71, 102, 135, 167, 200, 232][value];
  const y = 125 + 268 - bottom - 27;
  return `left:${(x / PLAYER_BOARD.width) * 100}%;top:${(y / PLAYER_BOARD.height) * 100}%;width:${(27 / PLAYER_BOARD.width) * 100}%;height:${(27 / PLAYER_BOARD.height) * 100}%`;
}

export const SEAL_LAYOUT = { x: 9, y: 5, gap: 72, size: 67 } as const;

/** Places owned seals over the five printed family-crest circles. */
export function sealMarkerStyle(index: number) {
  if (!Number.isInteger(index) || index < 0 || index >= 5) throw new Error(`Unknown seal slot ${index}.`);
  const x = SEAL_LAYOUT.x + index * SEAL_LAYOUT.gap;
  return `left:${(x / PLAYER_BOARD.width) * 100}%;top:${(SEAL_LAYOUT.y / PLAYER_BOARD.height) * 100}%;width:${(SEAL_LAYOUT.size / PLAYER_BOARD.width) * 100}%;height:${(SEAL_LAYOUT.size / PLAYER_BOARD.height) * 100}%`;
}

export function startingCardStyle(kind: "action" | "resource", card: number) {
  return materialAssetCss(`starting-${kind}-card-${card}`);
}
