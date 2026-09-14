import {
  applyAction,
  domainRowEffects,
  INFLUENCE_CHECKPOINTS,
  type Effect,
  type GameAction,
  type GameState,
} from "../core";
import { scoreGame } from "../core/scoring";
import { describeCastleRowSelection } from "./effect-labels";

const clone = <T>(value:T):T => structuredClone(value);

type ArrivalEffect = { source:string; effect:Effect };

export function describeScoringEffects(value:Effect | ArrivalEffect | Array<Effect | ArrivalEffect>):string {
  if (Array.isArray(value)) {
    return value.map(describeScoringEffects).filter(Boolean).join(" -> ");
  }
  if ("effect" in value) return describeScoringEffects(value.effect);
  const item = value;
  if (item.type === "gain") return `gain ${item.amount} ${item.resource}`;
  if (item.type === "gainPoints") return `gain ${item.amount} points`;
  if (item.type === "influence") return `gain ${item.amount} influence`;
  if (item.type === "lantern") return "resolve current lantern rewards";
  if (item.type === "majorAction") return `start ${item.action} action`;
  if (item.type === "wellAction") return "resolve current well rewards";
  if (item.type === "domainAction") return "choose one available personal-board row";
  if (item.type === "castleTileAction") return describeCastleRowSelection(item.color);
  if (item.type === "pay") {
    const nested = describeScoringEffects(item.effects);
    return `${item.optional ? "optional " : ""}pay ${item.amount} ${item.resource}${nested ? ` to ${nested}` : ""}`;
  }
  if (item.type === "chooseOne") {
    return `choose exactly one: ${item.options.map((effects) => `[${describeScoringEffects(effects)}]`).join(" OR ")}`;
  }
  if (item.type === "actionOrder") {
    return `perform every reward group once; choose group order: ${item.groups.map((group) => `${group.id}{${describeScoringEffects(group.effects)}}`).join("; ")}`;
  }
  if (item.type === "effectOrder") {
    return `perform every effect once; choose order: ${item.effects.map((effect) => `[${describeScoringEffects(effect)}]`).join("; ")}`;
  }
  if (item.type === "gainChoice") {
    return `choose ${item.amount} resources from ${JSON.stringify(item.resources)}; repeats allowed`;
  }
  if (item.type === "daimyoReward") return `choose one open daimyo reward from positions ${JSON.stringify(item.positions)}`;
  if (item.type === "gardenActivation") return `choose activation order for gardens ${JSON.stringify(item.gardens)}`;
  if (item.type === "castleRefresh") return `refresh ${item.room}`;
  const unsupported:never = item;
  throw new Error(`Unsupported scoring effect: ${JSON.stringify(unsupported)}`);
}

const describeEffects = describeScoringEffects;

const courtierScore = (location:string):number => (
  location === "gate" ? 1
    : location.startsWith("steward-") ? 3
      : location.startsWith("diplomat-") ? 6
        : location === "daimyo" ? 10 : 0
);

const courtierFloor = (location:string):number => (
  location === "gate" ? 0
    : location.startsWith("steward-") ? 1
      : location.startsWith("diplomat-") ? 2
        : location === "daimyo" ? 3 : -1
);

export type CourtierPromotionSource = {
  member:string;
  location:string;
  targets:Array<{
    id:string;
    levels:1|2;
    rawCost:{pearl:number};
    effectiveCost:{pearl:number};
    spendable:{pearl:number};
    remainingGap:{pearl:number};
    affordableNow:boolean;
    scoreBefore:number;
    scoreAfter:number;
    publicArrivalEffects:ArrivalEffect[];
  }>;
};

function promotionArrivalEffects(
  state:GameState,
  seat:number,
  member:string,
  target:string,
  levels:1|2,
):ArrivalEffect[] {
  let projected = clone(state);
  projected.currentPlayer = seat;
  projected.phase = "resolve";
  projected.draftedDie = undefined;
  projected.actionFlow = undefined;
  projected.players[seat].resources.pearl = 7;
  projected.pendingEffects = [{
    id:"scoring-frame-courtier",
    source:"scoring-frame",
    effect:{ type:"majorAction", action:"courtier" },
  }];
  for (const action of [
    { type:"beginMajorAction", mode:"promote" },
    { type:"selectMajorActionSource", member },
    { type:"selectMajorActionTarget", target, levels },
    { type:"confirmMajorAction" },
  ] as GameAction[]) {
    projected = applyAction(projected, action).state;
  }
  return projected.pendingEffects
    .filter((pending) => pending.id !== "scoring-frame-courtier")
    .map((pending) => ({
      source:pending.source,
      effect:clone(pending.effect),
    }));
}

