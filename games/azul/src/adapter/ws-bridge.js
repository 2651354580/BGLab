/** Azul's bridge policy; transport and authority confirmation are shared. */
const Bridge = window.BGLabGameBridge.createGameBridge({
  adapter: () => window.BGLabGameAdapter,
  frontend: () => window.BGLabFrontend,
});

window.Bridge = Bridge;
