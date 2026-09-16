import { influencePaymentLabel } from "./reward-interaction";
import {
  getLegalActions,
  INFLUENCE_CHECKPOINTS,
  type GameAction,
  type GameEvent,
  type GameState,
  type PlayerState,
  type Resource,
} from "../core";

/**
 * The presenter deliberately repeats the small shared-shell shape here.  The
 * browser renderer is a dependency-free UMD module, while the game package is
 * TypeScript; keeping this structural type local means the presenter remains
 * usable in focused tests even when the shared package is consumed as a plain
 * browser asset.
 */
export type GameField = {
  id: string;
  label: string;
  value: string | number;
  secondaryValue?: string | number;
  icon?: string;
  tone?: "default" | "positive" | "warning" | "danger";
};

export type GameProgressItem = {
  id: string;
  label: string;
  value: string | number;
  tone?: "default" | "positive" | "warning" | "danger";
};

export type GameFieldGroup = {
  id: string;
  label: string;
  fields: GameField[];
  collapsible?: boolean;
};

export type GameTimelineEntry = {
  id: string;
  turnId?: string;
  actor?: { seat: number; name: string } | null;
  kind: "choice" | "payment" | "gain" | "movement" | "score" | "phase" | "error";
  summary: string;
  icon?: string;
  valueDelta?: { before?: number; after?: number; delta?: number };
  children?: GameTimelineEntry[];
};

export type GameInteractionView = {
  state:
    | "idle"
    | "source_selectable"
    | "source_selected"
    | "target_selectable"
    | "effect_pending"
    | "confirmation_pending"
    | "resolving"
    | "waiting_next_decision"
    | "finished"
    | "error";
  instruction: string;
  sourceLabel?: string;
  targetLabel?: string;
  validatedSteps?: string[];
  canCancel: boolean;
  canSkip: boolean;
  canConfirm: boolean;
  cancelLabel?: string;
  skipLabel?: string;
  confirmLabel?: string;
  error?: { code: string; message: string };
};

export type GameShellView = {
  game: {
    id: string;
    title: string;
    phaseLabel: string;
    progressLabel: string;
    progressPercent?: number;
    progressItems?: GameProgressItem[];
    primaryInstruction: string;
  };
  activeSeat: number | null;
  players: Array<{
    seat: number;
    name: string;
    kind: "human" | "ai";
    isViewer: boolean;
    isActive: boolean;
    score: number | null;
    statusLabel?: string;
    gameFields: GameFieldGroup[];
  }>;
  interaction: GameInteractionView;
  timeline: GameTimelineEntry[];
  finalResult?: {
    columns?: string[];
    title: string;
    winnerSeats: number[];
    summary: string;
    rows: Array<{ id: string; label: string; values: number[] }>;
  };
};

export type WhiteCastleUiState = {
  busy?: boolean;
  readOnly?: boolean;
  announcement?: string | null;
  pausedReason?: string | null;
  undoLockedReason?: string | null;
  manualTest?: boolean;
  viewerSeat?: number | null;
  playerTypes?: readonly string[];
  legalActions?: readonly GameAction[];
  draftActions?: readonly GameAction[];
  resourceChoiceDraft?: { effectId: string; total: number; picks: readonly Resource[] } | null;
  setupDraftAction?: GameAction | null;
  hasUndo?: boolean;
  error?: { code: string; message: string };
};

export type WhiteCastleActionRecord = {
  decisionId?: string;
  turnId?: string;
  turn?: number | null;
  actor?: number | null;
  action?: unknown;
  events?: unknown[];
  outcome?: unknown;
  order?: number;
  text?: string;
};

const RESOURCE_LABELS: Record<Resource, string> = {
  coins: "钱币",
  seals: "家纹",
  food: "食物",
  iron: "铁",
  pearl: "珍珠母",
};

const MEMBER_LABELS = {
  courtier: "家臣",
  gardener: "园丁",
  warrior: "武士",
} as const;

const COLOR_LABELS = { black: "黑", white: "白", coral: "红" } as const;

