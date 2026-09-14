(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.BGLabSplendorScoringFrame = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function buildScoringDecisionFacts({engine, state, seat, gems, canAfford}) {
    const player = state.playerstorage[seat];
    const colors = gems.slice(0, 5);
    const cardFacts = (cards, source) => cards.map(card => ({
      id:card.id,
      label:`card-${card.id}`,
      source,
      costs:structuredClone(card.cost),
      printedPoints:card.points,
      immediateEffects:[{type:'permanent_bonus', color:gems[card.type]}],
    }));
    const marketCards = state.gamestorage.cards.map(card => engine.carddb[card.id]);
    const reservedCards = player.storedCards.map(card => engine.carddb[card.id]);
    const cardPoints = player.boughtCards.reduce(
      (total, card) => total + (engine.carddb[card.id]?.points || 0),
      0,
    );
    const scoreFacts = {
      currentTotal:engine.score(state, seat),
      components:[
        {id:'cards', label:'card points', value:cardPoints},
        {id:'nobles', label:'noble points', value:player.boughtNobles.length * 3},
      ],
      rules:[
        {ruleId:'card-points', timing:'immediate', formula:'sum(purchased_card.printedPoints)', dependencies:['purchasedCards.printedPoints'], workedExamples:['buying cards worth 1 and 3 gives 4 points']},
        {ruleId:'noble-points', timing:'after_purchase', formula:'each acquired noble = 3', dependencies:['permanentBonuses','noble.cost'], workedExamples:['one acquired noble = 3 points']},
      ],
    };
    const scoringTarget = (card, source) => {
      const bonuses = engine.bonuses(state, seat);
      const rawCost = Object.fromEntries(
        colors.map(color => [color, Number(card.cost[color] || 0)]),
      );
      const effectiveCost = Object.fromEntries(colors.map(color => [
        color,
        Math.max(0, rawCost[color] - Number(bonuses[color] || 0)),
      ]));
      const spendable = Object.fromEntries(
        gems.map(color => [color, Number(player[color] || 0)]),
      );
      const remainingGap = Object.fromEntries(colors.map(color => [
        color,
        Math.max(0, effectiveCost[color] - spendable[color]),
      ]));
      const goldRequired = Object.values(remainingGap)
        .reduce((total, amount) => total + amount, 0);
      const bonusColor = gems[card.type];
      const bonusesAfter = {
        ...bonuses,
        [bonusColor]:Number(bonuses[bonusColor] || 0) + 1,
      };
      const nobleProgressAfter = state.gamestorage.nobles.map(noble => ({
        id:noble.id,
        printedPoints:3,
        remainingBonusGap:Object.fromEntries(colors.map(color => [
          color,
          Math.max(
            0,
            Number(noble.cost[color] || 0) - Number(bonusesAfter[color] || 0),
          ),
        ])),
      }));
      const nobleUnlocked = nobleProgressAfter.some(noble => (
        Object.values(noble.remainingBonusGap).every(amount => amount === 0)
      ));
      const nobleUnlockIds = nobleProgressAfter
        .filter(noble => Object.values(noble.remainingBonusGap).every(amount => amount === 0))
        .map(noble => noble.id);
      const contestedBy = source === 'market'
        ? state.playerstorage
          .map((_candidate, pid) => pid)
          .filter(pid => pid !== seat && canAfford(pid, card))
        : [];
      return {
        id:String(card.id),
        actionFamily:'buy_card',
        source,
        printedPoints:Number(card.points || 0),
        rawCost,
        effectiveCost,
        spendable,
        remainingGap,
        goldRequired,
        affordableNow:goldRequired <= spendable.G,
        immediateScoreDelta:Number(card.points || 0) + (nobleUnlocked ? 3 : 0),
        endNowScoreDelta:Number(card.points || 0) + (nobleUnlocked ? 3 : 0),
        bonusColor,
        scoringStructureChanges:[{
          ruleId:'permanent-discount',
          color:bonusColor,
          before:Number(bonuses[bonusColor] || 0),
          after:bonusesAfter[bonusColor],
        }],
        ...(nobleUnlockIds.length ? {nobleUnlockIds} : {}),
        ...(contestedBy.length ? {publicContention:{
          opponentsAbleToTake:contestedBy,
        }} : {}),
      };
    };
    const scoringTargets = [
      ...marketCards.map(card => scoringTarget(card, 'market')),
      ...reservedCards.map(card => scoringTarget(card, 'reserved')),
    ];
    const currentBonuses = engine.bonuses(state, seat);
    const nobleFacts = state.gamestorage.nobles.map(noble => ({
      id:noble.id,
      printedPoints:3,
      requiredBonuses:structuredClone(noble.cost),
      currentBonuses:Object.fromEntries(
        colors.map(color => [color, Number(currentBonuses[color] || 0)]),
      ),
      remainingBonusGap:Object.fromEntries(colors.map(color => [
        color,
        Math.max(
          0,
          Number(noble.cost[color] || 0) - Number(currentBonuses[color] || 0),
        ),
      ])),
    }));
    return {
      cardFacts,
      marketCards,
      reservedCards,
      colors,
      cardPoints,
      scoreFacts,
      scoringTargets,
      currentBonuses,
      nobleFacts,
      scoringDecisionFacts:{
        contractVersion:'scoring-decision-facts-v2',
        score:scoreFacts,
        endCondition:{
          description:'Reaching at least 15 points starts the final round; highest score wins, then fewer purchased cards.',
          targetScore:15,
          remaining:{
            points:Math.max(0, 15 - engine.score(state, seat)),
            turns:state.wrapper.remainingTurns,
          },
          tieBreakers:['fewer purchased cards'],
        },
        scoringTargets,
        nobles:nobleFacts,
        currentState:{
          prestige:engine.score(state, seat),
          distanceToEnd:Math.max(0, 15 - engine.score(state, seat)),
          tokens:Object.fromEntries(
            gems.map(color => [color, Number(player[color] || 0)]),
          ),
          permanentBonuses:structuredClone(currentBonuses),
          purchasedCardCount:player.boughtCards.length,
          reservedCardCount:player.storedCards.length,
        },
        opponents:state.playerstorage
          .map((_candidate, pid) => ({
            seat:pid,
            currentTotal:engine.score(state, pid),
            distanceToEnd:Math.max(0, 15 - engine.score(state, pid)),
            purchasedCardCount:state.playerstorage[pid].boughtCards.length,
          }))
          .filter(item => item.seat !== seat),
      },
    };
  }

  // Public, conditional one-purchase consequences. No deck order, opponent
  // reserved identity, opponent intent or strategic ranking enters this fact.
  function buildPublicPurchaseHorizon({engine, state, seat, gems, canAfford}) {
    if (state.wrapper.phase !== 'playing' || state.wrapper.remainingTurns != null) return null;
    const colors = gems.slice(0, 5);
    const witnesses = [];
    const actorCanReserve = state.playerstorage[seat].storedCards.length < 3;
    for (let opponent = 0; opponent < state.playerstorage.length; opponent++) {
      if (opponent === seat) continue;
      const bonuses = engine.bonuses(state, opponent);
      const before = engine.score(state, opponent);
      for (const reference of state.gamestorage.cards) {
        const card = engine.carddb[reference.id];
        if (!canAfford(opponent, card)) continue;
        const color = gems[card.type];
        const bonusesAfter = {...bonuses, [color]:Number(bonuses[color] || 0) + 1};
        const nobles = state.gamestorage.nobles.filter(noble => colors.every(
          gem => Number(bonusesAfter[gem] || 0) >= Number(noble.cost[gem] || 0),
        )).map(noble => noble.id);
        const after = before + Number(card.points || 0) + (nobles.length ? 3 : 0);
        if (after < 15) continue;
        const remaining = Array.from(
          {length:state.wrapper.playerCount - opponent - 1}, (_, index) => opponent + index + 1,
        );
        let text = `玩家${opponent}若用当前资源购买仍在市场的卡${card.id}：卡牌得${Number(card.points || 0)}分`;
        if (nobles.length) text += `，满足当前贵族${JSON.stringify(nobles)}之一再得3分`;
        text += `，分数${before}→${after}。若因此开始终局，随后本轮还行动的座位是${JSON.stringify(remaining)}；`;
        text += remaining.includes(seat)
          ? `玩家${seat}仍有本轮一次行动。` : `玩家${seat}本局不再行动。`;
        const methods = [];
        if (canAfford(seat, card)) methods.push('购买');
        if (actorCanReserve) methods.push('保留（不支付该牌费用；持有宝石超过10枚仍须弃牌）');
        text += `玩家${seat}本回合移走该牌的可用方式：${methods.length ? methods.join('、') : '无'}。`;
        if (methods.length) text += `移走后玩家${opponent}不能再买这张卡；新补牌未知，不能由此保证阻断全部后续得分。`;
        witnesses.push(text);
      }
    }
    return witnesses.length ? {
      kind:'DirectOutcomeFact',
      id:'public-purchase-horizon',
      title:'当前公开购买的终局后果与市场变化',
      data:{
        scope:'只描述当前可见牌、当前资源与贵族的条件结果。对手能力不是行动预测；牌和贵族必须保持可用，未知补牌不在这些结果里。',
        witnesses,
      },
    } : null;
  }

  return {buildScoringDecisionFacts, buildPublicPurchaseHorizon};
});
