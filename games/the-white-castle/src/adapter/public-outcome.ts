import { RESOURCE_CAPS } from "../core/game";

/** Presentation of already computed, public Authority facts; never runs rules. */
const effectNames:Record<string, string> = {
  coins:"钱", seals:"印", food:"食", iron:"铁", pearl:"珍", influence:"影响", points:"分",
  courtier:"家臣", gardener:"园丁", warrior:"武士",
};

/** Lossless abbreviation of the package's existing public-effect vocabulary. */
function compactEffects(value:string):string {
  return value
    .replace(/make (\d+) resource selections from (\[[^\]]*\]); each selection gains exactly 1 chosen resource; the same resource may be selected again/g,
      (_match, count:string, resources:string) => `选${count}次${resources}(每次+1，可重复)`)
    .replace(/gain (-?\d+) (coins|seals|food|iron|pearl|influence|points)\b/g,
      (_match, amount:string, resource:string) => `+${effectNames[resource]}${amount}`)
    .replace(/pay (\d+) (coins|seals|food|iron|pearl)\b/g,
      (_match, amount:string, resource:string) => `付${effectNames[resource]}${amount}`)
    .replace(/start one (courtier|gardener|warrior) action/g,
      (_match, member:string) => `${effectNames[member]}行动`)
    .replace(/optional payment: /g, "可选：")
    .replace(/ to unlock /g, "后执行")
    .replace(/; decline = skip every unlocked effect/g, "；可跳过全部")
    .replace(/resolve the current lantern reward/g, "当前灯笼")
    .replace(/resolve the current well rewards/g, "当前水井奖励")
    .replace(/choose one currently available personal-board row/g, "选一条当前可用个人行")
    .replace(/choose one castle card row with die-color=(black|white|coral)/g, "选一条骰色为$1的城堡卡行")
    .replace(/choose one light-tone castle card row \(any die color\)/g, "选一条浅色城堡卡行（骰色不限）")
    .replace(/choose any castle card row/g, "选任一城堡卡行")
    .replace(/choose one (\w+) castle card row/g, "选一条$1城堡卡行")
    .replace(/nothing further/g, "无")
    .replace(/, then /g, "→");
}

function structuralChange(before:unknown, after:unknown):string {
  // This is the complete resulting configuration, not another reward chain
  // being executed now. Do not require the reader to reconstruct shared gains.
  const newText = typeof after === "string" ? after : "";
  return `结算后完整配置[${compactEffects(newText) || "无"}]`;
}

export function renderPublicOutcome(outcome: Record<string, unknown>): string {
  const object = (value: unknown): Record<string, unknown> | undefined =>
    value !== null && typeof value === "object" && !Array.isArray(value)
      ? value as Record<string, unknown> : undefined;
  const numeric = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
  const atom = (value: unknown): string => {
    const text = typeof value === "string" ? value.trim().replace(/\s+/g, " ")
      : numeric(value) ? String(value) : "null";
    return [...text].length <= 48 ? text : [...text].slice(0, 47).join("") + "…";
  };
  const signed = (value: number) => value >= 0 ? `+${value}` : String(value);
  const labels: Record<string, string> = {coins:"钱", seals:"印", food:"食", iron:"铁", pearl:"珍", influence:"影响"};
  const label = (key: string) => labels[key] ?? atom(key);
  const sections: string[] = [];
  const costs = Object.entries(object(outcome.factualCosts) ?? {})
    .filter(([, amount]) => numeric(amount) && amount > 0)
    .map(([resource, amount]) => `${label(resource)}${atom(amount)}`);
  if (costs.length) sections.push("花费" + costs.join("/"));
  const gained = new Set<string>();
  const gains: string[] = [];
  for (const [resource, amount] of Object.entries(object(outcome.factualGains) ?? {})) {
    if (!numeric(amount) || amount <= 0) continue;
    gained.add(resource);
    gains.push(`${label(resource)}${atom(amount)}`);
  }
  for (const [key, resource, name] of [
    ["foodDelta", "food", "食物"], ["ironDelta", "iron", "铁"],
    ["pearlDelta", "pearl", "珍珠"], ["sealsDelta", "seals", "印玺"],
    ["influenceDelta", "influence", "影响力"],
  ]) {
    const amount = outcome[key];
    if (!gained.has(resource) && numeric(amount) && amount > 0) gains.push(`${name}${atom(amount)}`);
  }
  if (gains.length) sections.push("实际入账" + gains.join("/"));
  const immediate = outcome.immediateScoreDelta ?? outcome.scoreDelta;
  if (numeric(immediate)) sections.push(`即时${signed(immediate)}`);
  const endNow = outcome.endNowScoreDelta;
  const endScore = object(outcome.scoreIfGameEnded);
  const total = object(endScore?.after)?.total;
  if (numeric(total)) {
    sections.push("立即终局账面快照（不含未来路线价值）："
      + (numeric(endNow) ? `变化${signed(endNow)}，` : "") + `总分${atom(total)}`);
  } else if (!endScore && numeric(endNow)) {
    sections.push(`立即终局账面快照（不含未来路线价值）：变化${signed(endNow)}`);
  }
  const priority: Record<string, number> = {warrior_multiplier:0, lantern_reward:1, personal_board:2};
  const structural = Array.isArray(outcome.structuralBenefits) ? outcome.structuralBenefits : [];
  const ordered = structural.map(object).filter((item): item is Record<string, unknown> => Boolean(item))
    .sort((a, b) => (priority[String(a.kind)] ?? 3) - (priority[String(b.kind)] ?? 3));
  for (const item of ordered) {
    if (item.kind === "warrior_multiplier") sections.push(`武士终局乘数 ${atom(item.before)}→${atom(item.after)}`);
    if (item.kind === "lantern_reward") sections.push(`灯笼：${structuralChange(item.before, item.after)}`);
    if (item.kind === "personal_board") sections.push(`个人板${effectNames[String(item.row)] ?? atom(item.row)}：${structuralChange(item.before, item.after)}`);
  }
  const remaining = object(outcome.remaining);
  const resources = Object.entries(object(remaining?.resources) ?? {})
    .filter(([, amount]) => numeric(amount))
    .map(([resource, amount]) => {
      const cap = RESOURCE_CAPS[resource as keyof typeof RESOURCE_CAPS];
      return `${label(resource)}${atom(amount)}`
        + (cap !== undefined && amount === cap ? `（上限${cap}）` : "");
    });
  if (resources.length) sections.push("余" + resources.join("/"));
  if (numeric(remaining?.influence)) sections.push(`余影响${atom(remaining.influence)}`);
  if (!sections.length) return "无额外结构摘要；以结果行的资源、分数和终态事实为准。";
  const rendered = sections.join("；") + "。";
  return rendered;
}