export function buildCourtierPromotionSources(
  state:GameState,
  seat:number,
):CourtierPromotionSource[] {
  const player = state.players[seat];
  return player.members
    .filter((member) => (
      member.type === "courtier"
      && courtierFloor(member.location) >= 0
      && courtierFloor(member.location) < 3
    ))
    .map((member) => {
      const floor = courtierFloor(member.location);
      const targets = ([1, 2] as const).flatMap((levels) => {
        const destinationFloor = floor + levels;
        if (destinationFloor > 3) return [];
        const cost = levels === 2 ? 5 : 2;
        const destinations = destinationFloor === 1
          ? state.board.castleRooms
            .filter((room) => room.floor === "steward")
            .map((room) => room.id)
          : destinationFloor === 2
            ? state.board.castleRooms
              .filter((room) => room.floor === "diplomat")
              .map((room) => room.id)
            : ["daimyo"];
        return destinations.map((target) => ({
          id:target,
          levels,
          rawCost:{ pearl:cost },
          effectiveCost:{ pearl:cost },
          spendable:{ pearl:player.resources.pearl },
          remainingGap:{ pearl:Math.max(0, cost - player.resources.pearl) },
          affordableNow:player.resources.pearl >= cost,
          scoreBefore:courtierScore(member.location),
          scoreAfter:courtierScore(target),
          publicArrivalEffects:promotionArrivalEffects(
            state, seat, member.id, target, levels,
          ),
        }));
      });
      return { member:member.id, location:member.location, targets };
    });
}

export function buildPostRecruitPromotionSource(
  state:GameState,
  seat:number,
):CourtierPromotionSource | null {
  const projected = clone(state);
  const recruited = projected.players[seat].members.find((member) => (
    member.type === "courtier" && member.location === "domain"
  ));
  if (!recruited) return null;
  recruited.location = "gate";
  return buildCourtierPromotionSources(projected, seat)
    .find((source) => source.member === recruited.id) ?? null;
}