const PHASE_LABELS: Record<GameState["phase"], string> = {
  setup: "选择初始组合",
  draft: "选择骰子",
  place: "放置骰子",
  resolve: "执行行动",
  roundEnd: "轮末庭园结算",
  finished: "游戏结束",
};

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function playerName(state: GameState, seat: number | null | undefined): string {
  return Number.isInteger(seat) && state.players[seat as number]
    ? state.players[seat as number].name
    : Number.isInteger(seat)
      ? `玩家 ${(seat as number) + 1}`
      : "系统";
}

function eventRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" ? value as Record<string, unknown> : null;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function textValue(value: unknown): string | undefined {
  return typeof value === "string" && value.length > 0 ? value : undefined;
}

function resourceLabel(value: unknown): string {
  return RESOURCE_LABELS[value as Resource] ?? String(value ?? "资源");
}

function memberLabel(value: unknown): string {
  return MEMBER_LABELS[value as keyof typeof MEMBER_LABELS] ?? String(value ?? "米宝");
}

function locationLabel(value: unknown, state?: GameState): string {
  const location = String(value ?? "版图");
  const workspace = state?.workspaces[location];
  if (workspace?.label) return workspace.label;
  if (location === "gate") return "城门";
  if (location === "daimyo") return "大名房间";
  if (location === "domain") return "个人领地";
  if (location === "lantern") return "灯笼区";
  const garden = location.match(/^garden-(black|white|coral)-(\d+)$/);
  if (garden) return `${COLOR_LABELS[garden[1] as keyof typeof COLOR_LABELS]}桥庭园 ${garden[2]}`;
  const yard = location.match(/^yard-(\d+)$/);
  if (yard) return `训练场 ${yard[1]}`;
  return location
    .replace(/^steward-/, "家臣层 ")
    .replace(/^diplomat-/, "使节层 ")
    .replace(/^castle-/, "城堡 ");
}

function effectLabel(effect: unknown): string {
  const value = eventRecord(effect);
  if (!value || typeof value.type !== "string") return "处理当前效果";
  switch (value.type) {
    case "gain":
      return `获得 ${numberValue(value.amount) ?? 0} ${resourceLabel(value.resource)}`;
    case "gainChoice":
      return `选择获得 ${numberValue(value.amount) ?? 0} 个资源`;
    case "gainPoints":
      return `获得 ${numberValue(value.amount) ?? 0} 分`;
    case "influence":
      return `影响力前进 ${numberValue(value.amount) ?? 0}`;
    case "pay":
      return `支付 ${numberValue(value.amount) ?? 0} ${resourceLabel(value.resource)}`;
    case "majorAction":
      return `执行${memberLabel(value.action)}行动`;
    case "castleRefresh":
      return "确认奖励并补充城堡卡牌";
    case "gardenActivation":
      return "激活庭园";
    case "actionOrder":
      return "选择行动结算顺序";
    case "effectOrder":
      return "选择效果结算顺序";
    case "chooseOne":
      return "选择一项行动";
    case "domainAction":
      return "选择个人领地行动";
    case "castleTileAction":
      return "选择城堡行动行";
    case "wellAction":
      return "执行水井行动";
    case "lantern":
      return "触发灯笼奖励";
    case "daimyoReward":
      return "选择大名奖励";
    default:
      return "处理当前效果";
  }
}

function actionFlowCostLabel(state: GameState): string {
  return Object.entries(state.actionFlow?.costs ?? {})
    .filter(([, amount]) => typeof amount === "number" && amount > 0)
    .map(([resource, amount]) => `${amount} ${resourceLabel(resource)}`)
    .join("、");
}

function actionFlowPreviewInstruction(state: GameState): string {
  const flow = state.actionFlow;
  if (!flow?.selectedTarget) return "请检查花费与奖励后确认";
  const cost = actionFlowCostLabel(state);
  const parts = [
    cost ? `支付 ${cost}` : "无需支付",
    `将${memberLabel(flow.kind)}放到${locationLabel(flow.selectedTarget, state)}`,
  ];
  if (flow.mode === "gardener") {
    const garden = state.gardens.find((candidate) => candidate.id === flow.selectedTarget);
    if (garden) {
      parts.push(`终局 ${garden.points} 分`);
      parts.push(...garden.effects.map(effectLabel));
    }
  } else if (flow.mode === "warrior") {
    const yard = state.trainingYards.find((candidate) => candidate.id === flow.selectedTarget);
    if (yard) {
      parts.push(`武士值 ${yard.warriorValue}`);
      parts.push(...yard.effects.map(effectLabel));
    }
  } else if (flow.mode === "promote") {
    parts.push(flow.selectedTarget === "daimyo" ? "随后选择大名奖励" : "随后结算房间奖励并补牌");
  }
  return parts.join("，");
}

