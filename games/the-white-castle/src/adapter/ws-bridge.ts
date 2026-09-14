/** White Castle's bridge policy; transport and authority confirmation are shared. */
import type { GameBridge } from "../../../shared/ui/game-bridge";
import type { AdapterSnapshot } from "./bglab-adapter";

const getFrontend = () => window.BGLabFrontend;

export const Bridge: GameBridge = window.BGLabGameBridge!.createGameBridge({
  adapter: () => window.BGLabGameAdapter,
  frontend: getFrontend,
});

declare global {
  interface Window {
    BG_GAME_ID?: string;
    BG_REPLAY_MODE?: boolean;
    BGLabGameBridge: typeof import("../../../shared/ui/game-bridge");
    Bridge?: typeof Bridge;
    BGLabFrontend?: {
      start: (config?: any) => void;
      restore: (snapshot: AdapterSnapshot, config?: any) => void;
      bridgeReady?: () => void;
      pause: (message: string) => void;
      status: () => Record<string, unknown>;
    };
  }
}

if (typeof window !== "undefined") window.Bridge = Bridge;
