export interface BglabActionRecord {
  decisionId: string;
  turnId: string;
  turn: number | null;
  actor: number | null;
  action: unknown;
  events: unknown[];
  outcome: unknown;
  order: number;
  text: string;
}

export function records(snapshot: unknown, formatter?: (record: BglabActionRecord) => string, limit?: number): BglabActionRecord[];
export function merge(existing: BglabActionRecord[], incoming: BglabActionRecord[], limit?: number): BglabActionRecord[];
export function committedMap(snapshot: unknown): Record<string, unknown>;
