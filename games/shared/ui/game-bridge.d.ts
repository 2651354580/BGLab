export as namespace BGLabGameBridge;

export type BridgeAdapter = {
  dispatch(decisionId: string, transaction: unknown): {ok?: boolean; [key: string]: unknown};
  snapshot(): Record<string, any>;
  view(pid: number): Record<string, any>;
  finalResult?(): unknown;
};

export type BridgeFrontend = {
  bridgeReady?(): void;
  pause?(message: string): void;
  flushConfirmedAdapterEvents?(): boolean;
};

export type BridgeDependency<T> = T | (() => T | undefined);

export type GameBridgeOptions = {
  adapter: BridgeDependency<BridgeAdapter>;
  frontend?: BridgeDependency<BridgeFrontend>;
  WebSocketImpl?: typeof WebSocket;
  requestTimeoutMs?: number;
  reconnectInitialMs?: number;
  reconnectMaxMs?: number;
  deferEventsDuringValidation?: boolean;
  onConfirmed?: ((message: Record<string, any>, bridge: GameBridge) => boolean | void) | null;
};

export type GameBridgeResponse = {
  decisionSource?: 'host_fallback';
  fallback?: {decisionId: string; reason: string; source: string};
  retry?: boolean;
  content?: string;
  transaction?: unknown;
  adapterCommitted?: boolean;
  confirmedTurnId?: string | null;
  confirmedStateHash?: string | null;
};

export type GameBridge = {
  _ws: WebSocket | null;
  _ready: boolean;
  _pending: unknown;
  _validationInFlight: boolean;
  _retries: number;
  _reconnectTimer: ReturnType<typeof setTimeout> | null;
  _closed: boolean;
  close(): void;
  validationInFlight(): boolean;
  trace(event: string, details?: Record<string, unknown>): void;
  init(): boolean;
  persist(): boolean;
  requestAITurn(pid: number): Promise<GameBridgeResponse>;
  send(payload: Record<string, unknown>): boolean;
  onMessage(event: MessageEvent<string>): Promise<void>;
};

export function createGameBridge(options: GameBridgeOptions): GameBridge;
