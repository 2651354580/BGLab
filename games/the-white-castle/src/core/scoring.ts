import { GameState, PlayerState, ScoreBreakdown } from "./types";

import { scoreTimeTrack } from "./influence-track";

export function scoreResources(player: PlayerState): number {
  const { coins, seals, food, iron, pearl } = player.resources;
  const stock = [food, iron, pearl].reduce((total, amount) => total + (amount === 7 ? 2 : amount >= 3 ? 1 : 0), 0);
  return Math.floor((coins + seals) / 5) + stock;
}

function scoreCourtiers(player: PlayerState): number {
  return player.members.filter((member) => member.type === "courtier").reduce((total, member) => total + (member.location === "gate" ? 1 : member.location.startsWith("steward-") ? 3 : member.location.startsWith("diplomat-") ? 6 : member.location === "daimyo" ? 10 : 0), 0);
}

export function scoreGame(state: GameState): ScoreBreakdown[] {
  return state.players.map((player) => {
    const duringGame = player.points;
    const resources = scoreResources(player);
    const timeTrack = scoreTimeTrack(player.influence);
    const courtiers = scoreCourtiers(player);
    const castleCourtiers = player.members.filter((member) => member.type === "courtier" && (member.location.startsWith("steward-") || member.location.startsWith("diplomat-") || member.location === "daimyo")).length;
    const warriors = state.trainingYards.reduce((total, yard) => total + yard.warriors.filter((owner) => owner === player.id).length * yard.warriorValue * castleCourtiers, 0);
    const gardeners = state.gardens.reduce((total, garden) => total + (garden.gardeners.includes(player.id) ? garden.points : 0), 0);
    return { player: player.id, duringGame, resources, timeTrack, courtiers, warriors, gardeners, total: duringGame + resources + timeTrack + courtiers + warriors + gardeners };
  });
}

export function determineWinner(state: GameState, scores: ScoreBreakdown[]): number {
  return [...scores].sort((a, b) => b.total - a.total || state.turnOrder.indexOf(a.player) - state.turnOrder.indexOf(b.player))[0].player;
}
