import { PLAYER_BOARD } from "./setup-layout";

// The supplied atlas is 2334 × 402, containing nine portrait card backs.
export const LANTERN_CARD_RATIO = 402 / (2334 / 9);
// Icons finish before source y=100; the ribbon tail continues to about y=170.
// Expose only the icon and a small margin on each covered card.
export const LANTERN_REWARD_FRACTION = 0.26;
const CARD_HEIGHT = 130;
const CARD_WIDTH = CARD_HEIGHT * LANTERN_CARD_RATIO;
const SHELF_WIDTH = 382;
const STRIDE = CARD_WIDTH * LANTERN_REWARD_FRACTION;
const PER_ROW = Math.floor((SHELF_WIDTH - CARD_WIDTH) / STRIDE) + 1;

export function lanternLayout(count: number) {
  const rows = Math.max(1, Math.ceil(count / PER_ROW));
  const height = rows * CARD_HEIGHT + (rows - 1) * 12;
  return {
    x: 312, y: 315, width: SHELF_WIDTH, height,
    canvasHeight: Math.max(PLAYER_BOARD.height, 315 + height + 8),
    cards: Array.from({ length: count }, (_, index) => ({
      x: (index % PER_ROW) * STRIDE,
      y: Math.floor(index / PER_ROW) * (CARD_HEIGHT + 12),
      width: CARD_WIDTH, height: CARD_HEIGHT,
    })),
  };
}

export function lanternShelfStyle(count: number): string {
  const shelf = lanternLayout(count);
  return `left:${shelf.x / PLAYER_BOARD.width * 100}%;top:${shelf.y / shelf.canvasHeight * 100}%;width:${shelf.width / PLAYER_BOARD.width * 100}%;height:${shelf.height / shelf.canvasHeight * 100}%`;
}

export function lanternCardStyle(count: number, index: number): string {
  const shelf = lanternLayout(count), card = shelf.cards[index];
  return `left:${card.x / shelf.width * 100}%;top:${card.y / shelf.height * 100}%;width:${card.width / shelf.width * 100}%;height:${card.height / shelf.height * 100}%`;
}
