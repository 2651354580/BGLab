import type { Effect, GameAction, GameState, MajorAction } from "../core";
import { INFLUENCE_CHECKPOINTS } from "../core";

const DOMAIN_ROWS: MajorAction[] = ["courtier", "gardener", "warrior"];
const MEMBER_NAMES: Record<MajorAction, string> = { courtier: "家臣", gardener: "园丁", warrior: "武士" };
const RESOURCE_NAMES = { coins: "钱币", seals: "家纹", food: "食物", iron: "铁", pearl: "珍珠母" } as const;

export function domainRewardActionIndex(state: GameState, legalActions: GameAction[], row: MajorAction): number {
  const pending = state.pendingEffects[0];
  if (!pending || pending.effect.type !== "domainAction") return -1;
  const option = DOMAIN_ROWS.filter((candidate) => !state.usedDomainRowsThisTurn.includes(candidate)).indexOf(row);
  if (option < 0) return -1;
  return legalActions.findIndex((action) => action.type === "chooseEffectOption"
    && action.effectId === pending.id
    && action.option === option);
}

export function gardenActivationActionIndex(state: GameState, legalActions: GameAction[], gardenId: string): number {
  const pending = state.pendingEffects[0];
  if (!pending || pending.effect.type !== "gardenActivation") return -1;
  const option = pending.effect.gardens.indexOf(gardenId);
  if (option < 0) return -1;
  return legalActions.findIndex((action) => action.type === "chooseEffectOption"
    && action.effectId === pending.id
    && action.option === option);
}

export function daimyoRewardActionIndex(state: GameState, legalActions: GameAction[], position: number): number {
  const pending = state.pendingEffects[0];
  if (!pending || pending.effect.type !== "daimyoReward") return -1;
  const option = pending.effect.positions.indexOf(position);
  if (option < 0) return -1;
  return legalActions.findIndex((action) => action.type === "chooseEffectOption"
    && action.effectId === pending.id
    && action.option === option);
}

export function deterministicGardenActivation(
  state: GameState,
  legalActions: GameAction[],
): Extract<GameAction, { type: "chooseEffectOption" }> | undefined {
  const pending = state.pendingEffects[0];
  if (!pending || pending.effect.type !== "gardenActivation" || pending.effect.gardens.length !== 1) return undefined;
  const choices = legalActions.filter((action): action is Extract<GameAction, { type: "chooseEffectOption" }> => (
    action.type === "chooseEffectOption" && action.effectId === pending.id
  ));
  return choices.length === 1 ? choices[0] : undefined;
}

export function isBoardNativeRewardAction(state: GameState, action: GameAction): boolean {
  if (["selectCastleTileAction", "selectMajorActionSource", "selectMajorActionTarget"].includes(action.type)) return true;
  if (action.type !== "chooseEffectOption") return false;
  const pending = state.pendingEffects[0];
  if (!pending || action.effectId !== pending.id) return false;
  return pending.effect.type === "domainAction" || pending.effect.type === "gardenActivation";
}

function continuationLabel(effect: Effect): string {
  if (effect.type === "majorAction") return `执行${MEMBER_NAMES[effect.action]}行动`;
  if (effect.type === "domainAction") return "执行个人领地行动";
  if (effect.type === "castleTileAction") {
    const selector = String(effect.color) === "light"
      ? "浅色背景"
      : effect.color === "any"
        ? "任意"
        : effect.color === "coral"
          ? "红色"
          : effect.color === "black"
            ? "黑色"
            : "白色";
    return `执行${selector}城堡行动行`;
  }
  if (effect.type === "lantern") return "获得灯笼奖励";
  if (effect.type === "wellAction") return "执行水井行动";
  if (effect.type === "gain") return `获得 ${effect.amount} ${RESOURCE_NAMES[effect.resource]}`;
  if (effect.type === "gainPoints") return `获得 ${effect.amount} 分`;
  if (effect.type === "influence") return `影响力前进 ${effect.amount}`;
  return "执行后续奖励";
}

export function payActionLabel(effect: Extract<Effect, { type: "pay" }>, option: number): string {
  if (option !== 0) return "跳过支付";
  const continuation = effect.effects.map(continuationLabel).join("并");
  return `支付 ${effect.amount} ${RESOURCE_NAMES[effect.resource]}${continuation ? `并${continuation}` : ""}`;
}

export function influencePaymentLabel(state: GameState, option: number): string {
  const position = state.players[state.currentPlayer].influence;
  const checkpoint = INFLUENCE_CHECKPOINTS.find((item) => item.position > position);
  if (!checkpoint) return "影响力已到最后一季";
  return option === 0
    ? `支付 ${checkpoint.cost} 家纹，通过季节检查点`
    : `不支付，停在第 ${checkpoint.position - 1} 格`;
}

export function lanternCollectionStep(state: GameState, legalActions: GameAction[]): Extract<GameAction, { type: "chooseEffectOption" }> | undefined {
  const pending = state.pendingEffects[0];
  if (!pending || !["lantern", "daimyo-lantern"].includes(pending.source) || pending.effect.type !== "effectOrder") return undefined;
  // A human collects the whole lantern reward in printed order. The engine
  // still stops on resource selections, payments, and other actual decisions.
  return legalActions.find((action): action is Extract<GameAction, { type: "chooseEffectOption" }> => action.type === "chooseEffectOption" && action.effectId === pending.id && action.option === 0);
}

export function deterministicMajorActionStart(state: GameState, legalActions: GameAction[]): Extract<GameAction, { type: "beginMajorAction" }> | undefined {
  if (state.pendingEffects[0]?.effect.type !== "majorAction" || state.actionFlow) return undefined;
  const starts = legalActions.filter((action): action is Extract<GameAction, { type: "beginMajorAction" }> => action.type === "beginMajorAction");
  return starts.length === 1 ? starts[0] : undefined;
}
