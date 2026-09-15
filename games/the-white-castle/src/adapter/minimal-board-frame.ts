import {
  availableCastleTileActions,
  castleCardDomainRowGroups,
  domainCardLanternEffects,
  domainRowRewardSources,
  getLegalActions,
  INFLUENCE_CHECKPOINTS,
  RESOURCE_CAPS,
  DAIMYO_CARDS,
  DIPLOMAT_CARDS,
  STEWARD_CARDS,
  materialEffects,
  placementReferenceValue,
  scoreGame,
  type GameState,
} from "../core";
import {
  buildCourtierPromotionSources,
  buildPostRecruitPromotionSource,
} from "./scoring-frame";
import { describeCastleRowSelection } from "./effect-labels";

type BoundaryInput = {
  actorTurnsRemaining:number;
  laterTurnsRemaining:number;
  endsRound:boolean;
  endsGame:boolean;
  nextActor:number | null;
};

const fact = (
  kind:string,
  id:string,
  title:string,
  data:Record<string, unknown>,
) => ({ kind, id, title, data });

const colorName = (color:string):string => color;

export function buildMinimalBoardFrame(
  state:GameState,
  seat:number,
  boundary:BoundaryInput,
):Record<string, unknown> {
  const player = state.players[seat];
  const render = (value:unknown):string => {
    if (Array.isArray(value)) return value
      .map((item) => render(item))
      .filter(Boolean).join(" → ") || "无";
    if (!value || typeof value !== "object") return String(value ?? "无");
    const item = value as Record<string, unknown>;
    if (item.effect) return render(item.effect);
    if (!item.type && Array.isArray(item.effects)) {
      return `${String(item.id ?? "组")}{${render(item.effects)}}`;
    }
    const type = String(item.type ?? "");
    if (type === "gain") return `gain ${String(item.amount)} ${String(item.resource)}`;
    if (type === "gainPoints") return `gain ${String(item.amount)} points`;
    if (type === "gainChoice") {
      const resources = Array.isArray(item.resources) ? item.resources : [];
      return `choose ${String(item.amount ?? 1)} resources; repeats allowed: ${resources.map((resource, choice) => (
        `Resource ${choice + 1}=${String(resource)}`
      )).join(", ")}`;
    }
    if (type === "influence") return `gain ${String(item.amount)} influence`;
    if (type === "lantern") return "Lantern reward (see Personal)";
    if (type === "wellAction") return "Well rewards listed under Actions";
    if (type === "majorAction") {
      return `${String(item.action)} action`;
    }
    if (type === "castleTileAction") return describeCastleRowSelection(String(item.color));
    if (type === "domainAction") return "choose one personal-board row";
    if (type === "castleRefresh") return `refresh ${String(item.room)}`;
    if (type === "pay") {
      const pay = `pay ${String(item.amount)} ${String(item.resource)}`;
      return item.optional
        ? `choose_reward kind=optional_payment: Reward 1 (choice=0)=${pay}, then ${render(item.effects)}; Reward 2 (choice=1)=skip only this optional reward; earlier actions and their end points still apply`
        : `${pay}→${render(item.effects)}`;
    }
    if (type === "chooseOne") {
      const options = Array.isArray(item.options) ? item.options : [];
      return `choose one action: ${options.map((option, choice) => (
        `Action ${choice + 1}=${render(option)}`
      )).join("; ")}`;
    }
    if (type === "effectOrder") {
      const effects = Array.isArray(item.effects) ? item.effects : [];
      return `perform all; choose order: ${effects.map((effect, choice) => (
        `Action ${choice + 1} first=${render(effect)}`
      )).join("; ")}`;
    }
    if (type === "actionOrder") {
      const groups = Array.isArray(item.groups) ? item.groups : [];
      return `perform all rewards; choose order: ${groups.map((group, choice) => (
        `Reward ${choice + 1} first=${render(group)}`
      )).join("; ")}`;
    }
    if (type === "daimyoReward") {
      const card = DAIMYO_CARDS.find((candidate) => candidate.id === state.board.daimyoCard);
      const positions = Array.isArray(item.positions) ? item.positions : [];
      return `choose one Daimyo reward: ${positions.map((position, index) => {
        const choice = Number(position);
        return `Reward ${index + 1}=${render(materialEffects(card?.rewards[choice] ?? []))}`;
      }).join("; ")}`;
    }
    if (type === "gardenActivation") {
      return `choose one garden ${JSON.stringify(item.gardens ?? [])}`;
    }
    return type || "无";
  };
  const compactEffect = (value:unknown):string => {
    if (Array.isArray(value)) return value
      .map((item) => compactEffect(item))
      .filter(Boolean).join(" + ") || "none";
    if (!value || typeof value !== "object") return String(value ?? "none");
    const item = value as Record<string, unknown>;
    if (item.effect) return compactEffect(item.effect);
    if (!item.type && Array.isArray(item.effects)) return compactEffect(item.effects);
    const type = String(item.type ?? "");
    if (type === "gain") return `+${String(item.amount)} ${String(item.resource)}`;
    if (type === "gainPoints") return `+${String(item.amount)} points`;
    if (type === "gainChoice") {
      const resources = Array.isArray(item.resources) ? item.resources : [];
      return `choose ${String(item.amount ?? 1)} from ${resources.join("/")} (repeat allowed)`;
    }
    if (type === "influence") return `+${String(item.amount)} influence`;
    if (type === "lantern") return "Lantern (see Personal)";
    if (type === "wellAction") return "current Well rewards";
    if (type === "majorAction") {
      return `${String(item.action)} action`;
    }
    if (type === "castleTileAction") return describeCastleRowSelection(String(item.color));
    if (type === "domainAction") return "choose personal row";
    if (type === "castleRefresh") return "";
    if (type === "pay") {
      const payment = `pay${String(item.amount)} ${String(item.resource)}`;
      const effects = compactEffect(item.effects);
      return item.optional
        ? `optional_payment[0=${payment}->${effects}|1=skip]`
        : `${payment}->${effects}`;
    }
    if (type === "chooseOne") {
      const options = Array.isArray(item.options) ? item.options : [];
      return options.map((option, choice) => (
        `${choice}=${compactEffect(option)}`
      )).join("|");
    }
    if (type === "effectOrder" || type === "actionOrder") {
      const effects = Array.isArray(item.effects)
        ? item.effects
        : Array.isArray(item.groups) ? item.groups : [];
      return `all;order[${effects.map((effect, choice) => (
        `${choice}=${compactEffect(effect)} first`
      )).join("|")}]`;
    }
    if (type === "daimyoReward") {
      const card = DAIMYO_CARDS.find((candidate) => candidate.id === state.board.daimyoCard);
      const positions = Array.isArray(item.positions) ? item.positions : [];
      return positions.map((position, choice) => (
        `${choice}=${compactEffect(materialEffects(card?.rewards[Number(position)] ?? []))}`
      )).join("|");
    }
    return render(item);
  };

  const memberLine = (type:"courtier"|"gardener"|"warrior") => {
    const groups = new Map<string, string[]>();
    for (const member of player.members.filter((candidate) => candidate.type === type)) {
      const ids = groups.get(member.location) ?? [];
      ids.push(member.id);
      groups.set(member.location, ids);
    }
    return [...groups].map(([location, ids]) => `${location}=${ids.length}`).join("; ");
  };
  const nextCheckpoints = INFLUENCE_CHECKPOINTS
    .filter((checkpoint) => checkpoint.position > player.influence)
    .slice(0, 2)
    .map((checkpoint) => (
      `${checkpoint.position}(pay ${checkpoint.cost} seals; `
      + `enough seals=${player.resources.seals >= checkpoint.cost ? "yes" : "no"})`
    ));
  // Lantern effects are player-ordered, unlike a card's printed sequence.
  const lantern = player.lanternEffects.map((effect) => render(effect)).join("; ") || "无";
  const castleCourtierCount = player.members.filter((member) => (
    member.type === "courtier"
    && (member.location.startsWith("steward-")
      || member.location.startsWith("diplomat-")
      || member.location === "daimyo")
  )).length;
  type SelectableDie = {
    end:"left"|"right";
    value:number;
  };
  const legalBridgeEnds = new Map<string, SelectableDie[]>();
  for (const action of getLegalActions(state)) {
    if (action.type !== "draftDie") continue;
    const dice = state.bridges[action.bridge];
    const die = action.end === "left" ? dice[0] : dice[dice.length - 1];
    if (!die) continue;
    const ends = legalBridgeEnds.get(action.bridge) ?? [];
    ends.push({
      end:action.end,
      value:die.value,
    });
    legalBridgeEnds.set(action.bridge, ends);
  }
  const bridgeLine = Object.entries(state.bridges).map(([color, dice]) => {
    if (!dice.length) return `${colorName(color)}: none`;
    if (dice.length === 1) {
      const end = legalBridgeEnds.get(color)?.[0]?.end;
      return `${colorName(color)}: only die=${dice[0].value}; `
        + (end
          ? `${end} selectable${end === "left" ? " (+Lantern)" : " (no Lantern)"}`
          : "not selectable now");
    }
    const middle = dice.slice(1, -1).map((die) => die.value);
    return `${colorName(color)}: left=${dice[0].value} (+Lantern), right=${dice[dice.length - 1].value} (no Lantern)`
      + `${middle.length ? `, middle=[${middle.join(",")}] unavailable` : ""}`;
  }).join("; ");

  const lightRows = new Set(
    availableCastleTileActions(state, "light")
      .map((row) => `${row.room}/${row.rowId}`),
  );
  const workspaceLines = (workspace:GameState["workspaces"][string]):string[] => {
    const domainRow = workspace.id === `p${seat}-domain-courtier` ? "courtier"
      : workspace.id === `p${seat}-domain-gardener` ? "gardener"
        : workspace.id === `p${seat}-domain-warrior` ? "warrior" : null;
    const rewardFor = (color:string):string => {
      if (domainRow) {
        const sources = domainRowRewardSources(state, seat, domainRow);
        return `all rewards resolve in printed order: revealed queue=[${render(sources.queue)}]; `
          + `current action-card effects=[${render(sources.card)}]`;
      }
      const groups = workspace.effectGroupsByColor?.[color as "black"|"white"|"coral"];
      if (groups?.length) {
        const roomId = workspace.id.replace(/^castle-/, "");
        const room = state.board.castleRooms.find((candidate) => candidate.id === roomId);
        const rewards = groups.map((group, index) => {
          const row = room?.slots.findIndex((slot) => slot.rowId === group.id) ?? -1;
          const fullId = `${roomId}/${group.id}`;
          const label = workspace.kind === "castle"
            ? `[Row ${row + 1}${lightRows.has(fullId) ? "; light" : ""}]`
            : `[${group.id}]`;
          return `Reward ${index + 1}${label}=${render(group.effects)}`;
        });
        return groups.length > 1
          ? `all rewards resolve; choose order: ${rewards.join("; ")}`
          : rewards[0];
      }
      const effects = workspace.effectsByColor?.[color as "black"|"white"|"coral"]
          ?? workspace.effects;
      return effects.length > 1
        ? `all rewards resolve: ${effects.map((effect, index) => (
          `Reward ${index + 1}=${render(effect)}`
        )).join("; ")}`
        : render(effects);
    };
    const printedColors = workspace.allowedColors.map((color) => colorName(color));
    const affordabilityFor = (color:string) => (
      (legalBridgeEnds.get(color) ?? []).map((die) => {
        // The core charges placement before it resolves the left-die Lantern.
        // Only resources already owned can fund placement or prior exchanges.
        const coinsBeforePlacement = player.resources.coins;
        const sealsBeforePlacement = player.resources.seals;
        const coinDelta = die.value - placementReferenceValue(workspace);
        const sealsToCoins = Math.max(0, -(coinsBeforePlacement + coinDelta));
        return {
          label:`${color}-${die.end}${die.value}`,
          affordable:sealsToCoins <= sealsBeforePlacement,
        };
      })
    );
    const placementsByColor = workspace.allowedColors.map((color) => ({
      color,
      placements:affordabilityFor(color),
    }));
    const reachablePlacementsByColor = placementsByColor.filter((item) => (
      item.placements.some((placement) => placement.affordable)
    ));
    const reachableColors = reachablePlacementsByColor.map((item) => item.color);
    const rewards = reachableColors.map((color) => ({ color, reward:rewardFor(color) }));
    const sameReward = rewards.length > 0 && rewards.every((item) => item.reward === rewards[0].reward);
    const full = workspace.capacity !== "unlimited" && workspace.dice.length >= workspace.capacity;
    const colors = rewards.map((item) => colorName(item.color)).join("/");
    const personalMatch = /^p[0-9]+-domain-(courtier|gardener|warrior)$/.exec(workspace.id);
    const castleMatch = /^castle-(steward|diplomat)-([0-9]+)$/.exec(workspace.id);
    const visibleName = personalMatch
      ? `personal row=${personalMatch[1]}`
      : castleMatch
        ? `${castleMatch[1]} room ${castleMatch[2]}`
        : workspace.id;
    const printedColorText = printedColors.join("/");
    const cannotPay = placementsByColor.flatMap((item) => (
      item.placements.filter((placement) => !placement.affordable)
        .map((placement) => placement.label)
    )).join(",");
    const placementLabel = `value=${placementReferenceValue(workspace)}`
      + `; occupied=${workspace.dice.length}/${workspace.capacity}`
      + (workspace.dice.length && workspace.kind !== "well"
        ? `; top=${workspace.dice.at(-1)!.color}${workspace.dice.at(-1)!.value}; printed=${workspace.printedValue}` : "");
    const header = `${visibleName} ${placementLabel}`
      + `${workspace.kind === "castle"
        ? reachableColors.length === workspace.allowedColors.length
          ? `; legal=${colors}`
          : `; legal-now=${colors || "none"}; printed=${printedColorText}`
        : ""}`
      + `${cannotPay ? `; cannot-pay=${cannotPay}` : ""}`;
    // A blocked placement does not remove the player's installed capability.
    // Keep the trigger separate from the effects, using the same engine projection.
    const persistentCapability = domainRow
      ? `; ONLY ${printedColorText} die compatible; capability when triggered=${rewardFor(workspace.allowedColors[0])}`
      : "";
    // A castle-row reward is a separate access path: die placement limits
    // must not hide a printed row that a garden/warrior reward can select.
    const room = castleMatch
      ? state.board.castleRooms.find((candidate) => candidate.id === `${castleMatch[1]}-${castleMatch[2]}`)
      : undefined;
    const hiddenRows = (room?.slots ?? []).flatMap((slot, index) => (
      full || !reachableColors.includes(slot.color)
        ? [`[Row ${index + 1}; ${slot.color}${lightRows.has(`${room!.id}/${slot.rowId}`) ? "; light" : ""}]=${render(slot.effects)}`]
        : []
    ));
    const rowCapabilities = hiddenRows.length
      ? [`Row rewards: ${visibleName} -> ${hiddenRows.join("; ")}`]
      : [];
    if (full) {
      return [`UNAVAILABLE(full; no die placement): ${header}${persistentCapability}`, ...rowCapabilities];
    }
    if (!rewards.length) {
      return [`UNAVAILABLE(no affordable selectable endpoint): ${header}${persistentCapability}`, ...rowCapabilities];
    }
    const noMemberAction = domainRow && !rewards[0]?.reward.includes(" action")
      ? `; no ${domainRow} action`
      : "";
    const castleRewardLine = (item:{color:string; reward:string}):string => {
      const unreachableEnds = placementsByColor
        .find((placement) => placement.color === item.color)
        ?.placements.filter((placement) => !placement.affordable)
        .map((placement) => placement.label.replace(`${item.color}-`, ""))
        .join(",");
      return `${visibleName} ${colorName(item.color)} ${placementLabel}`
        + `${unreachableEnds ? `; cannot-pay=${unreachableEnds}` : ""}`
        + ` -> ${item.reward}`;
    };
    if (!sameReward && workspace.kind === "castle") {
      return [...rewards.map(castleRewardLine), ...rowCapabilities];
    }
    return [...(sameReward
      ? [rewards.length === 1
        ? `${header}; ONLY ${colors} die legal; reward=${rewards[0].reward}${noMemberAction}`
        : `${header}; ${colors} -> ${rewards[0].reward}`]
      : [header, ...rewards.map((item) => `  ${colorName(item.color)} -> ${item.reward}`)]), ...rowCapabilities];
  };
  const workspaces = Object.values(state.workspaces).filter((workspace) => (
    workspace.active
    && (workspace.owner === undefined || workspace.owner === seat)
  ));
  const publicWorkspacePriority:Record<string, number> = {
    "outside-left": 0,
    "outside-right": 1,
    well: 2,
  };
  const publicWorkspaces = workspaces
    .filter((workspace) => workspace.owner === undefined && workspace.kind !== "castle")
    .sort((left, right) => (
      (publicWorkspacePriority[left.id] ?? 99) - (publicWorkspacePriority[right.id] ?? 99)
    ))
    .flatMap(workspaceLines);
  const castleWorkspaces = workspaces
    .filter((workspace) => workspace.owner === undefined && workspace.kind === "castle")
    .flatMap(workspaceLines);
  const personalWorkspaces = workspaces
    .filter((workspace) => workspace.owner === seat)
    .flatMap(workspaceLines);
  const selectableDieValues = [...new Set(
    [...legalBridgeEnds.values()].flat().map((die) => die.value),
  )].sort((a, b) => a - b);
  const activeWorkspaceValues = [...new Set(
    workspaces.filter((workspace) => workspace.capacity === "unlimited" || workspace.dice.length < workspace.capacity)
      .map(placementReferenceValue),
  )].sort((a, b) => a - b);
  const placementCoinMatrix = activeWorkspaceValues.map((workspaceValue) => (
    `workspace value ${workspaceValue} -> ${selectableDieValues.map((dieValue) => {
      const delta = dieValue - workspaceValue;
      const consequence = delta > 0 ? `gain ${delta} coins`
        : delta < 0 ? `pay ${-delta} coins` : "no coin change";
      return `die ${dieValue}: ${consequence}`;
    }).join(", ")}`
  )).join("; ");

  const scores = scoreGame(state);
  const currentScore = scores.find((score) => score.player === seat)!;
  const opponentScores = scores
    .filter((score) => score.player !== seat)
    .map((score) => `P${score.player}=${score.total}`)
    .join(", ");
  const promotionTargets = new Map<string, {
    id:string;
    score:number;
    routes:string[];
    arrival:string;
  }>();
  for (const source of buildCourtierPromotionSources(state, seat)) {
    for (const target of source.targets) {
      const cost = (target.rawCost as { pearl:number }).pearl;
      const targetFact = promotionTargets.get(target.id) ?? {
        id:target.id,
        score:target.scoreAfter,
        routes:[],
        arrival:compactEffect(target.publicArrivalEffects),
      };
      const route = `${source.location}+${target.levels}/pay${cost}`;
      if (!targetFact.routes.includes(route)) targetFact.routes.push(route);
      promotionTargets.set(target.id, targetFact);
    }
  }
  const postRecruit = buildPostRecruitPromotionSource(state, seat);
  for (const target of postRecruit?.targets ?? []) {
    const cost = target.rawCost.pearl;
    const targetFact = promotionTargets.get(target.id) ?? {
      id:target.id,
      score:target.scoreAfter,
      routes:[],
      arrival:compactEffect(target.publicArrivalEffects),
    };
    const route = `after recruit+${target.levels}/pay${cost}`;
    const existingGateRoute = `gate+${target.levels}/pay${cost}`;
    // A recruited courtier arrives at gate.  When an existing gate courtier
    // already exposes the identical promotion route, repeating every target
    // as a second "after recruit" route adds no decision fact and can make the
    // dynamic Frame grow as the game progresses.
    if (
      !targetFact.routes.includes(existingGateRoute)
      && !targetFact.routes.includes(route)
    ) targetFact.routes.push(route);
    promotionTargets.set(target.id, targetFact);
  }
  const promotionLines = [...promotionTargets.values()].map((target) => {
    const room = state.board.castleRooms.find((candidate) => candidate.id === target.id);
    let cardTransfer = "";
    if (room) {
      const cards = room.floor === "steward" ? STEWARD_CARDS : DIPLOMAT_CARDS;
      const deck = room.floor === "steward" ? state.board.stewardDeck : state.board.diplomatDeck;
      const card = cards.find((candidate) => candidate.id === room.cardId)!;
      cardTransfer = deck.length
        ? `; take this room card: personal ${castleCardDomainRowGroups(card).map((rows, index) => (
          `${rows.join("/")}←Row ${index + 1}`
        )).join(", ")}`
        : "; floor deck empty: keep current personal card and Lantern";
    }
    return `${target.id}: routes=${target.routes.join(" | ")}; endPoints=${target.score}; `
      + `arrival=[${target.arrival}]${cardTransfer}.`;
  });
  const postRecruitLine = postRecruit ? (
    "After recruit, that courtier is at gate and may promote now via its listed gate/after-recruit route; resolve the numbered arrival reward."
  ) : null;
  const roundEndGardenLines = state.round < 3 ? [
    "Round-end reactivation requires at least one die on the garden's own bridge after the round's final draft. Taking a bridge's last die prevents every player's gardens on that bridge from reactivating; their game-end points remain.",
  ] : [];
  const nextTurnTiming = boundary.endsGame
    ? "This is the game's final die action; final scoring follows, with no garden reactivation."
    : boundary.endsRound
      ? "This is the round's final die action. No other player drafts again before round-end garden rewards. Your next die action follows next round's order."
      : boundary.laterTurnsRemaining === 0
        ? state.round === 3
          ? "This is your last die action. Other players may still finish theirs before final scoring; you have no later die action."
          : "This is your last die action this round. Other players finish remaining die actions before round-end rewards; your next die action follows next round's order."
        : "Other players take their die actions in this round's cyclic order before your next die action.";

  return {
    version:1,
    coverage:"complete-current-decision",
    facts:[
      fact("TurnFact", "turn", "T", {
        lines:[
          `round=${state.round}/3; turn=${state.turn}/${state.playerCount * 9}; actor=P${seat}; `
          + `roundActionsLeft=${boundary.actorTurnsRemaining} including now (${boundary.laterTurnsRemaining} later this round); `
          + `gameActionsLeft=${boundary.actorTurnsRemaining + Math.max(0, 3 - state.round) * 3} including now; `
          + `endsRound=${boundary.endsRound ? "yes" : "no"}; endsGame=${boundary.endsGame ? "yes" : "no"}; `
          + `next=${boundary.nextActor === null ? "unknown" : `P${boundary.nextActor}`}.`,
          `Players=${state.playerCount}; cyclic order=${state.turnOrder.map((id) => `P${id}`).join("→")}; ${nextTurnTiming}`,
          "Action counts are personal die placements; reward choices are not extra die actions.",
        ],
      }),
      fact("ResourceSnapshot", "personal", "Personal", {
        lines:[
          `Resources: coins=${player.resources.coins}; ${(['seals','food','iron','pearl'] as const).map(resource=>`${resource}=${player.resources[resource]} (cap ${RESOURCE_CAPS[resource]})`).join('; ')}.`,
          `Influence=${player.influence}; influence_checkpoint choiceIds=[0=pay and cross,1=stop before checkpoint]; next=${nextCheckpoints.join(", ") || "none"}.`,
          `Lantern=${lantern}; ${player.lanternEffects.length > 1
            ? "resolve all in an order you choose"
            : "resolve all listed effects"}.`,
        ],
      }),
      fact("CurrentTargetFact", "action-selection", "Actions", {
        lines:[
          "Choose 1 die/turn for 1 public/personal workspace.",
          `Placement value=top die if occupied, printed value if empty; well always compares with 1. Public capacity=${state.playerCount === 2 ? 1 : 2}, personal capacity=1, well unlimited. The new die color selects the reward.`,
          "affordable=at Frame start; prior gains/payments/exchanges may change it at activation.",
          `Bridge endpoints: ${bridgeLine}. For a selectable left endpoint, resolve Lantern/checkpoints before workspace rewards.`,
          `Placement coins (die value minus workspace value): ${placementCoinMatrix}.`,
          "Resolve placement coins first; endpoints still unaffordable after available seal→coin exchanges are illegal.",
          "Placement never costs food/iron/pearl. Member costs are separate.",
          "Seal exchange available: 1 seal→1 coin; 2→1 food/iron/pearl; repeatable, no die action.",
          "Public workspaces:",
          ...publicWorkspaces,
          "Personal dice workspaces: row=courtier/gardener/warrior identifies a member-reserve row, not a member action. Activate the position to receive its revealed queue rewards and current action-card effects in order.",
          "Only an explicit courtier/gardener/warrior action in the actual effects opens that member action; it may differ from the row name. Recruiting/deploying reveals queue rewards; replacing the card changes card effects. Neither change alone activates the row.",
          ...personalWorkspaces,
          "personal_row_choice: unused rows ordered courtier/gardener/warrior; remove used rows (die or reward), renumber from 0. "
          + "The once-per-turn limit is per personal row activation, not per member type: separately unlocked gardener/warrior/courtier actions can occur in the same turn.",
        ],
      }),
      fact("CurrentTargetFact", "castle-actions", "Castle", {
        lines:[
          "Castle: placement executes its listed groups after left Lantern/checkpoints. Row choices need matching color/light/any reward, no die; check costs when reached.",
          ...castleWorkspaces,
        ],
      }),
      fact("CurrentTargetFact", "gardener-area", "Gardeners", {
        lines:[
          `Gardener: domain→available garden; pay food; gain listed reward now and printed points at game end; ${state.round < 3 ? "eligible owned gardens reactivate at round end" : "round 3 ends with final scoring, so gardens do not reactivate"}. foodNow=${player.resources.food}.`,
          ...roundEndGardenLines,
          `Members: ${memberLine("gardener")}.`,
          ...state.gardens.map((garden) => (
            `${garden.id}: ${garden.gardeners.includes(seat) ? "unavailable(already yours)" : "available"}; `
            + `payFood=${garden.foodCost}; affordable=${player.resources.food >= garden.foodCost ? "yes" : "no"}; `
            + `endPoints=${garden.points}; now=${render(garden.effects)}; owners=[${garden.gardeners.map((id) => `P${id}`).join(",")}].`
          )),
        ],
      }),
      fact("CurrentTargetFact", "warrior-area", "Warriors", {
        lines:[
          `Yards are not die workspaces. Warrior: domain→non-full yard; pay iron; gain reward now; endPoints=yard value×castle courtiers. ironNow=${player.resources.iron}.`,
          `Warriors stay; multiplier=${castleCourtierCount} castle courtiers (gate excluded); gate→castle adds 1; promotion within castle adds 0.`,
          `Members: ${memberLine("warrior")}.`,
          ...state.trainingYards.map((yard) => (
            `${yard.id}: full=${yard.capacity !== "unlimited" && yard.warriors.length >= yard.capacity ? "yes" : "no"}; `
            + `payIron=${yard.ironCost}; affordable=${player.resources.iron >= yard.ironCost ? "yes" : "no"}; value=${yard.warriorValue}; `
            + `now=${yard.effects.length > 1 ? render({
              type:"actionOrder",
              groups:yard.effects.map((effect, index) => ({
                id:`yard-tile-${yard.tileIds[index] ?? index + 1}`, effects:[effect],
              })),
            }) : render(yard.effects)}.`
          )),
        ],
      }),
      fact("CurrentTargetFact", "courtier-area", "Courtiers", {
        lines:[
          "Courtier: recruit≤1(pay2 coins) and promote≤1(+1/pay2 pearl or +2/pay5), either order.",
          `When arrival takes a room card, it replaces the current personal card (${player.domainCard.kind}-${player.domainCard.id}); `
          + `the old card adds [${render(domainCardLanternEffects(player))}] to future Lantern activations. `
          + "The arrival reward is a separate choice from that room card; it does not select which card to take. "
          + "If that reward activates Lantern/personal rows, use the replaced card state. Card replacement alone does not activate them.",
          `Members: ${memberLine("courtier")}.`,
          ...(postRecruitLine ? [postRecruitLine] : []),
          ...promotionLines,
        ],
      }),
      fact("DynamicScoreFact", "scoring", "Score", {
        lines:[
          `Now: total=${currentScore.total}; during=${currentScore.duringGame}; track=${currentScore.timeTrack}; courtier=${currentScore.courtiers}; warrior=${currentScore.warriors}; gardener=${currentScore.gardeners}.`,
          `Opponent totals: ${opponentScores || "none"}.`,
          "Influence end: 0-5=0; 6-10=3; 11-14=6; 15-20=position minus 5. Positions count spaces from the heron, not printed score values.",
          "Member end: courtier gate/steward/diplomat/daimyo=1/3/6/10; gardener=garden points; warrior=yard value x castle courtiers.",
        ],
      }),
    ],
  };
}
