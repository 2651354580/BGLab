import { materialEffect, materialEffects } from "./effects";
import { GARDEN_CARDS, YARD_TILES } from "./material";
import { BoardState, DieColor, Effect, EffectGroup, GardenSpace, PlayerState, TrainingYard, Workspace } from "./types";

const allColors = ["black", "white", "coral"] as const;

export const STARTING_SETS = [
  { id: "starting-set-1", resources: { coins: 2, seals: 1, food: 2, iron: 1, pearl: 0 }, lantern: { type: "gain", resource: "pearl", amount: 1 } as Effect },
  { id: "starting-set-2", resources: { coins: 3, seals: 1, food: 1, iron: 2, pearl: 1 }, lantern: { type: "gain", resource: "iron", amount: 1 } as Effect },
  { id: "starting-set-3", resources: { coins: 2, seals: 1, food: 3, iron: 0, pearl: 1 }, lantern: { type: "gain", resource: "food", amount: 1 } as Effect },
] as const;

function workspace(input: Omit<Workspace, "dice">): Workspace {
  return { ...input, dice: [] };
}

export function createWorkspaces(players: PlayerState[], board: BoardState): Record<string, Workspace> {
  const result: Record<string, Workspace> = {
    well: workspace({ id: "well", kind: "well", label: "阿菊井", printedValue: 1, active: true, allowedColors: [...allColors], capacity: "unlimited", effects: [{ type: "gain", resource: "seals", amount: 1 }, ...board.wellTiles.map((tile) => tile.reward)] }),
    "outside-left": workspace({ id: "outside-left", kind: "outside", label: "墙外·左", printedValue: 5, active: true, allowedColors: [...allColors], capacity: players.length > 2 ? 2 : 1, effects: [{ type: "chooseOne", labels: ["园丁行动", "家臣行动"], options: [[{ type: "majorAction", action: "gardener" }], [{ type: "majorAction", action: "courtier" }]] }] }),
    "outside-right": workspace({ id: "outside-right", kind: "outside", label: "墙外·右", printedValue: 5, active: true, allowedColors: [...allColors], capacity: players.length > 2 ? 2 : 1, effects: [{ type: "chooseOne", labels: ["家臣行动", "武士行动"], options: [[{ type: "majorAction", action: "courtier" }], [{ type: "majorAction", action: "warrior" }]] }] }),
  };
  for (const room of board.castleRooms) {
    const effectsByColor: Partial<Record<DieColor, Effect[]>> = {};
    const effectGroupsByColor: Partial<Record<DieColor, EffectGroup[]>> = {};
    for (const color of allColors) {
      const groups = room.slots.filter((slot) => slot.color === color).map((slot) => ({ id: slot.rowId, effects: [...slot.effects] }));
      effectGroupsByColor[color] = groups;
      effectsByColor[color] = groups.flatMap((group) => group.effects);
    }
    result[`castle-${room.id}`] = workspace({ id: `castle-${room.id}`, kind: "castle", label: room.id, printedValue: room.floor === "steward" ? 3 : 4, active: true, allowedColors: allColors.filter((color) => (effectGroupsByColor[color]?.length ?? 0) > 0), capacity: players.length > 2 ? 2 : 1, effects: [], effectsByColor, effectGroupsByColor });
  }
  for (const player of players) {
    result[`p${player.id}-domain-courtier`] = workspace({ id: `p${player.id}-domain-courtier`, kind: "domain", label: `${player.name}·家臣`, printedValue: 6, active: true, owner: player.id, allowedColors: ["coral"], capacity: 1, effects: [{ type: "gain", resource: "coins", amount: 2 }, { type: "influence", amount: 1 }] });
    result[`p${player.id}-domain-gardener`] = workspace({ id: `p${player.id}-domain-gardener`, kind: "domain", label: `${player.name}·园丁`, printedValue: 6, active: true, owner: player.id, allowedColors: ["black"], capacity: 1, effects: [{ type: "gain", resource: "food", amount: 2 }] });
    result[`p${player.id}-domain-warrior`] = workspace({ id: `p${player.id}-domain-warrior`, kind: "domain", label: `${player.name}·武士`, printedValue: 6, active: true, owner: player.id, allowedColors: ["white"], capacity: 1, effects: [{ type: "gain", resource: "iron", amount: 2 }] });
  }
  return result;
}

export function gardenCardIdsBySpace(board: Pick<BoardState, "gardenCardIds">): Record<string, number> {
  const [blackPlant, whitePlant, coralPlant, blackStone, whiteStone, coralStone] = board.gardenCardIds;
  return {
    "garden-black-1": blackPlant, "garden-black-2": blackStone,
    "garden-white-1": whitePlant, "garden-white-2": whiteStone,
    "garden-coral-1": coralPlant, "garden-coral-2": coralStone,
  };
}

export function createGardens(board: BoardState): GardenSpace[] {
  const cardsBySpace = gardenCardIdsBySpace(board);
  return ["black", "white", "coral"].flatMap((bridge) => [1, 2].map((side) => {
    const id = `garden-${bridge}-${side}`;
    const cardId = cardsBySpace[id];
    const card = GARDEN_CARDS.find((candidate) => candidate.id === cardId)!;
    return {
    id,
    bridge: bridge as GardenSpace["bridge"],
    foodCost: card.foodCost,
    points: card.points,
    effects: materialEffects(card.action),
    gardeners: [],
  }; }));
}

export function createTrainingYards(board: BoardState): TrainingYard[] {
  const tile = (id: number) => YARD_TILES.find((candidate) => candidate.id === id)!;
  const groups = [board.yardTileIds.slice(0, 2), board.yardTileIds.slice(2, 3), board.yardTileIds.slice(3, 4)];
  const faceGroups = [["blue", "gold"], ["blue"], ["gold"]] as const;
  return groups.map((tileIds, index) => {
    const tileFaces = [...faceGroups[index]];
    return {
      id: `yard-${index + 1}`,
      ironCost: [5, 3, 1][index],
      warriorValue: [2, 1, 1][index],
      capacity: "unlimited",
      effects: tileIds.map((id, tileIndex) => materialEffect(tileFaces[tileIndex] === "blue" ? tile(id).blueAction : tile(id).goldAction)),
      warriors: [], tileIds, tileFaces,
    };
  });
}
