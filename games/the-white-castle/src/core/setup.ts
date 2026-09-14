import { materialEffects } from "./effects";
import { castleCardDarkRow, castleCardRowData, CastleCardData, CastleCardRow, DAIMYO_CARDS, DIE_TILE_REWARDS, DIPLOMAT_CARDS, GARDEN_CARDS, MaterialEffect, STEWARD_CARDS, YARD_TILES } from "./material";
import { BoardState, CastleRoomState, DieColor, PlayerCount } from "./types";

function nextRandom(seed: number): [number, number] {
  const next = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
  return [next / 0x100000000, next];
}

export function seededShuffle<T>(items: readonly T[], seed: number): T[] {
  const result = [...items];
  let cursor = seed >>> 0;
  for (let index = result.length - 1; index > 0; index -= 1) {
    const [value, next] = nextRandom(cursor);
    cursor = next;
    const target = Math.floor(value * (index + 1));
    [result[index], result[target]] = [result[target], result[index]];
  }
  return result;
}

function majorActionFamily(effects: readonly MaterialEffect[]): string {
  return effects.flatMap((effect): string[] => {
    if (effect.type === "majorAction") return [effect.action];
    if (effect.type === "pay") return [majorActionFamily(effect.then)];
    return [];
  }).filter(Boolean).join("+");
}

function visibleCastleCards(seed: number, playerCount: PlayerCount): { stewardCards: CastleCardData[]; diplomatCards: CastleCardData[] } {
  const eligibleStewards = STEWARD_CARDS.filter((card) => playerCount > 2 || !card.excludedAtTwoPlayers);
  const eligibleDiplomats = DIPLOMAT_CARDS.filter((card) => playerCount > 2 || !card.excludedAtTwoPlayers);
  for (let attempt = 0; attempt < 50; attempt += 1) {
    const stewardCards = seededShuffle(eligibleStewards, seed + 11 + attempt * 97);
    const diplomatCards = seededShuffle(eligibleDiplomats, seed + 23 + attempt * 97);
    const visible = [...stewardCards.slice(0, 3), ...diplomatCards.slice(0, 2)];
    const darkActions = visible.map((card) => majorActionFamily(castleCardDarkRow(card)));
    if (new Set(darkActions).size > 1) return { stewardCards, diplomatCards };
  }
  throw new Error("Could not create a varied castle-card setup.");
}

function makeRoom(id: string, floor: CastleRoomState["floor"], cardId: number, tiles: { color: DieColor; effect: ReturnType<typeof materialEffects>[number] }[], rows: readonly CastleCardRow[]): CastleRoomState {
  return { id, floor, cardId, slots: tiles.map((tile, index) => ({ color: tile.color, reward: tile.effect, rowId: rows[index].id, effects: materialEffects(rows[index].effects) })) };
}

export function createBoardSetup(seed: number, playerCount: PlayerCount = 2): BoardState {
  const { stewardCards, diplomatCards } = visibleCastleCards(seed, playerCount);
  const colors = ["black", "white", "coral"] as DieColor[];
  const tilePool = colors.flatMap((color) => DIE_TILE_REWARDS.map((effect) => ({ color, effect: materialEffects([effect])[0] })));
  const diamondTiles = colors.map((color, index) => seededShuffle(tilePool.filter((tile) => tile.color === color), seed + 701 + index * 13)[0]);
  const diamondKeys = new Set(diamondTiles.map((tile) => `${tile.color}-${JSON.stringify(tile.effect)}`));
  const remainder = tilePool.filter((tile) => !diamondKeys.has(`${tile.color}-${JSON.stringify(tile.effect)}`));
  let tiles = [...diamondTiles, ...remainder];
  let groups: typeof tiles[] = [];
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const shuffled = seededShuffle(remainder, seed + 1000 + attempt * 31);
    groups = [
      [diamondTiles[0], ...shuffled.slice(0, 2)],
      [diamondTiles[1], ...shuffled.slice(2, 4)],
      [diamondTiles[2], ...shuffled.slice(4, 6)],
      shuffled.slice(6, 8),
      shuffled.slice(8, 10),
    ];
    tiles = [...groups.flat(), ...shuffled.slice(10, 12)];
    if (groups.every((group) => new Set(group.map((tile) => tile.color)).size >= 2)) break;
  }
  if (groups.some((group) => new Set(group.map((tile) => tile.color)).size < 2)) throw new Error("Could not distribute die tiles with two colors per room.");
  const rooms = [
    ...stewardCards.slice(0, 3).map((card, index) => makeRoom(`steward-${index + 1}`, "steward", card.id, groups[index], castleCardRowData(card))),
    ...diplomatCards.slice(0, 2).map((card, index) => makeRoom(`diplomat-${index + 1}`, "diplomat", card.id, groups[index + 3], castleCardRowData(card))),
  ];
  const daimyo = seededShuffle(DAIMYO_CARDS, seed + 37)[0];
  const yards = seededShuffle(YARD_TILES, seed + 41).slice(0, 4);
  const gardens = [
    ...seededShuffle(GARDEN_CARDS.filter((card) => card.kind === "plant"), seed + 43).slice(0, 3),
    ...seededShuffle(GARDEN_CARDS.filter((card) => card.kind === "stone"), seed + 47).slice(0, 3),
  ];
  return {
    castleRooms: rooms,
    stewardDeck: stewardCards.slice(3).map((card) => card.id),
    diplomatDeck: diplomatCards.slice(2).map((card) => card.id),
    daimyoCard: daimyo.id,
    daimyoTaken: [null, null, null],
    wellTiles: tiles.slice(13, 15).map((tile) => ({ color: tile.color, reward: tile.effect })),
    yardTileIds: yards.map((yard) => yard.id),
    gardenCardIds: gardens.map((garden) => garden.id),
  };
}
