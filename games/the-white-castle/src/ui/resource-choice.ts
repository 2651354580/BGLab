import type { CappedResource } from "../core";

export interface ResourceChoiceDraft {
  effectId: string;
  total: number;
  picks: CappedResource[];
}

export function addResourcePick(draft: ResourceChoiceDraft, resource: CappedResource): ResourceChoiceDraft {
  if (draft.picks.length >= draft.total) return draft;
  return { ...draft, picks: [...draft.picks, resource] };
}

export function isResourceChoiceComplete(draft: ResourceChoiceDraft): boolean {
  return draft.picks.length === draft.total;
}
