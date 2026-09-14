export as namespace BGLabGameShell;

export type InteractionState =
  | "idle" | "source_selectable" | "source_selected"
  | "target_selectable" | "effect_pending" | "confirmation_pending"
  | "resolving" | "waiting_next_decision" | "finished" | "error";

export type GameProgressItem = {
  id: string;
  label: string;
  value: string | number;
  tone?: "default" | "positive" | "warning" | "danger";
};

export type GameField = {
  id: string;
  label: string;
  value: string | number;
  secondaryValue?: string | number;
  icon?: string;
  tone?: "default" | "positive" | "warning" | "danger";
};

export type GameFieldGroup = {
  id: string;
  label: string;
  fields: GameField[];
  collapsible?: boolean;
};

export type GamePlayerSummary = {
  seat: number;
  name: string;
  kind: "human" | "ai";
  isViewer: boolean;
  isActive: boolean;
  score: number | null;
  statusLabel?: string;
  gameFields: GameFieldGroup[];
};

export type GameTimelineEntry = {
  id: string;
  turnId?: string;
  actor?: {seat: number; name: string} | null;
  kind: "choice" | "payment" | "gain" | "movement" | "score" | "phase" | "error";
  summary: string;
  icon?: string;
  valueDelta?: {before?: number; after?: number; delta?: number};
  children?: GameTimelineEntry[];
};

export type GameInteractionView = {
  state: InteractionState;
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
  error?: {code: string; message: string};
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
  players: GamePlayerSummary[];
  interaction: GameInteractionView;
  timeline: GameTimelineEntry[];
  finalResult?: {
    columns?: string[];
    title: string;
    winnerSeats: number[];
    summary: string;
    rows: Array<{id: string; label: string; values: number[]}>;
  };
};

export type GameShellSlots = {
  turnSurfaceHtml?: string;
  boardHtml?: string;
  /** @deprecated Use actionPrimaryHtml for ordered action rails. */
  actionHtml?: string;
  actionPrimaryHtml?: string;
  actionAuxiliaryHtml?: string;
  actionRollbackHtml?: string;
  overlayHtml?: string;
};

export function normalizeView(view: GameShellView): GameShellView;
export function renderTopBar(view: GameShellView): string;
export function renderSidebar(view: GameShellView): string;
export function renderTimeline(entries: GameTimelineEntry[]): string;
export function renderActionDock(interaction: GameInteractionView): string;
export function renderShell(view: GameShellView, slots?: GameShellSlots): string;
export function bindActions(
  rootElement: ParentNode,
  handlers: Partial<Record<"cancel" | "skip" | "confirm", (event: Event, action: string) => void>>,
): void;
