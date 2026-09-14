import { atlasRecord } from "./material-assets";

const BOARD_WIDTH = 900;
const BOARD_HEIGHT = 460;

export type CardAtlas = "steward" | "diplomat" | "daimyo" | "garden" | "backs";

export interface BoardCard {
  id: string;
  atlas: CardAtlas;
  card: number;
  x: number;
  y: number;
  width: number;
  height: number;
  label: string;
}

export interface CardRowRect { x: number; y: number; width: number; height: number }

export function lightCardRowIndex(tones: readonly ("dark" | "light")[], option: number): number {
  const lightRows = tones.flatMap((tone, index) => tone === "light" ? [index] : []);
  return lightRows[option] ?? -1;
}

export function cardRowRects(card: Pick<BoardCard, "x" | "y" | "width" | "height">, spans: readonly number[]): CardRowRect[] {
  if (spans.length === 0 || spans.some((span) => !Number.isFinite(span) || span <= 0)) throw new Error("Castle card row spans must be positive.");
  const total = spans.reduce((sum, span) => sum + span, 0);
  let offset = 0;
  return spans.map((span) => {
    const height = card.height * span / total;
    const rect = { x: card.x, y: card.y + offset, width: card.width, height };
    offset += height;
    return rect;
  });
}

interface AtlasDefinition {
  file: string;
  columns: number;
  rows: number;
}

function cardAtlas(id: string): AtlasDefinition {
  const { file, columns, rows } = atlasRecord(id);
  return { file: `/${file}`, columns, rows };
}

export const CARD_ATLASES: Record<CardAtlas, AtlasDefinition> = {
  steward: cardAtlas("steward-cards"),
  diplomat: cardAtlas("diplomat-cards"),
  daimyo: cardAtlas("daimyo-cards"),
  garden: cardAtlas("garden-cards"),
  backs: { file: "/twc-card-backs.png", columns: 9, rows: 1 },
};

/**
 * Coordinates use the 900 x 460 board coordinate
 * system, then converted to percentages at render time. This keeps every card
 * attached to its printed board slot at any responsive scale.
 */
export const BOARD_CARDS: BoardCard[] = [
  { id: "daimyo", atlas: "daimyo", card: 1, x: 412, y: 105, width: 93, height: 60.10834, label: "大名卡" },

  { id: "diplomat-deck", atlas: "backs", card: 1, x: 274, y: 195, width: 60.10834, height: 93, label: "武士卡牌堆" },
  { id: "diplomat-1", atlas: "diplomat", card: 1, x: 421, y: 193, width: 60.10834, height: 93, label: "武士卡 1" },
  { id: "diplomat-2", atlas: "diplomat", card: 2, x: 618, y: 193, width: 60.10834, height: 93, label: "武士卡 2" },

  { id: "steward-deck", atlas: "backs", card: 2, x: 234, y: 292, width: 60.10834, height: 93, label: "家臣卡牌堆" },
  { id: "steward-1", atlas: "steward", card: 1, x: 382, y: 290, width: 60.10834, height: 93, label: "家臣卡 1" },
  { id: "steward-2", atlas: "steward", card: 2, x: 579, y: 290, width: 60.10834, height: 93, label: "家臣卡 2" },
  { id: "steward-3", atlas: "steward", card: 3, x: 777, y: 290, width: 60.10834, height: 93, label: "家臣卡 3" },

  { id: "garden-red-rock", atlas: "garden", card: 1, x: 3, y: 131.5, width: 93, height: 60.10834, label: "红桥石庭卡" },
  { id: "garden-red-plant", atlas: "garden", card: 2, x: 102, y: 131.5, width: 93, height: 60.10834, label: "红桥花庭卡" },
  { id: "garden-black-rock", atlas: "garden", card: 1, x: 3, y: 260, width: 93, height: 60.10834, label: "黑桥石庭卡" },
  { id: "garden-black-plant", atlas: "garden", card: 2, x: 102, y: 260, width: 93, height: 60.10834, label: "黑桥花庭卡" },
  { id: "garden-white-rock", atlas: "garden", card: 1, x: 3, y: 389, width: 93, height: 60.10834, label: "白桥石庭卡" },
  { id: "garden-white-plant", atlas: "garden", card: 2, x: 102, y: 389, width: 93, height: 60.10834, label: "白桥花庭卡" },
];

function percentage(value: number, total: number) {
  return `${(value / total) * 100}%`;
}

export function boardCardStyle(card: BoardCard) {
  const atlas = CARD_ATLASES[card.atlas];
  const index = card.card - 1;
  const column = index % atlas.columns;
  const row = Math.floor(index / atlas.columns);
  const positionX = atlas.columns === 1 ? 0 : (column / (atlas.columns - 1)) * 100;
  const positionY = atlas.rows === 1 ? 0 : (row / (atlas.rows - 1)) * 100;

  return [
    `left:${percentage(card.x, BOARD_WIDTH)}`,
    `top:${percentage(card.y, BOARD_HEIGHT)}`,
    `width:${percentage(card.width, BOARD_WIDTH)}`,
    `height:${percentage(card.height, BOARD_HEIGHT)}`,
    `background-image:url('${atlas.file}')`,
    `background-size:${atlas.columns * 100}% ${atlas.rows * 100}%`,
    `background-position:${positionX}% ${positionY}%`,
  ].join(";");
}
