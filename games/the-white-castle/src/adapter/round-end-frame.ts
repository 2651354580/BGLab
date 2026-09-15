import {
  availableCastleTileActions,
  domainRowRewardSources,
  type Effect,
  type GameState,
} from "../core";
import { buildMinimalBoardFrame } from "./minimal-board-frame";

type Fact = {kind:string; id:string; title:string; data:{lines:string[]}};

/** Public destinations of nested garden rewards, including occupied die spaces. */
export function buildRoundEndBoardFacts(
  state:GameState,
  seat:number,
  describeEffects:(effects:Effect[])=>string,
):Fact[] {
  const normal=buildMinimalBoardFrame(state,seat,{
    actorTurnsRemaining:0,laterTurnsRemaining:0,endsRound:false,endsGame:false,nextActor:null,
  }) as {facts:Fact[]};
  const reusable=new Set(["personal","gardener-area","warrior-area","courtier-area","scoring"]);
  const facts=normal.facts.filter(fact=>reusable.has(fact.id));
  const gardens=facts.find(fact=>fact.id==="gardener-area")!;
  gardens.data.lines=gardens.data.lines
    .filter(line=>!line.startsWith("Round-end reactivation requires"))
    .map(line=>line.startsWith("Gardener:")
      ? `New gardener placements unlocked by rewards: pay the listed food once to place a new gardener. foodNow=${state.players[seat].resources.food}.`
      : line.startsWith("garden-")
        ? line.replace(/: (available|unavailable\(already yours\));/,": new-placement=$1;")
        : line);
  const lightRows=new Set(availableCastleTileActions(state,"light").map(row=>`${row.room}/${row.rowId}`));
  const rows=state.board.castleRooms.flatMap(room=>room.slots.map((slot,index)=>(
    `${room.id} Row ${index+1}; die-color=${slot.color}; tone=${lightRows.has(`${room.id}/${slot.rowId}`)?"light":"dark"}; reward=${describeEffects(slot.effects)}.`
  )));
  const personal=(["courtier","gardener","warrior"] as const).map(row=>{
    const sources=domainRowRewardSources(state,seat,row);
    return `personal row=${row}: revealed queue=[${describeEffects(sources.queue)}]; current action-card effects=[${describeEffects(sources.card)}].`;
  });
  facts.splice(1,0,{
    kind:"CurrentTargetFact",id:"round-end-public-rewards",title:"Reward destinations",
    data:{lines:[
      "Castle-row rewards select one matching die-color/light/any row below; no die is placed, and occupied die spaces do not block that reward.",
      ...rows,
      `Well reward=${describeEffects(Object.values(state.workspaces).find(workspace=>workspace.kind==="well")?.effects??[])}.`,
      "A personal-row reward activates its revealed queue rewards and current action-card effects in printed order. row=courtier/gardener/warrior identifies the member-reserve row; only the actual effects determine which member action, if any, is supplied.",
      ...personal,
      "Each personal row can be activated at most once in the current turn. Order unused rows courtier/gardener/warrior and renumber choice from 0 after each use. A new card changes subsequent row rewards.",
    ]},
  });
  return facts;
}
