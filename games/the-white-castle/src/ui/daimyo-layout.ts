import type { PlayerState } from "../core";
import type { BoardCard } from "./card-layout";

export interface DaimyoRewardRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface DaimyoClaimAssignment {
  memberId: string;
  playerId: number;
  position: number;
}

export function daimyoRewardRects(card: BoardCard): DaimyoRewardRect[] {
  const width = card.width / 3;
  return Array.from({ length: 3 }, (_, position) => ({
    x: card.x + width * position,
    y: card.y,
    width,
    height: card.height,
  }));
}

export function daimyoClaimPiecePosition(card: BoardCard, position: number): { x: number; y: number } | undefined {
  const rect = daimyoRewardRects(card)[position];
  if (!rect) return undefined;
  return {
    x: rect.x + (rect.width - 25) / 2,
    y: card.y + 27,
  };
}

export function assignDaimyoClaims(players: PlayerState[], taken: (number | null)[]): DaimyoClaimAssignment[] {
  const available = new Map(players.map((player) => [
    player.id,
    player.members
      .filter((member) => member.type === "courtier" && member.location === "daimyo")
      .sort((left, right) => left.id.localeCompare(right.id)),
  ]));
  const cursors = new Map<number, number>();
  return taken.flatMap((playerId, position): DaimyoClaimAssignment[] => {
    if (playerId === null) return [];
    const members = available.get(playerId) ?? [];
    const cursor = cursors.get(playerId) ?? 0;
    const member = members[cursor];
    if (!member) return [];
    cursors.set(playerId, cursor + 1);
    return [{ memberId: member.id, playerId, position }];
  });
}
