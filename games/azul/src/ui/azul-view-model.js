(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.BGLabAzulViewModel = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  const boardAsset = () => 'assets/img/playerboard1.jpg';
  const CALIBRATION = Object.freeze({
    pattern:Object.freeze({x:2, y:-3.5, gapX:7, gapY:10, tileSize:56}),
    wall:Object.freeze({x:9, y:-5, gapX:4, gapY:6, tileSize:59}),
    floor:Object.freeze({x:-1.5, y:12.5, gapX:14, gapY:14, tileSize:55}),
  });

  function build(snapshot, authorizedView, config = {}, draft = []) {
    const game = snapshot.game;
    const viewerSeat = Number.isInteger(config.viewerSeat) ? config.viewerSeat : null;
    return {
      viewerSeat,
      currentPlayer:game.currentPlayer,
      round:game.round,
      turn:game.turn,
      phase:game.phase,
      winner:game.winner ?? null,
      winners:clone(game.winners || []),
      firstPlayerTokenAvailable:Boolean(game.firstPlayerTokenAvailable),
      factories:game.factories.map((tiles, factoryIndex) => ({
        factoryIndex,
        tiles:tiles.map((color, tileIndex) => ({id:`factory-${factoryIndex}-${tileIndex}`, color})),
      })),
      center:game.center.map((color, tileIndex) => ({id:`center-${tileIndex}`, color})),
      players:game.players.map((player, seat) => ({
        seat,
        name:config.names?.[seat] || player.name || `P${seat}`,
        kind:config.playerTypes?.[seat] === 'ai' ? 'AI' : '玩家',
        score:Number(player.score || 0),
        boardAsset:boardAsset(),
        patternLines:clone(player.patternLines),
        wall:clone(player.wall),
        floorLine:clone(player.floor),
      })),
      calibration:clone(CALIBRATION),
      draft:clone(draft),
      decisionId:authorizedView?.decisionId || snapshot.decisionId || null,
    };
  }

  return Object.freeze({boardAsset, CALIBRATION, build});
});
