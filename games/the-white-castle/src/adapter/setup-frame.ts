import { availableCastleTileActions, DECREE_CARDS, domainRowRewardSources, STARTING_ACTION_CARDS, STARTING_RESOURCE_CARDS, getLegalActions, placementReferenceValue, type GameState } from "../core";

function setupPublicBoardLines(state:GameState, seat:number, effects:(value:unknown) => string):string[] {
  const lightRows = new Set(availableCastleTileActions(state, "light").map(row => `${row.room}/${row.rowId}`));
  const describe = (value:unknown):string => {
    if (!Array.isArray(value)) return effects(value);
    return value.map(item => item.type === "chooseOne"
      ? `choose one: ${item.options.map((option:unknown, index:number) => `${index}=[${describe(option)}]`).join(" OR ")}`
      : effects([item])).join(", then ");
  };
  return [
    `After all setup choices, play order=${state.turnOrder.map(id => `P${id}`).join("→")}. The following public board supports planning; die placements are future actions, outside this setup chain.`,
    `Bridge dice, left to right: ${Object.entries(state.bridges).map(([color, dice]) => `${color}=[${dice.map(die => die.value).join(",")}]`).join("; ")}. Left endpoint triggers Lantern after placement payment; right endpoint does not. Middle dice become endpoints as dice are removed.`,
    "Placement coin change = die value - workspace reference value. Pay a negative difference before rewards. Later dice availability depends on earlier players' choices.",
    "Future action costs below are costs at execution, not a verdict on an unchosen offer. Count the selected offer, placement coins, the chosen personal row's queue, and any earlier rewards before paying a later action cost. Seal exchange: 1 seal→1 coin; 2 seals→1 food/iron/pearl; repeatable, no die action. Exchange only resources actually held at that point.",
    ...Object.values(state.workspaces).filter(workspace => workspace.active && workspace.owner === undefined).flatMap(workspace => {
      const room = state.board.castleRooms.find(candidate => `castle-${candidate.id}` === workspace.id);
      return workspace.allowedColors.map(color => {
        const groups = workspace.effectGroupsByColor?.[color];
        const reward = groups?.length ? groups.map(group => {
          const row = room?.slots.findIndex(slot => slot.rowId === group.id) ?? -1;
          return `${room ? `Row ${row + 1}${lightRows.has(`${room.id}/${group.id}`) ? " (light)" : ""}` : group.id}=[${describe(group.effects)}]`;
        }).join("; ") : describe(workspace.effectsByColor?.[color] ?? workspace.effects);
        return `${workspace.id}: reference=${placementReferenceValue(workspace)}; capacity=${workspace.capacity}; die=${color}; ${groups && groups.length > 1 ? "resolve all listed rows, choose order" : "reward"}: ${reward}.`;
      });
    }),
    ...(["courtier", "gardener", "warrior"] as const).map(row => {
      const workspace = state.workspaces[`p${seat}-domain-${row}`];
      const sources = domainRowRewardSources(state, seat, row);
      return `personal ${row}: reference=${placementReferenceValue(workspace)}; die=${workspace.allowedColors.join("/")}; queue=[${effects(sources.queue)}], then the action from the selected starting card. The row name does not determine that card's action.`;
    }),
    ...state.trainingYards.map(yard => `${yard.id}: warriorValue=${yard.warriorValue}; capacity=${yard.capacity}.`),
    ...state.players.filter(player => player.id !== seat && state.startingOffers.some(offer => offer.claimedBy === player.id)).map(player =>
      `P${player.id} setup resources now: ${Object.entries(player.resources).map(([key, value]) => `${key}=${value}`).join(", ")}.`),
  ];
}

