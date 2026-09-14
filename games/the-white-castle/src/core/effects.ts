import { MaterialEffect } from "./material";
import { Effect } from "./types";

export function materialEffects(effects: readonly MaterialEffect[]): Effect[] {
  return effects.map(materialEffect);
}

type MaterialEffectOf<T extends MaterialEffect["type"]> = Extract<MaterialEffect, { type: T }>;

function gainReward(effect: MaterialEffectOf<"gain">): Effect {
  if (effect.resource === "points") return { type: "gainPoints", amount: effect.amount };
  if (effect.resource === "influence") return { type: "influence", amount: effect.amount };
  return { type: "gain", resource: effect.resource, amount: effect.amount };
}

function resourceChoiceReward(effect: MaterialEffectOf<"choiceResource">): Effect {
  return { type: "gainChoice", resources: ["food", "iron", "pearl"], amount: effect.amount };
}

function majorActionReward(effect: MaterialEffectOf<"majorAction">): Effect { return effect; }
function lanternReward(_effect: MaterialEffectOf<"lantern">): Effect { return { type: "lantern" }; }
function wellReward(_effect: MaterialEffectOf<"well">): Effect { return { type: "wellAction" }; }
function personalDomainRowReward(_effect: MaterialEffectOf<"domain">): Effect { return { type: "domainAction" }; }
function castleDieTileReward(effect: MaterialEffectOf<"castleTile">): Effect { return { type: "castleTileAction", color: effect.color }; }
function optionalPaymentReward(effect: MaterialEffectOf<"pay">): Effect {
  return { type: "pay", resource: effect.resource, amount: effect.amount, effects: materialEffects(effect.then), optional: true };
}

const rewardHandlers = {
  gain: gainReward,
  choiceResource: resourceChoiceReward,
  majorAction: majorActionReward,
  lantern: lanternReward,
  well: wellReward,
  domain: personalDomainRowReward,
  castleTile: castleDieTileReward,
  pay: optionalPaymentReward,
};

export function materialEffect(effect: MaterialEffect): Effect {
  const handler = rewardHandlers[effect.type] as (value: MaterialEffect) => Effect;
  return handler(effect);
}