function actionFlowConfirmLabel(state: GameState): string {
  const flow = state.actionFlow;
  if (!flow) return "确认行动";
  const cost = actionFlowCostLabel(state);
  return `${cost ? `支付 ${cost}并` : ""}放置${memberLabel(flow.kind)}`;
}

function phaseInstruction(state: GameState): string {
  const pending = state.pendingEffects[0]?.effect;
  const flow = state.actionFlow;
  if (flow?.stage === "chooseSource") return `请选择一名${memberLabel(flow.kind)}`;
  if (flow?.stage === "chooseTarget") return "请选择一个高亮目的地";
  if (flow?.stage === "preview") return actionFlowPreviewInstruction(state);
  if (pending?.type === "gainChoice") return "请选择一种资源";
  if (pending?.type === "effectOrder") return "请选择结算顺序，或按印刷顺序结算全部";
  if (pending?.type === "actionOrder") return "请选择先结算的行动";
  if (pending?.type === "chooseOne") return "请选择一项行动";
  if (pending?.type === "pay") return "请选择支付或跳过";
  if (pending?.type === "influence") {
    const position = state.players[state.currentPlayer].influence;
    const checkpoint = INFLUENCE_CHECKPOINTS.find((item) => position < item.position && position + pending.amount >= item.position);
    if (checkpoint) return `本次影响力前进 ${pending.amount} 格，将经过第 ${checkpoint.position} 格的季节检查点`;
  }
  if (pending?.type === "domainAction") return "请选择个人版图上高亮的行动行";
  if (pending?.type === "gardenActivation") return "请选择下一座要结算的庭园";
  if (pending?.type === "castleTileAction") {
    const selector = String(pending.color) === "light" ? "浅色背景" : pending.color === "any" ? "任意" : `${COLOR_LABELS[pending.color as keyof typeof COLOR_LABELS]}色`;
    return `请选择高亮的${selector}城堡行动行`;
  }
  if (pending?.type === "castleRefresh") return "请确认浅色行动并补充主版图卡牌";
  if (pending) return "请完成当前奖励或行动";
  switch (state.phase) {
    case "setup": return "请选择一组初始卡牌";
    case "draft": return "请选择桥上最左端或最右端的骰子";
    case "place": return "请选择版图或个人领地上的高亮工位";
    case "roundEnd": return "正在结算轮末庭园";
    case "finished": return `${playerName(state, state.winner ?? 0)}赢得本局游戏`;
    default: return "请完成当前行动";
  }
}

function safeLegalActions(state: GameState): GameAction[] {
  try {
    return getLegalActions(state);
  } catch {
    return [];
  }
}

function diceTurnNumber(state: GameState): number {
  return Math.min(3, Math.floor(Math.max(0, state.turnsThisRound) / state.playerCount) + 1);
}

function progressFor(state: GameState): number {
  if (state.phase === "finished") return 100;
  const round = clamp(state.round - 1, 0, 3);
  const roundTurns = state.playerCount * 3;
  const turns = clamp(state.turnsThisRound, 0, roundTurns) / roundTurns;
  return Math.round(clamp(((round + turns) / 3) * 100, 0, 100));
}

function progressLabel(state: GameState): string {
  if (state.phase === "finished") return "三轮完成 · 终局计分";
  return `第 ${state.round} / 3 轮 · 骰行动 ${diceTurnNumber(state)} / 3`;
}