// The engine owns the pair, pending quantity and phase transition. This projection
// only explains those choices; it neither ranks offers nor fills in resources.
export function buildSetupFrame(state:GameState, seat:number, effects:(value:unknown) => string) {
  const player = state.players[seat];
  const selectingPair = getLegalActions(state).some(action => action.type === "chooseStartingPair");
  const selectedOffer = state.startingOffers.findIndex(offer => offer.claimedBy === seat);
  const pending = state.pendingEffects[0]?.effect;
  const remaining = pending?.type === "gainChoice" ? pending.amount : 0;
  const resourceOptions = pending?.type === "gainChoice" ? pending.resources : ["food", "iron", "pearl"];
  const hasNext = state.setupIndex + 1 < state.setupOrder.length;
  const nextSeat = hasNext ? state.setupOrder[state.setupIndex + 1] : state.turnOrder[0];
  const linesFact = (kind:string, id:string, title:string, lines:string[]) => ({ kind, id, title, data:{ lines } });
  const turnLines = [
    `phase=setup；当前由 P${seat}（${player.name}）完成开局选择。`,
    selectingPair
      ? "游戏尚未正式开始。请先基于当前公共局面，分析一条简要的起步路线，再据此选择合适的初始套组及任选资源。"
      : "游戏尚未正式开始。请基于当前局面和已经选定的套组，补完剩余任选资源。",
    `开局顺序=${state.setupOrder.map(id => `P${id}`).join("→")}；正在进行第 ${state.setupIndex + 1}/${state.setupOrder.length} 位玩家的选择，每位玩家只选一组。`,
    selectingPair
      ? "当前步骤：选择一组尚未领取的组合，并决定该组全部任选资源。组内两张卡固定配对，不能跨组搭配。"
      : `当前步骤：已选择 offer=${selectedOffer}；剩余任选资源=${remaining}。固定资源已经到账，卡牌已经装入个人版图，只补选剩余资源，不再选组或重复领取。`,
    `完成当前玩家全部选择后：${hasNext ? `仍为 setup，由 P${nextSeat} 选组` : `所有玩家开局完成，进入普通回合，由 P${nextSeat} 首先取骰`}。`,
  ];
  const offers = state.startingOffers.flatMap((offer, index) => {
    // A restored partial opening shows the chosen pair only. Unclaimed pairs
    // belong to later players and must not look selectable by this player.
    if (!selectingPair && index !== selectedOffer) return [];
    const resource = STARTING_RESOURCE_CARDS.find(card => card.id === offer.resourceCard)!;
    const action = STARTING_ACTION_CARDS.find(card => card.id === offer.actionCard)!;
    const count = resource.choiceResources ?? 0;
    const family = action.effect.type === "majorAction" ? action.effect.action : "none";
    const familyName = { courtier:"家臣", gardener:"园丁", warrior:"武士", none:"无" }[family];
    return [linesFact("CurrentTargetFact", `setup-offer-${index}`, `开局组合 offer=${index}`, [
      `状态：${offer.claimedBy === null ? "尚未领取，可选择" : `已由 P${offer.claimedBy} 领取，不可重选`}。`,
      `固定配对：资源卡 ${resource.materialId} ＋ 行动卡 ${action.materialId}。`,
      `${selectingPair ? "选择后立即获得" : "已领取的固定资源（计入当前资源）"}：${Object.entries(resource.resources).map(([key, value]) => `${key}+${value}`).join("，")}。`,
      `${selectingPair ? "需要同时选定" : "此组原本附带"}：${count} 个任选资源${count ? "；每个从 food／iron／pearl 中选一种，允许重复" : "，无需附加资源选择"}。`,
      `装入灯笼区：${effects(resource.lantern)}${resource.decree ? `；敕令：${effects(DECREE_CARDS[resource.decree])}` : ""}。以后触发灯笼时结算，开局不获得这些效果。`,
      `装入个人行动区：一整张${familyName}行动卡，覆盖 ${action.domainRows.join("／")} 三条队列行。这三个名称是位置，不是行动种类。以后激活任一所选队列行时，获得该行已露出的队列奖励，再执行一次${familyName}行动；开局不执行成员行动。`,
      ...(selectingPair && offer.claimedBy === null ? [
        `提交组成：choose_starting_pair(offer=${index})${count ? `，随后恰好 ${count} 个 choose_reward(kind=starting_resource, choice=所选资源编号)` : "；没有后续资源选择"}。`,
      ] : []),
    ])];
  });
  const commitLines = [
    "当前行动链只写套组和资源选择；设想的后续回合用于判断，不写入本次选择链。",
    selectingPair
      ? "每条 chains 的 actions 先写所选组的 choose_starting_pair，再依次写该组要求的全部资源选择。offer 是选组参数，不是 commit.id。"
      : `每条 chains 的 actions 恰好补完剩余 ${remaining} 个 choose_reward；不要再写 choose_starting_pair。`,
    `每个任选资源写一个 choose_reward：kind=starting_resource；${resourceOptions.map((resource, index) => `choice=${index} 表示 ${resource}+1`).join("，")}；允许多次选择同一种。`,
  ];
  const facts = [
    linesFact("TurnFact", "setup-turn", "开局阶段", turnLines),
    linesFact("CurrentTargetFact", "setup-public-board", "开局公共局面", setupPublicBoardLines(state, seat, effects)),
    ...offers,
    linesFact("AuthorityBoundaryFact", "setup-commit", "当前选择参数", commitLines),
  ];
  // Other legal player actions (for example seal exchanges on a restored
  // resource choice) are still available, without duplicating the card catalog.
  const exchanges = getLegalActions(state).filter(action => action.type === "exchangeSeal");
  if (exchanges.length) commitLines.splice(3, 0,
    `当前也可选择家纹兑换：${exchanges.map(action => `exchange_seal(receive=${action.receive})`).join("；")}；仅在需要兑换时写入，不代替必选资源。`);
  return {
    modelFacts:{ version:1, coverage:"complete-current-decision", phaseScope:"setup", facts },
    narrativeSections:[
      { id:"setup-turn", title:"开局阶段", lines:turnLines },
      { id:"setup-public-board", title:"开局公共局面", lines:facts[1].data.lines },
      ...offers.map(fact => ({ id:fact.id, title:fact.title, lines:fact.data.lines })),
      { id:"setup-commit", title:"当前选择参数", lines:commitLines },
    ],
  };
}
