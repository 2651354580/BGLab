export type OpportunityPlacementDie = {
  side:string;
  value:number;
  coinChange:number;
  sealsToCoins:number;
  coinsAfter:number;
  memberActionsOpened:string[];
};

export type OpportunityPlacementOption = {
  workspace:string;
  dieColor:string;
  openingReward:string;
  dice:OpportunityPlacementDie[];
};

export type OpportunityMemberAction = {
  family:string;
  data:Record<string, unknown>;
};

export type OpportunityFrameInput = {
  placementOptions:OpportunityPlacementOption[];
  memberActions:OpportunityMemberAction[];
  scoreContext:Record<string, unknown>;
  endCondition:Record<string, unknown>;
  opponents:Array<Record<string, unknown>>;
};

export type OpportunityEntry = {
  entry:string;
  coinChange:number;
  coinsAfter:number;
  sealsToCoins?:number;
  openingReward:string;
};

const entryFor = (
  option:OpportunityPlacementOption,
  die:OpportunityPlacementDie,
):OpportunityEntry => ({
  entry:`${option.dieColor}:${die.side}→${option.workspace}`,
  coinChange:die.coinChange,
  coinsAfter:die.coinsAfter,
  ...(die.sealsToCoins ? { sealsToCoins:die.sealsToCoins } : {}),
  openingReward:option.openingReward,
});

export function buildOpportunityFrameData(input:OpportunityFrameInput):{
  scoring:Record<string, unknown>;
  opportunities:Record<string, unknown>;
  otherPlacements:Record<string, unknown>;
} {
  const memberActions = input.memberActions.map(({ family, data }) => {
    const seen = new Set<string>();
    const entries = input.placementOptions.flatMap((option) => (
      option.dice.flatMap((die) => {
        if (!die.memberActionsOpened.includes(family)) return [];
        const entry = entryFor(option, die);
        if (seen.has(entry.entry)) return [];
        seen.add(entry.entry);
        return [entry];
      })
    ));
    return { ...data, entries };
  }).filter((action) => action.entries.length > 0);

  const otherPlacements = input.placementOptions.flatMap((option) => (
    option.dice.flatMap((die) => (
      die.memberActionsOpened.length === 0 ? [entryFor(option, die)] : []
    ))
  ));

  return {
    scoring:{
      scoreContext:input.scoreContext,
      endCondition:input.endCondition,
      opponents:input.opponents,
    },
    opportunities:{
      meaning:(
        "entries为原子骰子→位置入口；entry、成员、目标必须取自同一成员行动对象，"
        + "不得跨对象拼接；精确值由Check验证。"
      ),
      memberActions,
    },
    otherPlacements:{
      meaning:(
        "这些放置不开启成员行动；只比较钱币与奖励。"
      ),
      placements:otherPlacements,
    },
  };
}