function progressItems(state: GameState): GameProgressItem[] {
  const bridgeDice = Object.values(state.bridges).reduce((total, dice) => total + dice.length, 0);
  return [
    { id: "round", label: "轮次", value: `${state.round} / 3` },
    { id: "die-action", label: "骰行动", value: `${diceTurnNumber(state)} / 3` },
    { id: "bridge-dice", label: "桥上骰", value: bridgeDice },
  ];
}

function playerFields(player: PlayerState): GameFieldGroup[] {
  const resources = player.resources;
  return [
    {
      id: "resources",
      label: "资源",
      fields: [
        { id: "food", label: "食物", value: resources.food, icon: "food" },
        { id: "iron", label: "铁", value: resources.iron, icon: "iron" },
        { id: "pearl", label: "珍珠母", value: resources.pearl, icon: "pearl" },
        { id: "coins", label: "钱币", value: resources.coins, icon: "coins" },
        { id: "seals", label: "家纹", value: resources.seals, icon: "seals" },
      ],
    },
  ];
}

function actionDescription(action: GameAction): string {
  switch (action.type) {
    case "chooseStartingPair": return `选择初始组合 ${action.offer + 1}`;
    case "draftDie": return `取${COLOR_LABELS[action.bridge]}桥${action.end === "left" ? "左端" : "右端"}骰子`;
    case "placeDie": return `放入 ${locationLabel(action.workspace)}`;
    case "chooseEffectOption": return `选择效果 ${action.option + 1}`;
    case "selectCastleTileAction": return "选择城堡行动行";
    case "exchangeSeal": return action.receive === "coins" ? "家纹兑换钱币" : `家纹兑换${resourceLabel(action.receive)}`;
    case "beginMajorAction": return `开始${action.mode === "recruit" ? "招募" : action.mode === "promote" ? "晋升" : memberLabel(action.mode)}行动`;
    case "selectMajorActionSource": return "选择行动米宝";
    case "selectMajorActionTarget": return `选择目的地 ${locationLabel(action.target)}`;
    case "cancelMajorActionSelection": return "取消行动选择";
    case "confirmMajorAction": return "确认人物行动";
    case "refreshCastleRoom": return "确认奖励并补充城堡卡牌";
    case "finishMajorAction": return "结束人物行动";
    case "finishResolution": return "确认本回合";
    default: return "处理当前行动";
  }
}

function eventEntry(
  state: GameState,
  event: Record<string, unknown>,
  id: string,
): GameTimelineEntry | null {
  const seat = numberValue(event.player);
  const actor = seat === undefined ? null : { seat, name: playerName(state, seat) };
  const type = textValue(event.type) ?? "事件";
  switch (type) {
    case "DieDrafted": {
      const bridge = COLOR_LABELS[event.bridge as keyof typeof COLOR_LABELS] ?? String(event.bridge ?? "");
      const end = event.end === "left" ? "左端" : event.end === "right" ? "右端" : "端点";
      const die = eventRecord(event.die);
      return { id, actor, kind: "choice", summary: `取${bridge}桥${end} ${numberValue(die?.value) ?? ""} 点骰`, icon: "die", children: [] };
    }
    case "DiePlaced":
      return { id, actor, kind: "choice", summary: `将骰子放入${locationLabel(event.workspace, state)}`, icon: "die", children: [] };
    case "ResourceChanged": {
      const amount = numberValue(event.amount) ?? 0;
      return {
        id,
        actor,
        kind: amount < 0 ? "payment" : "gain",
        summary: `${amount < 0 ? "支付" : "获得"} ${Math.abs(amount)} ${resourceLabel(event.resource)}`,
        icon: String(event.resource ?? "resource"),
        children: [],
      };
    }
    case "InfluenceChanged": {
      const amount = numberValue(event.amount) ?? 0;
      return {
        id,
        actor,
        kind: "movement",
        summary: `影响力${amount >= 0 ? "前进" : "后退"} ${Math.abs(amount)} 格`,
        icon: "influence",
        children: [],
      };
    }
    case "MemberMoved": {
      const member = String(event.member ?? "").split("-")[1];
      return { id, actor, kind: "movement", summary: `${memberLabel(member)}移动到${locationLabel(event.to, state)}`, icon: "worker", children: [] };
    }
    case "CardMoved":
      return { id, actor, kind: "movement", summary: `将一张卡牌移动到${locationLabel(event.to, state)}`, icon: "card", children: [] };
    case "ChoiceRequired":
      return null;
    case "EffectResolved": {
      const effect = eventRecord(event.effect);
      if (!effect || effect.type !== "gainPoints") return null;
      return { id, actor, kind: "score", summary: `获得 ${numberValue(effect.amount) ?? 0} 分`, icon: "score", children: [] };
    }
    case "TurnStarted":
      return { id, actor, kind: "phase", summary: `开始第 ${numberValue(event.turn) ?? state.turn} 回合`, icon: "phase", children: [] };
    case "RoundEnded":
      return { id, actor: null, kind: "phase", summary: `第 ${numberValue(event.round) ?? state.round} 轮结束，进入庭园结算`, icon: "phase", children: [] };
    case "RoundStarted":
      return { id, actor, kind: "phase", summary: `第 ${numberValue(event.round) ?? state.round} 轮开始`, icon: "phase", children: [] };
    case "GameScored": {
      const winner = numberValue(event.winner);
      return { id, actor: winner === undefined ? null : { seat: winner, name: playerName(state, winner) }, kind: "score", summary: "游戏结束并获胜", icon: "score", children: [] };
    }
    default:
      return null;
  }
}

