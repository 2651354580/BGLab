/** Splendor's bridge policy; transport and authority confirmation are shared. */
const getFrontend = () => window.BGLabFrontend;

const Bridge = window.BGLabGameBridge.createGameBridge({
  adapter: () => window.BGLabGameAdapter,
  frontend: getFrontend,
  onConfirmed: () => getFrontend()?.flushConfirmedAdapterEvents?.() || false,
});

window.Bridge = Bridge;
