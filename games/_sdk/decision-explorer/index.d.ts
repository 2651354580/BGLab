export type ContinuationStatus = 'must_continue' | 'may_stop' | 'boundary';
export type BoundaryReason = 'turn_passed' | 'game_finished' | 'new_information' | 'other_actor';
export declare function canonicalFingerprint(value: unknown): string;
export declare function makeRootId(decisionId: string, actionFingerprint: string): string;
export declare function makeProgramId(decisionId: string, snapshotFingerprint: string, stepFingerprint: string): string;
export declare function createDecisionExplorer<State, Action, Event>(initialState: State, seat: number, hooks: Record<string, unknown>, limits: { maxNodes: number; maxTimeMs: number; pageSize: number }): { outcomeIndex(request?: Record<string, unknown>): Record<string, unknown>; enumerateRoutes(request?: Record<string, unknown>): Record<string, unknown> };
export declare function createFiniteProgramExplorer(options: { decisionId: string; snapshot: unknown; programs: () => Iterable<Record<string, unknown>>; pageSize?: number }): { outcomeIndex(): Record<string, unknown>; enumerateRoutes(request?: Record<string, unknown>): Record<string, unknown> };

export type DecisionFrame = { gameId: string; decisionId: string; seat: number; stateHash: string };
export type DecisionMapProgram = Record<string, unknown> & { steps: unknown[] };
export type DecisionMapSourceContext = { frame: DecisionFrame; proposal: Record<string, unknown>; prefix: unknown[]; page: number; pageSize: number };
export type DecisionMapSourceResult = Iterable<DecisionMapProgram> | { programs?: Iterable<DecisionMapProgram>; coverageStatus?: 'unknown' | 'complete' };
export declare function createDecisionMap(options: {
  frame: DecisionFrame;
  programs: Iterable<DecisionMapProgram> | ((context: DecisionMapSourceContext) => DecisionMapSourceResult);
  pageSize?: number;
  limits?: { maxNodes?: number; maxTimeMs?: number };
  eager?: boolean;
}): {
  enumerateRoutes(request?: { proposal?: Record<string, unknown>; page?: number; pageSize?: number }): Record<string, unknown>;
  issueComplete(program: DecisionMapProgram & { complete?: true; outcome?: Record<string, unknown> }): Record<string, unknown>;
  snapshot(): Record<string, unknown>;
  replaceFrame(frame: DecisionFrame): Record<string, unknown>;
  commit(programId: string): Record<string, unknown>;
  stop(): { released: true };
  serializeIssuedMappings(): Record<string, unknown>;
  restoreIssuedMappings(serialized: Record<string, unknown>, options: { revalidate: (steps: unknown[], mapping: Record<string, unknown>) => boolean }): Record<string, unknown>;
};

export declare function createAuthorityGateway(options: {
  currentIdentity: () => { decisionId: string; seat: number };
  finiteExplorerForSeat: (seat: number) => {
    outcomeIndex(request?: Record<string, unknown>): Record<string, unknown>;
    enumerateRoutes(request?: Record<string, unknown>): Record<string, unknown>;
  };
  programsForSeat?: (seat: number) => Iterable<DecisionMapProgram>;
  decisionMapFactory?: (context: {
    frame: DecisionFrame;
    seat: number;
    pageSize: number;
    finiteExplorer: {
      outcomeIndex(request?: Record<string, unknown>): Record<string, unknown>;
      enumerateRoutes(request?: Record<string, unknown>): Record<string, unknown>;
    };
  }) => {
    enumerateRoutes(request?: { proposal?: Record<string, unknown>; page?: number; pageSize?: number }): Record<string, unknown>;
    stop(): { released: boolean };
  };
  decisionMapPageSize?: number;
}): {
  outcomeIndex(request?: Record<string, unknown>): Record<string, unknown>;
  enumerateRoutes(request?: Record<string, unknown> | null): Record<string, unknown>;
  invalidate(): { released: boolean };
};