function timelineFor(state: GameState, records: readonly WhiteCastleActionRecord[]): GameTimelineEntry[] {
  const usedIds = new Set<string>();
  return records.flatMap((record, recordIndex) => {
    const rawEvents = Array.isArray(record.events) ? record.events : [];
    const firstEvent = rawEvents.map(eventRecord).find((event): event is Record<string, unknown> => Boolean(event));
    const seat = numberValue(record.actor) ?? numberValue(firstEvent?.player);
    const actor = seat === undefined ? null : { seat, name: playerName(state, seat) };
    const turnId = String(record.turnId ?? record.decisionId ?? `turn-${record.turn ?? recordIndex}`);
    const baseId = `twc-${turnId}`;
    let id = baseId;
    let suffix = 1;
    while (usedIds.has(id)) id = `${baseId}-${++suffix}`;
    usedIds.add(id);
    const publicFacts = rawEvents.flatMap((rawEvent, eventIndex) => {
      const event = eventRecord(rawEvent);
      if (!event) return [];
      const entry = eventEntry(state, event, `${id}-${eventIndex}`);
      return entry ? [{ entry: { ...entry, turnId }, eventType: textValue(event.type) }] : [];
    });
    const action = eventRecord(record.action);
    const steps = Array.isArray(action?.steps) ? action.steps.map(eventRecord).filter((step): step is Record<string, unknown> => Boolean(step)) : [];
    if (steps.at(-1)?.op === "finishResolution") {
      const confirmation = {
        entry: {
          id: `${id}-confirmed`,
          turnId,
          actor,
          kind: "phase" as const,
          summary: "确认其回合",
          icon: "phase",
          children: [],
        },
        eventType: "TurnConfirmed",
      };
      const boundaryIndex = publicFacts.findIndex(({ eventType }) => (
        eventType === "TurnStarted"
        || eventType === "RoundEnded"
        || eventType === "RoundStarted"
        || eventType === "GameScored"
      ));
      publicFacts.splice(boundaryIndex < 0 ? publicFacts.length : boundaryIndex, 0, confirmation);
    }
    return publicFacts.reverse().map(({ entry }) => entry);
  });
}

