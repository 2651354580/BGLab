import materialManifest from "../../data/material.json";
import { CastleActionSelector, CappedResource, DieColor, MajorAction, Resource } from "./types";

export type MaterialEffect =
  | { type: "gain"; resource: Resource | "points" | "influence"; amount: number }
  | { type: "choiceResource"; amount: number }
  | { type: "majorAction"; action: MajorAction }
  | { type: "lantern" }
  | { type: "well" }
  | { type: "domain" }
  | { type: "castleTile"; color: CastleActionSelector }
  | { type: "pay"; resource: "coins" | "seals" | CappedResource; amount: number; then: MaterialEffect[] };

export interface CastleCardRow {
  id: string;
  position: "top" | "middle" | "bottom";
  tone: "dark" | "light";
  printed: string[];
  effects: MaterialEffect[];
}

export interface CastleCardData {
  id: number;
  materialId: string;
  floor: "steward" | "diplomat";
  rows: CastleCardRow[];
  excludedAtTwoPlayers?: boolean;
}

export interface DaimyoCardData { id: number; materialId: string; rewards: MaterialEffect[][]; }
export interface GardenCardData { id: number; materialId: string; kind: "plant" | "stone"; foodCost: number; points: number; action: MaterialEffect[]; }
export interface StartingResourceCardData {
  id: number;
  materialId: string;
  lantern: MaterialEffect[];
  resources: Partial<Record<Resource, number>>;
  choiceResources?: number;
  decree?: "coin" | "seal" | "points";
}

interface ManifestCard {
  id: string;
  numericId?: number;
  kind: string;
  excludedAtTwoPlayers?: boolean;
  rows?: CastleCardRow[];
  rewards?: MaterialEffect[][];
  foodCost?: number;
  points?: number;
  action?: MaterialEffect[];
  effect?: MaterialEffect;
  lantern?: MaterialEffect[];
  resources?: Partial<Record<Resource, number>>;
  choiceResources?: number;
  decree?: "coin" | "seal" | "points";
}

interface ManifestComponent {
  id: string;
  numericId?: number;
  type: string;
  effect?: MaterialEffect;
  action?: MaterialEffect;
  blueAction?: MaterialEffect;
  goldAction?: MaterialEffect;
}

interface MaterialManifest { schemaVersion: number; cards: ManifestCard[]; components: ManifestComponent[]; }

const manifest = materialManifest as unknown as MaterialManifest;
if (manifest.schemaVersion !== 1) throw new Error(`Unsupported White Castle material schema ${manifest.schemaVersion}.`);

function numericId(item: ManifestCard | ManifestComponent): number {
  if (item.numericId === undefined) throw new Error(`Material ${item.id} requires numericId.`);
  return item.numericId;
}

function castleCards(kind: CastleCardData["floor"]): CastleCardData[] {
  return manifest.cards.filter((card) => card.kind === kind).map((card) => ({
    id: numericId(card),
    materialId: card.id,
    floor: kind,
    rows: card.rows ?? [],
    excludedAtTwoPlayers: card.excludedAtTwoPlayers,
  }));
}

export function castleCardRows(card: CastleCardData): readonly (readonly MaterialEffect[])[] {
  return card.rows.map((row) => row.effects);
}

export function castleCardDomainRowGroups(card: CastleCardData): readonly (readonly MajorAction[])[] {
  if (card.floor === "steward") return [["courtier"], ["gardener"], ["warrior"]];
  return card.id <= 8
    ? [["courtier"], ["gardener", "warrior"]]
    : [["courtier", "gardener"], ["warrior"]];
}

export function castleCardRowData(card: CastleCardData): readonly CastleCardRow[] {
  return card.rows;
}

export function castleCardLightRows(card: CastleCardData): readonly (readonly MaterialEffect[])[] {
  return card.rows.filter((row) => row.tone === "light").map((row) => row.effects);
}

export function castleCardDarkRow(card: CastleCardData): readonly MaterialEffect[] {
  return card.rows.find((row) => row.tone === "dark")?.effects ?? [];
}

export const STEWARD_CARDS = castleCards("steward");
export const DIPLOMAT_CARDS = castleCards("diplomat");

export const DAIMYO_CARDS: DaimyoCardData[] = manifest.cards.filter((card) => card.kind === "daimyo").map((card) => ({
  id: numericId(card), materialId: card.id, rewards: card.rewards ?? [],
}));

export const GARDEN_CARDS: GardenCardData[] = manifest.cards.filter((card) => card.kind.startsWith("garden-")).map((card) => ({
  id: numericId(card),
  materialId: card.id,
  kind: card.kind.replace("garden-", "") as GardenCardData["kind"],
  foodCost: card.foodCost ?? 0,
  points: card.points ?? 0,
  action: card.action ?? [],
}));

export const STARTING_ACTION_CARDS = manifest.cards.filter((card) => card.kind === "starting-action").map((card) => ({
  id: numericId(card), materialId: card.id, effect: card.effect!,
}));

export const STARTING_RESOURCE_CARDS: StartingResourceCardData[] = manifest.cards.filter((card) => card.kind === "starting-resource").map((card) => ({
  id: numericId(card),
  materialId: card.id,
  lantern: card.lantern ?? [],
  resources: card.resources ?? {},
  choiceResources: card.choiceResources,
  decree: card.decree,
}));

function decreeRewards(decree: "coin" | "seal" | "points"): MaterialEffect[] {
  return manifest.cards.find((card) => card.kind === "decree" && card.decree === decree)?.rewards?.[0] ?? [];
}

export const DECREE_CARDS: Record<"coin" | "seal" | "points", MaterialEffect[]> = {
  coin: decreeRewards("coin"), seal: decreeRewards("seal"), points: decreeRewards("points"),
};

export const DIE_TILE_REWARDS: MaterialEffect[] = manifest.components.filter((component) => component.type === "die-tile-reward").map((component) => component.effect!);

export const YARD_TILES = manifest.components.filter((component) => component.type === "yard-tile").map((component) => ({
  id: numericId(component), materialId: component.id, blueAction: component.blueAction!, goldAction: component.goldAction!,
}));
