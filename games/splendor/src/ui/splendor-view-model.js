(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.BGLabSplendorViewModel = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const COLORS = ['C', 'S', 'E', 'R', 'O'];
  const ALL_GEMS = [...COLORS, 'G'];
  const clone = value => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
  const cardSprite = cardId => ((Number(cardId) - 1) % 5) + 1;
  const nobleSprite = nobleId => Number(nobleId) % 12;

  function score(snapshot, player) {
    return player.boughtCards.reduce((sum, owned) => sum + Number(snapshot.carddb[owned.id]?.points || 0), 0)
      + player.boughtNobles.length * 3;
  }

  function bonuses(snapshot, player) {
    const result = Object.fromEntries(COLORS.map(color => [color, 0]));
    for (const owned of player.boughtCards) {
      const card = snapshot.carddb[owned.id];
      const color = card && COLORS[card.type];
      if (color) result[color] += 1;
    }
    return result;
  }

  function mapCard(snapshot, owned, source) {
    const card = snapshot.carddb[owned.id];
    if (!card) return null;
    return {
      id:card.id,
      level:card.lvl,
      color:COLORS[card.type],
      points:Number(card.points || 0),
      cost:clone(card.cost),
      sprite:cardSprite(card.id),
      source,
    };
  }

  function mapNoble(noble) {
    return {...clone(noble), sprite:nobleSprite(noble.id)};
  }

  function build(snapshot, authorizedView, config = {}, draft = {steps:[]}) {
    const viewerSeat = Number.isInteger(config.viewerSeat) ? config.viewerSeat : null;
    const players = snapshot.playerstorage.map((player, seat) => ({
      seat,
      name:config.names?.[seat] || `P${seat}`,
      kind:config.playerTypes?.[seat] === 'ai' ? 'AI' : '玩家',
      score:score(snapshot, player),
      tokens:Object.fromEntries(ALL_GEMS.map(color => [color, Number(player[color] || 0)])),
      bonuses:bonuses(snapshot, player),
      purchasedCount:player.boughtCards.length,
      reserved:seat === viewerSeat ? player.storedCards.map(card => mapCard(snapshot, card, 'reserved')).filter(Boolean) : [],
      reservedCount:player.storedCards.length,
      nobles:player.boughtNobles.map(mapNoble),
    }));

    return {
      viewerSeat,
      currentPlayer:snapshot.wrapper.currentPlayer,
      turn:snapshot.wrapper.turn,
      phase:snapshot.wrapper.phase,
      winner:snapshot.wrapper.winner,
      winners:Array.isArray(snapshot.wrapper.winners)
        ? clone(snapshot.wrapper.winners)
        : (Number.isInteger(snapshot.wrapper.winner) ? [snapshot.wrapper.winner] : []),
      remainingTurns:snapshot.wrapper.remainingTurns,
      supply:Object.fromEntries(ALL_GEMS.map(color => [color, Number(snapshot.gamestorage[color] || 0)])),
      decks:Object.fromEntries([1, 2, 3].map(level => [level, Number(snapshot.gamestorage.drawcounts[level] || 0)])),
      market:Object.fromEntries([1, 2, 3].map(level => [level, snapshot.gamestorage.cards
        .filter(card => card.location === `market_${level}`)
        .map(card => mapCard(snapshot, card, 'market'))
        .filter(Boolean)])),
      nobles:snapshot.gamestorage.nobles.map(mapNoble),
      players,
      draft:clone(draft),
      decisionId:authorizedView?.decisionId || snapshot.decisionId || null,
    };
  }

  return Object.freeze({COLORS, ALL_GEMS, cardSprite, nobleSprite, build});
});
