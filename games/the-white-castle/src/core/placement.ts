import type { Die, Workspace } from "./types";

/** Well dice never replace its printed reference. Other spaces use the top die. */
export function placementReferenceValue(workspace: Workspace): number {
  return workspace.kind === "well" ? workspace.printedValue
    : workspace.dice.at(-1)?.value ?? workspace.printedValue;
}

/** Evaluate before adding the new die; shared by legality, payment and UI preview. */
export function placementCoinDelta(workspace: Workspace, die: Die): number {
  return die.value - placementReferenceValue(workspace);
}
