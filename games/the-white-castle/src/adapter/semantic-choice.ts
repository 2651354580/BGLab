export type EngineRouteStep = {
  op:string;
  [key:string]:unknown;
};

export function semanticChoiceKindForEngineStep(
  step:EngineRouteStep,
  choiceType?:string,
):string | null {
  if (step.op === "refreshCastleRoom") return "refresh_castle_room";
  if (step.op !== "chooseEffectOption") return null;
  if (choiceType === "pay") return "optional_payment";
  if (choiceType === "influence") return "influence_checkpoint";
  if (choiceType === "actionOrder") return "action_order";
  if (choiceType === "domainAction") return "personal_row_choice";
  const id = String(step.effectId ?? "");
  const orderedKinds:Array<[RegExp, string]> = [
    [/^setup-p[0-9]+-resources$/, "starting_resource"],
    [/^daimyo-lantern-.*$/, "daimyo_lantern"],
    [/^(?:turn-[0-9]+-)?lantern-.*$/, "lantern"],
    [/^daimyo-reward-.*$/, "daimyo_reward"],
    [/^round-.*-gardens-p[0-9]+(?:-remaining)*$/, "round_end_garden_order"],
    [/^round-.*-gardens-p[0-9]+(?:-remaining)*-garden-.*-[0-9]+$/, "garden_resource"],
    [/^(?:turn-[0-9]+-)?well-.*$/, "well_resource"],
    [/^arrival-.*$/, "arrival_reward"],
    [/^gain(?:-.*)?$/, "resource_choice"],
    [/^pay-.*$/, "payment"],
    [/^turn-[0-9]+-p[0-9]+-domain-warrior-[0-9]+(?:-after-[0-9]+)*$/, "influence_checkpoint"],
    [/^(?:ordered(?:-remaining-order)*|threshold|first-[0-9]+|second-[0-9]+)$/, "effect_order"],
    [/^turn-.*-castle-.*-actions$/, "castle_action_order"],
    [/^turn-[0-9]+-outside-.*$/, "outside_action"],
    [/^pay$/, "optional_payment"],
  ];
  const idKind = orderedKinds.find(([pattern]) => pattern.test(id))?.[1];
  if (idKind) return idKind;
  if (choiceType === "gainChoice") return "resource_choice";
  return null;
}