function finalResult(state: GameState): GameShellView["finalResult"] {
  if (state.phase !== "finished" || !state.scores || state.scores.length === 0) return undefined;
  const bySeat = state.players.map((player) => state.scores?.find((score) => score.player === player.id));
  const totals = bySeat.map((score) => score?.total ?? 0);
  const winner = state.winner ?? state.scores.reduce((best, score) => score.total > best.total ? score : best, state.scores[0]).player;
  const winnerName = playerName(state, winner);
  const rows: Array<{ id: string; label: string; values: number[] }> = [
    { id: "total", label: "总分", values: totals },
    { id: "during-game", label: "游戏中得分", values: bySeat.map((score) => score?.duringGame ?? 0) },
    { id: "resources", label: "资源换算", values: bySeat.map((score) => score?.resources ?? 0) },
    { id: "time-track", label: "时间轨", values: bySeat.map((score) => score?.timeTrack ?? 0) },
    { id: "courtiers", label: "家臣", values: bySeat.map((score) => score?.courtiers ?? 0) },
    { id: "warriors", label: "武士", values: bySeat.map((score) => score?.warriors ?? 0) },
    { id: "gardeners", label: "园丁", values: bySeat.map((score) => score?.gardeners ?? 0) },
  ];
  return {
    title: "终局计分",
    columns: state.players.map(player => playerName(state, player.id)),
    winnerSeats: [winner],
    summary: `${winnerName}获胜 · ${totals.map((total, seat) => `${playerName(state, seat)} ${total} 分`).join("，")}`,
    rows,
  };
}

function interactionFor(state: GameState, uiState: WhiteCastleUiState, legalActions: readonly GameAction[]): GameInteractionView {
  const pending = state.pendingEffects[0];
  const flow = state.actionFlow;
  const isAi = uiState.playerTypes?.[state.currentPlayer] === "ai";
  const canAct = !uiState.busy && !uiState.readOnly && !uiState.pausedReason && !uiState.error && !isAi && state.phase !== "finished";
  const setupOffer = uiState.setupDraftAction?.type === "chooseStartingPair"
    ? uiState.setupDraftAction.offer
    : undefined;
  const setupConfirmed = state.phase === "setup" && setupOffer !== undefined;
  const resourceConfirmed = Boolean(
    uiState.resourceChoiceDraft
    && uiState.resourceChoiceDraft.picks.length >= uiState.resourceChoiceDraft.total,
  );
  const has = (type: GameAction["type"]): boolean => legalActions.some((action) => action.type === type);
  let interactionState: GameInteractionView["state"] = "idle";
  if (uiState.pausedReason || uiState.error) interactionState = "error";
  else if (state.phase === "finished") interactionState = "finished";
  else if (uiState.busy) interactionState = "resolving";
  else if (isAi || uiState.readOnly) interactionState = "waiting_next_decision";
  else if (flow?.stage === "chooseSource") interactionState = flow.selectedSource ? "source_selected" : "source_selectable";
  else if (flow?.stage === "chooseTarget") interactionState = "target_selectable";
  else if (flow?.stage === "preview") interactionState = "confirmation_pending";
  else if (setupConfirmed || resourceConfirmed) interactionState = "confirmation_pending";
  else if (pending) interactionState = "effect_pending";
  else if (state.phase === "draft" || state.phase === "setup") interactionState = "source_selectable";
  else if (state.phase === "place") interactionState = "target_selectable";
  else if (has("finishResolution") || has("confirmMajorAction")) interactionState = "confirmation_pending";

  const canConfirm = canAct && (
    has("finishResolution")
    || has("confirmMajorAction")
    || has("refreshCastleRoom")
    || setupConfirmed
    || resourceConfirmed
  );
  const completedCourtierSubactions = pending?.effect.type === "majorAction"
    && pending.effect.action === "courtier"
    ? pending.completedSubactions ?? []
    : [];
  const partialCourtierInstruction = completedCourtierSubactions.includes("recruit")
    ? has("beginMajorAction")
      ? "请求觐见已完成，可继续晋升或结束家臣行动"
      : "请求觐见已完成，当前无法继续晋升，可结束家臣行动"
    : completedCourtierSubactions.includes("promote")
      ? has("beginMajorAction")
        ? "晋升已完成，可继续请求觐见或结束家臣行动"
        : "晋升已完成，当前无法继续请求觐见，可结束家臣行动"
      : undefined;
  const unavailableMajorAction = pending?.effect.type === "majorAction"
    && has("finishMajorAction")
    && !has("beginMajorAction")
    ? partialCourtierInstruction
      ?? `当前无法执行${memberLabel(pending.effect.action)}行动，请跳过`
    : undefined;
  const instruction = uiState.pausedReason
    ?? (uiState.readOnly ? "历史回放，只读查看" : undefined)
    ?? (isAi ? uiState.announcement ?? "等待当前玩家行动" : undefined)
    ?? unavailableMajorAction
    ?? (!flow ? partialCourtierInstruction : undefined)
    ?? uiState.announcement
    ?? phaseInstruction(state);
  const validatedSteps = (uiState.draftActions ?? []).map(actionDescription);
  const error = uiState.error ?? (uiState.pausedReason ? { code: "paused", message: uiState.pausedReason } : undefined);
  const cancelLabel = flow?.selectedSource || flow?.selectedTarget ? "重新选择" : "取消";
  const skipAction = whiteCastleSkipAction(state, legalActions);
  const confirmLabel = setupConfirmed
    ? "确认起始卡牌"
    : resourceConfirmed
      ? "确认资源"
      : has("refreshCastleRoom")
        ? "确认并补牌"
        : has("confirmMajorAction")
          ? actionFlowConfirmLabel(state)
          : has("finishResolution")
            ? "确认回合"
            : "确认";
  return {
    state: interactionState,
    instruction,
    validatedSteps: validatedSteps.length > 0 ? validatedSteps : undefined,
    canCancel: canAct && has("cancelMajorActionSelection"),
    canSkip: canAct && Boolean(skipAction),
    canConfirm,
    cancelLabel,
    skipLabel: skipAction?.type === "chooseEffectOption" && state.pendingEffects[0]?.effect.type === "influence"
      ? influencePaymentLabel(state, 1)
      : skipAction?.type === "finishMajorAction" && completedCourtierSubactions.length > 0
      ? "结束家臣行动"
      : "跳过",
    confirmLabel,
    error,
  };
}