export function buildScoringDecisionFacts(
  state:GameState,
  seat:number,
):Record<string, unknown> {
  const player = state.players[seat];
  const scores = scoreGame(state);
  const score = scores.find((item) => item.player === seat)!;
  const castleCourtiers = player.members.filter((member) => (
    member.type === "courtier"
    && (
      member.location.startsWith("steward-")
      || member.location.startsWith("diplomat-")
      || member.location === "daimyo"
    )
  )).length;
  const nextInfluenceCheckpoint = INFLUENCE_CHECKPOINTS.find((checkpoint) => (
    player.influence < checkpoint.position
  ));
  const scoreFacts = {
    currentTotal:score.total,
    components:[
      ["duringGame", "during-game points"],
      ["resources", "resource scoring"],
      ["timeTrack", "time-track scoring"],
      ["courtiers", "courtier scoring"],
      ["warriors", "warrior scoring"],
      ["gardeners", "gardener scoring"],
    ].map(([id, label]) => ({
      id, label,
      value:score[id as keyof Omit<typeof score, "player" | "total">],
    })),
    rules:[
      { ruleId:"courtier-location", timing:"game_end", formula:"gate=1; first_level=3; second_level=6; third_level=10", dependencies:["courtier.location"], workedExamples:["gate + second_level + third_level = 1 + 6 + 10 = 17"] },
      { ruleId:"warrior-yard", timing:"game_end", formula:"sum(ownedWarriorsInYard * yardValue * castleCourtiersExcludingGate)", dependencies:["trainingYard.warriors","trainingYard.warriorValue","courtier.location"], workedExamples:["2 * 2 * 3 = 12"] },
      { ruleId:"gardener-printed", timing:"game_end", formula:"sum(occupiedGarden.printedPoints)", dependencies:["garden.gardeners","garden.points"], workedExamples:["one gardener on a 4-point garden = 4"] },
      { ruleId:"resources", timing:"game_end", formula:"coins_plus_seals: each complete 5 = 1; food_iron_pearl: each 3..6 = 1, each 7 = 2", dependencies:["resources"], workedExamples:["7 iron = 2; 5 coins plus seals = 1"] },
      { ruleId:"time-track", timing:"game_end", formula:"influence position 0..5=0; 6..10=3; 11..14=6; 15..20=10..15 (position minus 5)", dependencies:["influence"], workedExamples:["8 influence = 3 points; 15 influence = 10 points"] },
    ],
  };
  const resourceTarget = (
    id:string,
    actionFamily:string,
    resource:keyof typeof player.resources,
    cost:number,
    points:number,
    ruleId:string,
    publicFollowUpEffects:Effect[] = [],
  ) => ({
    id, actionFamily, printedPoints:points,
    rawCost:{ [resource]:cost },
    effectiveCost:{ [resource]:cost },
    spendable:{ [resource]:player.resources[resource] },
    remainingGap:{ [resource]:Math.max(0, cost - player.resources[resource]) },
    affordableNow:player.resources[resource] >= cost,
    immediateScoreDelta:0,
    endNowScoreDelta:points,
    publicFollowUpEffects:publicFollowUpEffects.length
      ? [describeEffects(publicFollowUpEffects)]
      : [],
    scoringStructureChanges:[{ ruleId, before:0, after:points }],
  });
  const promotions = buildCourtierPromotionSources(state, seat);
  const postRecruitPromotion = buildPostRecruitPromotionSource(state, seat);
  const postRecruitPromotionTargets = postRecruitPromotion
    ? postRecruitPromotion.targets.map((target) => ({
      id:`recruit->${target.id}`,
      actionFamily:"recruit+promote",
      printedPoints:target.scoreAfter,
      rawCost:{ coins:2, pearl:target.rawCost.pearl },
      effectiveCost:{ coins:2, pearl:target.effectiveCost.pearl },
      spendable:{
        coins:player.resources.coins,
        pearl:player.resources.pearl,
      },
      remainingGap:{
        coins:Math.max(0, 2 - player.resources.coins),
        pearl:target.remainingGap.pearl,
      },
      affordableNow:player.resources.coins >= 2 && target.affordableNow,
      immediateScoreDelta:0,
      endNowScoreDelta:target.scoreAfter,
      publicFollowUpEffects:target.publicArrivalEffects.length
        ? [describeEffects(target.publicArrivalEffects)]
        : [],
      scoringStructureChanges:[{
        ruleId:"courtier-location",
        before:0,
        after:target.scoreAfter,
      }],
    }))
    : [];
  const legalTargets = [
    ...state.gardens
      .filter((garden) => !garden.gardeners.includes(seat))
      .map((garden) => resourceTarget(
        garden.id, "gardener", "food", garden.foodCost,
        garden.points, "gardener-printed", garden.effects,
      )),
    ...state.trainingYards
      .filter((yard) => (
        yard.capacity === "unlimited" || yard.warriors.length < yard.capacity
      ))
      .map((yard) => resourceTarget(
        yard.id, "warrior", "iron", yard.ironCost,
        yard.warriorValue * castleCourtiers, "warrior-yard", yard.effects,
      )),
    ...promotions.flatMap((source) => source.targets.map((target) => ({
      id:`${source.member}:${target.id}`,
      actionFamily:"promote",
      printedPoints:target.scoreAfter - target.scoreBefore,
      rawCost:target.rawCost,
      effectiveCost:target.effectiveCost,
      spendable:target.spendable,
      remainingGap:target.remainingGap,
      affordableNow:target.affordableNow,
      immediateScoreDelta:0,
      endNowScoreDelta:target.scoreAfter - target.scoreBefore,
      publicFollowUpEffects:target.publicArrivalEffects.length
        ? [describeEffects(target.publicArrivalEffects)]
        : [],
      scoringStructureChanges:[{
        ruleId:"courtier-location",
        before:target.scoreBefore,
        after:target.scoreAfter,
      }],
    }))),
    ...postRecruitPromotionTargets,
    ...(player.members.some((member) => (
      member.type === "courtier" && member.location === "domain"
    )) ? [resourceTarget(
      "gate", "recruit", "coins", 2, 1, "courtier-location",
    )] : []),
  ];
  const members = (type:"courtier"|"gardener"|"warrior") => player.members
    .filter((member) => member.type === type)
    .map((member) => ({
      id:member.id,
      location:member.location,
      ...(type === "courtier" ? { endGamePoints:courtierScore(member.location) } : {}),
    }));
  return {
    contractVersion:"scoring-decision-facts-v2",
    score:scoreFacts,
    endCondition:{
      description:"The game ends after round 3 and scores all end-game components.",
      remaining:{ rounds:Math.max(0, 4 - state.round) },
      tieBreakers:[],
    },
    scoringTargets:legalTargets,
    currentState:{
      resources:clone(player.resources),
      influence:player.influence,
      influenceCheckpoint:nextInfluenceCheckpoint ? {
        nextPosition:nextInfluenceCheckpoint.position,
        sealsCost:nextInfluenceCheckpoint.cost,
        sealsNow:player.resources.seals,
        affordableNow:player.resources.seals >= nextInfluenceCheckpoint.cost,
        choiceSemantics:{
          0:"pay seals and cross the checkpoint",
          1:"stop immediately before the checkpoint; gain no influence beyond it",
        },
      } : null,
      members:{
        courtiers:members("courtier"),
        gardeners:members("gardener"),
        warriors:members("warrior"),
      },
      remainingInDomain:{
        courtiers:members("courtier").filter((item) => item.location === "domain").length,
        gardeners:members("gardener").filter((item) => item.location === "domain").length,
        warriors:members("warrior").filter((item) => item.location === "domain").length,
      },
      warriorMultiplier:{ castleCourtiers },
      personalBoard:{
        lantern:describeEffects(player.lanternEffects),
        courtier:describeEffects(domainRowEffects(state, seat, "courtier")),
        gardener:describeEffects(domainRowEffects(state, seat, "gardener")),
        warrior:describeEffects(domainRowEffects(state, seat, "warrior")),
      },
    },
    opponents:scores
      .filter((item) => item.player !== seat)
      .map((item) => ({ seat:item.player, currentTotal:item.total })),
  };
}