/**
 * Only actions whose public meaning is genuinely "skip/decline" belong in the
 * shared dock.  Option index 1 is a normal strategic choice for several other
 * White Castle effects, so it must remain in the game-owned action panel.
 */
export function whiteCastleSkipAction(state: GameState, legalActions: readonly GameAction[]): GameAction | undefined {
  const finishMajorAction = legalActions.find((action) => action.type === "finishMajorAction");
  if (finishMajorAction) return finishMajorAction;
  const pending = state.pendingEffects[0];
  if (!pending || (pending.effect.type !== "pay" && pending.effect.type !== "influence")) return undefined;
  return legalActions.find((action) => (
    action.type === "chooseEffectOption"
    && action.effectId === pending.id
    && action.option === 1
  ));
}

/** Build the only game-specific data consumed by the shared browser shell. */
export function buildWhiteCastleShellView(
  state: GameState,
  uiState: WhiteCastleUiState = {},
  records: readonly WhiteCastleActionRecord[] = [],
): GameShellView {
  const legalActions = uiState.legalActions ?? safeLegalActions(state);
  const final = finalResult(state);
  const primaryInstruction = uiState.pausedReason
    ?? uiState.announcement
    ?? (uiState.busy && uiState.playerTypes?.[state.currentPlayer] === "ai" ? `${playerName(state, state.currentPlayer)}正在思考…` : phaseInstruction(state));
  return {
    game: {
      id: "the-white-castle",
      title: "白城堡",
      phaseLabel: PHASE_LABELS[state.phase],
      progressLabel: progressLabel(state),
      progressPercent: progressFor(state),
      progressItems: progressItems(state),
      primaryInstruction,
    },
    activeSeat: state.phase === "finished" ? null : state.currentPlayer,
    players: state.players.map((player) => {
      const score = state.scores?.find((candidate) => candidate.player === player.id)?.total ?? player.points;
      const kind = uiState.playerTypes?.[player.id] === "ai" ? "ai" : "human";
      return {
        seat: player.id,
        name: player.name,
        kind,
        isViewer: uiState.viewerSeat === undefined ? player.id === 0 : uiState.viewerSeat === player.id,
        isActive: state.phase !== "finished" && state.currentPlayer === player.id,
        score,
        gameFields: playerFields(player),
      };
    }),
    interaction: interactionFor(state, uiState, legalActions),
    timeline: timelineFor(state, records),
    finalResult: final,
  };
}
