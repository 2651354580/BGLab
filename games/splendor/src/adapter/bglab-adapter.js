(function (root, factory) {
  const core = typeof module === 'object' && module.exports
    ? require('../core/splendor-engine.js')
    : root.BGLabSplendorCore;
  const decisionExplorer = typeof module === 'object' && module.exports
    ? require('../../../_sdk/decision-explorer/index.cjs')
    : root.BGLabDecisionExplorer;
  const scoringFrame = typeof module === 'object' && module.exports
    ? require('./scoring-frame.js')
    : root.BGLabSplendorScoringFrame;
  const actionFrame = typeof module === 'object' && module.exports
    ? require('./action-frame.js')
    : root.BGLabSplendorActionFrame;
  const publicOutcome = typeof module === 'object' && module.exports
    ? require('./public-outcome.js') : root.BGLabSplendorPublicOutcome;
  const api = factory(core, decisionExplorer, scoringFrame, actionFrame, publicOutcome);
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && core) root.BGLabGameAdapter = new api.SplendorBGLabAdapter();
})(typeof globalThis !== 'undefined' ? globalThis : this, function (core, decisionExplorer, scoringFrame, actionFrame, publicOutcome) {
  'use strict';

  if (!core || !core.SplendorEngine) throw new Error('BGLab Splendor core must load before the adapter');
  if (!scoringFrame) throw new Error('Splendor scoring Frame must load before the adapter');
  if (!actionFrame) throw new Error('Splendor action Frame must load before the adapter');
  const GEMS = ['C', 'S', 'E', 'R', 'O', 'G'];
  const same = (left, right) => JSON.stringify(left) === JSON.stringify(right);

  function recentMarketChanges(state, seat) {
    const recent = [];
    for (let index = state.history.actions.length - 1; index >= 0; index--) {
      const entry = state.history.actions[index];
      const actor = Number(entry.decisionId.split(':')[1]);
      if (actor === seat) break;
      recent.push({actor, steps:entry.chain.steps});
    }
    const changes = [];
    for (const {actor, steps} of recent.reverse()) {
      const action = steps.find(step => step.op === 'begin')?.action;
      if (action !== 'buy_card' && action !== 'reserve_card') continue;
      for (const step of steps) {
        // Validated public selections only. A blind reservation's select_deck
        // has no visible card identity; never inspect its private outcome.
        if (step.op === 'select_card' && step.source === 'market') {
          changes.push({seat:actor, action, cardId:step.cardId, source:'market'});
        }
      }
    }
    return changes.length ? {
      kind:'DirectOutcomeFact', id:'recent-market-changes',
      title:'自你上次行动后，公开市场发生的变化',
      data:{
        scope:'这些是已经发生的公开操作；被移走的牌已不在市场。当前可购买或保留的市场牌见本次市场列表。盲抽保留牌的身份不在这些事实中。',
        changes,
      },
    } : null;
  }

  function discardDistributions(available, count, index = 0, current = {}, result = []) {
    if (index === GEMS.length) {
      if (count === 0) result.push({...current});
      return result;
    }
    const color = GEMS[index];
    for (let amount = 0; amount <= Math.min(count, available[color] || 0); amount += 1) {
      if (amount) current[color] = amount;
      else delete current[color];
      discardDistributions(available, count - amount, index + 1, current, result);
    }
    delete current[color];
    return result;
  }

  class SplendorBGLabAdapter {
    constructor(engine = new core.SplendorEngine()) {
      this.protocolVersion = 2;
      this.snapshotVersion = 7;
      this.engine = engine;
      this._authorityGateway = null;
      this._derivedCommitted = {};
    }

    start(config = {}) {
      this.engine.start(config);
      this._derivedCommitted = {};
      this._authorityGateway?.invalidate();
      return this.view(Number(config.seat || 0));
    }

    coverageCatalog() {
      return {
        actionFamilies:['take_gems','buy_card','reserve_card'],
        effectFamilies:['take_2','take_3','buy_card','reserve_card','noble_awarded','final_round_started','game_finished'],
        boundaryReasons:['turn_passed','game_finished','new_information'],
      };
    }
    strategicOpportunityCatalog() { return []; }

    _canAfford(state, candidate, card) {
      const colors = GEMS.slice(0, 5);
      const candidateBonuses = this.engine.bonuses(state, candidate);
      const goldRequired = colors.reduce((total, color) => (
        total + Math.max(
          0,
          Number(card.cost[color] || 0)
            - Number(candidateBonuses[color] || 0)
            - Number(state.playerstorage[candidate][color] || 0),
        )
      ), 0);
      return goldRequired <= Number(state.playerstorage[candidate].G || 0);
    }

    view(seat = 0) {
      const view = this.engine.view(seat);
      // Host persistence can reconstruct a human receipt from validated history
      // without adding presentation fields to the authoritative snapshot.
      const lastCommitted = this.engine.state.history.actions.at(-1);
      if (lastCommitted) {
        view.lastCommittedTransaction = {
          decisionId:lastCommitted.decisionId,
          transaction:structuredClone(lastCommitted.chain),
        };
      }
      if (seat === view.currentPlayer) {
        const state = this.engine.state;
        const player = state.playerstorage[seat];
        const visibleCardIds = new Set([
          ...state.gamestorage.cards.map(card => card.id),
          ...state.playerstorage.flatMap(candidate => candidate.boughtCards.map(card => card.id)),
          ...player.storedCards.map(card => card.id),
        ]);
        view.publicState.carddb = Object.fromEntries(
          [...visibleCardIds].map(id => [id, structuredClone(this.engine.carddb[id])]),
        );
        const canAfford = (candidate, card) => this._canAfford(state, candidate, card);
        const {
          cardFacts,
          marketCards,
          reservedCards,
          colors,
          cardPoints,
          scoreFacts,
          scoringTargets,
          currentBonuses,
          nobleFacts,
          scoringDecisionFacts,
        } = scoringFrame.buildScoringDecisionFacts({
          engine:this.engine,
          state,
          seat,
          gems:GEMS,
          canAfford,
        });
        const reserveTargets = [
          ...cardFacts(marketCards, 'market'),
          ...[1,2,3].filter(level => state.decks[level]?.length).map(level => ({
            id:`deck-${level}`, label:`deck-${level}`, source:'deck', costs:{}, printedPoints:0,
            immediateEffects:[{type:'reserve_hidden_card', level}],
          })),
        ];
        const reserveAvailable = player.storedCards.length < 3 && reserveTargets.length > 0;
        const availableColors = GEMS.slice(0, 5).filter(color => state.gamestorage[color] > 0).map(color => ({
          id:color, label:color, costs:{}, printedPoints:0,
          immediateEffects:[{type:'gain_gem', color, available:state.gamestorage[color]}],
        }));
        const normalLegalFamilies = [
          {family:'take_gems', variants:['distinct_colors', 'same_color_pair'], availableNow:availableColors.length > 0, legalTargets:availableColors, routeQuery:{stepsContain:[{op:'take_gem'}]}},
          {family:'buy_card', variants:['market', 'reserved'], availableNow:marketCards.length + player.storedCards.length > 0, legalTargets:[...cardFacts(marketCards, 'market'), ...cardFacts(reservedCards, 'reserved')], routeQuery:{stepsContain:[{op:'select_card'}]}},
          {family:'reserve_card', variants:['market', 'deck'], availableNow:reserveAvailable, legalTargets:reserveTargets, routeQuery:{stepsContain:[{op:'select_card'}]}},
        ];
        const legalFamilies = state.wrapper.phase === 'reserve_discard'
          ? [{family:'reserve_card', variants:['reserve_discard'], availableNow:true, legalTargets:[], routeQuery:{stepsContain:[{op:'discard_gem'}]}}]
          : state.wrapper.phase === 'choose_noble'
            ? [{
              family:'buy_card', variants:['choose_noble'], availableNow:true,
              legalTargets:nobleFacts.filter(noble => (state.wrapper.pendingNobles?.ids || []).includes(noble.id)),
              routeQuery:{stepsContain:[{op:'choose_noble'}]},
            }]
            : normalLegalFamilies;
        view.privateState = {
          ...view.privateState,
          decisionFacts:{
            score:scoreFacts,
            scoringDecisionFacts,
            legalFamilies,
            outcomeDimensions:[
              {id:'cardCount', label:'purchased cards', currentValue:player.boughtCards.length},
              {id:'nobleCount', label:'nobles', currentValue:player.boughtNobles.length},
              {id:'gemCount', label:'gems', currentValue:GEMS.reduce((total, color) => total + (player[color] || 0), 0)},
            ],
            actionFamilyCoverage:['take_gems', 'buy_card', 'reserve_card'].map(family => ({
              family, variants:[], coverageStatus:'bounded', observedProgramCount:0,
              presentOnReturnedPage:false, continuationAvailable:true,
            })),
          },
        };
        const formatResourceMap = values => (
          Object.entries(values)
            .map(([color, amount]) => `${color}:${amount}`)
            .join(',')
        );
        const playerTokens = candidate => Object.fromEntries(
          GEMS.map(color => [color, Number(candidate[color] || 0)]),
        );
        const playerBonusLine = pid => formatResourceMap(
          this.engine.bonuses(state, pid),
        );
        const cardLines = [
          ...marketCards.map(card => ({card, source:'market'})),
          ...reservedCards.map(card => ({card, source:'reserved'})),
        ].map(({card, source}) => {
          const target = scoringTargets.find(candidate => (
            candidate.id === String(card.id) && candidate.source === source
          ));
          return (
            `developmentCardId=${card.id}; source=${source}; tier=${card.lvl}; points=${card.points}; `
            + `bonus=${GEMS[card.type]}; printedCost=${formatResourceMap(target.rawCost)}; `
            + `payableAfterBonuses=${formatResourceMap(target.effectiveCost)}; `
            + `tokens=${formatResourceMap(target.spendable)}; `
            + `remainingGap=${formatResourceMap(target.remainingGap)}; `
            + `goldRequired=${target.goldRequired}; affordableNow=${target.affordableNow ? 'yes' : 'no'}; `
            + `printedPoints=${target.printedPoints}; `
            + `noblePointsIfBoughtNow=${Math.max(0, target.immediateScoreDelta - target.printedPoints)}; `
            + `totalImmediateScoreIfBoughtNow=${target.immediateScoreDelta}`
          );
        });
        const reserveDeckLines = [1,2,3]
          .filter(level => state.decks[level]?.length)
          .map(level => `deck-${level}:level=${level},remaining=${state.decks[level].length}`);
        const opponentLines = state.playerstorage
          .map((candidate, pid) => ({candidate, pid}))
          .filter(({pid}) => pid !== seat)
          .map(({candidate, pid}) => (
            `seat=${pid}; score=${this.engine.score(state, pid)}; `
            + `distanceTo15=${Math.max(0, 15 - this.engine.score(state, pid))}; `
            + `tokens=${formatResourceMap(playerTokens(candidate))}; `
            + `bonuses=${playerBonusLine(pid)}; purchasedCards=${candidate.boughtCards.length}; `
            + `reservedCards=${candidate.storedCards.length}`
          ));
        const contentionLines = scoringTargets
          .filter(target => target.source === 'market' && target.publicContention)
          .map(target => (
            `card=${target.id}; opponentSeatsAbleToBuyNow=[${target.publicContention.opponentsAbleToTake.join(',')}]`
          ));
        const immediatelyContested = scoringTargets
          .filter(target => target.source === 'market' && target.publicContention)
          .map(target => (
            `card ${target.id} by seats [${target.publicContention.opponentsAbleToTake.join(',')}]`
          ));
        const affordablePurchaseTargets = scoringTargets
          .filter(target => target.affordableNow)
          .map(target => target.id);
        const opponentSeats = state.playerstorage
          .map((_candidate, pid) => pid)
          .filter(pid => pid !== seat);
        const marketCardFacts = marketCards.map(card => {
          const target = scoringTargets.find(candidate => (
            candidate.id === String(card.id) && candidate.source === 'market'
          ));
          return {
            cardId:card.id,
            source:'market',
            tier:card.lvl,
            printedPoints:Number(card.points || 0),
            bonusColor:GEMS[card.type],
            printedCost:structuredClone(target.rawCost),
            payableAfterBonuses:structuredClone(target.effectiveCost),
            remainingGap:structuredClone(target.remainingGap),
            goldRequired:target.goldRequired,
            affordableNow:target.affordableNow,
            opponentsAbleToBuyNow:opponentSeats.filter(pid => canAfford(pid, card)),
          };
        });
        const ownReservedFacts = reservedCards.map(card => {
          const target = scoringTargets.find(candidate => (
            candidate.id === String(card.id) && candidate.source === 'reserved'
          ));
          return {
            cardId:card.id,
            source:'reserved',
            tier:card.lvl,
            printedPoints:Number(card.points || 0),
            bonusColor:GEMS[card.type],
            printedCost:structuredClone(target.rawCost),
            payableAfterBonuses:structuredClone(target.effectiveCost),
            remainingGap:structuredClone(target.remainingGap),
            goldRequired:target.goldRequired,
            affordableNow:target.affordableNow,
          };
        });
        const modelNobleFacts = nobleFacts.map(noble => ({
          nobleId:noble.id,
          printedPoints:noble.printedPoints,
          requiredBonuses:structuredClone(noble.requiredBonuses),
          remainingBonusGap:structuredClone(noble.remainingBonusGap),
        }));
        const modelFacts = [
          {
            kind:'TurnFact', id:'turn', title:'Current turn', data:{
              turn:state.wrapper.turn,
              phase:state.wrapper.phase,
              actorSeat:seat,
              playerCount:state.wrapper.playerCount,
              nextActorSeat:(seat + 1) % state.wrapper.playerCount,
              opponentActionsBeforeActorReturns:Math.max(0, state.wrapper.playerCount - 1),
              finalRoundRemainingTurns:state.wrapper.remainingTurns ?? null,
            },
          },
          {
            kind:'ResourceSnapshot', id:'actor', title:'Acting seat', data:{
              seat,
              tokens:playerTokens(player),
              bonuses:structuredClone(currentBonuses),
              score:this.engine.score(state, seat),
              distanceTo15:Math.max(0, 15 - this.engine.score(state, seat)),
              purchasedCardCount:player.boughtCards.length,
              reservedCount:player.storedCards.length,
              reservedLimit:3,
              tokenCount:GEMS.reduce((total, color) => total + Number(player[color] || 0), 0),
              tokenLimit:10,
            },
          },
          {
            kind:'ResourceSnapshot', id:'bank', title:'Bank', data:{
              tokens:Object.fromEntries(GEMS.map(color => [color, Number(state.gamestorage[color] || 0)])),
              availableNonGoldColors:structuredClone(availableColors.map(item => item.id)),
              sameColorPairColors:colors.filter(color => Number(state.gamestorage[color] || 0) >= 4),
            },
          },
          {
            kind:'ResourceSnapshot', id:'opponents', title:'Public opponents', data:{
              players:opponentSeats.map(pid => ({
                seat:pid,
                tokens:playerTokens(state.playerstorage[pid]),
                bonuses:Object.fromEntries(colors.map(color => [
                  color, Number(this.engine.bonuses(state, pid)[color] || 0),
                ])),
                score:this.engine.score(state, pid),
                distanceTo15:Math.max(0, 15 - this.engine.score(state, pid)),
                purchasedCardCount:state.playerstorage[pid].boughtCards.length,
                reservedCount:state.playerstorage[pid].storedCards.length,
                canReserveAnyVisibleCard:state.playerstorage[pid].storedCards.length < 3,
              })),
            },
          },
        ];
        if (state.wrapper.phase === 'choose_noble') {
          const visibleNobles = modelNobleFacts.filter(noble => (
            (state.wrapper.pendingNobles?.ids || []).includes(noble.nobleId)
          ));
          if (visibleNobles.length) {
            modelFacts.push({
              kind:'CurrentTargetFact', id:'nobles', title:'Current noble choices', data:{nobles:visibleNobles},
            });
          }
        } else if (state.wrapper.phase !== 'reserve_discard') {
          for (const tier of [1, 2, 3]) {
            const cards = marketCardFacts.filter(card => card.tier === tier);
            if (cards.length) {
              modelFacts.push({
                kind:'CurrentTargetFact',
                id:`market-cards-tier-${tier}`,
                title:`Visible market cards — tier ${tier}`,
                data:{cards},
              });
            }
          }
          if (ownReservedFacts.length) {
            modelFacts.push({
              kind:'CurrentTargetFact', id:'own-reserved-cards', title:'Cards reserved by this seat', data:{cards:ownReservedFacts},
            });
          }
          if (modelNobleFacts.length) {
            modelFacts.push({
              kind:'CurrentTargetFact', id:'nobles', title:'Visible nobles', data:{nobles:modelNobleFacts},
            });
          }
          modelFacts.push(
            {
              kind:'DirectOutcomeFact', id:'card-cost-basis', title:'Card costs for the acting seat', data:{
                actorSeat:seat,
                payableAfterBonuses:'Printed cost minus this seat bonuses, floored at zero per color. Bonuses are already applied.',
                remainingGap:'Payable amount still missing after this seat colored tokens; gold can cover this gap.',
              },
            },
            {
              kind:'DirectOutcomeFact', id:'buy-now-seat-meaning', title:'Meaning of buy-now seats', data:{
                field:'opponentsAbleToBuyNow',
                meaning:'Opponents legally able to buy with their own bonuses and tokens now; their payment may differ from yours. Ability is not a prediction of what they will do.',
              },
            },
            {
              kind:'DirectOutcomeFact', id:'market-card-turnover', title:'Market card turnover', data:{
                triggerActions:['buy_card', 'reserve_card'],
                selectedMarketCardLeaves:true,
                selectedCardCanBeTargetedAgain:false,
                replacementAvailableByTier:Object.fromEntries([1,2,3].map(tier => [
                  tier, state.decks[tier].length > 0,
                ])),
                replacementRule:'Reveal a replacement immediately only if that tier deck is nonempty. If empty, the market slot stays empty.',
                replacementIdentity:'unknown before the selected action resolves',
              },
            },
          );
        }
        modelFacts.push(
          actionFrame.buildActionFact({
            phase:state.wrapper.phase,
            availableColors:availableColors.map(item => item.id),
            sameColorPairColors:colors.filter(color => Number(state.gamestorage[color] || 0) >= 4),
            affordableCards:scoringTargets
              .filter(target => target.affordableNow)
              .map(target => ({source:target.source, id:Number(target.id)})),
            reserveAvailable,
            marketCardIds:marketCards.map(card => card.id),
            deckLevels:[1,2,3].filter(level => state.decks[level]?.length),
            nobleIds:(state.wrapper.pendingNobles?.ids || []).map(Number),
            requiredDiscardCount:Math.max(
              0,
              GEMS.reduce((total, color) => total + Number(player[color] || 0), 0) - 10,
            ),
            availableTokens:playerTokens(player),
          }),
          {
            kind:'DirectOutcomeFact', id:'current-action-rules', title:'Current direct outcomes', data:(
              state.wrapper.phase === 'reserve_discard'
                ? {
                    discardGems:{
                      requiredCount:Math.max(0, GEMS.reduce((total, color) => total + Number(player[color] || 0), 0) - 10),
                      availableTokens:playerTokens(player),
                      colors:GEMS.filter(color => Number(player[color] || 0) > 0),
                    },
                  }
                : state.wrapper.phase === 'choose_noble'
                  ? {chooseNoble:{nobleIds:(state.wrapper.pendingNobles?.ids || []).map(Number)}}
                  : {
                      takeGems:{
                        availableColors:structuredClone(availableColors.map(item => item.id)),
                        distinctColorCount:Math.min(3, availableColors.length),
                        sameColorPairColors:colors.filter(color => Number(state.gamestorage[color] || 0) >= 4),
                        tokenLimit:10,
                      },
                      buyCard:{
                        marketCardIds:marketCards.map(card => card.id),
                        ownReservedCardIds:reservedCards.map(card => card.id),
                        paymentUsesPayableAfterBonuses:true,
                        paymentChoiceRequired:true,
                        paymentFormat:'one color entry per physical token; G replaces any color; [] only for zero payableAfterBonuses',
                      },
                      reserveCard:{
                        available:reserveAvailable,
                        marketCardIds:marketCards.map(card => card.id),
                        deckLevels:[1,2,3].filter(level => state.decks[level]?.length),
                        goldGainedIfAvailable:Number(state.gamestorage.G || 0) > 0,
                        excessTokenChoiceAfterReveal:true,
                      },
                    }
            ),
          },
          {
            kind:'DynamicScoreFact', id:'scoring', title:'Current scoring', data:{
              currentTotal:scoreFacts.currentTotal,
              cardPoints,
              noblePoints:player.boughtNobles.length * 3,
              targetScore:15,
              distanceToEnd:Math.max(0, 15 - this.engine.score(state, seat)),
              finalRoundRemainingTurns:state.wrapper.remainingTurns ?? null,
              tieBreaker:'fewer purchased cards',
            },
          },
          {
            kind:'AuthorityBoundaryFact', id:'current-authority', title:'Current authority boundary', data:{
              oneMainAction:true,
              nextActorSeat:(seat + 1) % state.wrapper.playerCount,
              playerChoosesOnlyExplicitOptions:true,
            },
          },
          {
            kind:'UnknownInformationFact', id:'unknown-information', title:'Unknown information', data:{
              futureMarketReplacement:'unknown until a visible card leaves the market',
              opponentReservedIdentities:'unknown',
              opponentNextAction:'unknown; buy and reserve capabilities describe legal options, not intentions or predictions',
            },
          },
        );
        const purchaseHorizon = scoringFrame.buildPublicPurchaseHorizon({
          engine:this.engine, state, seat, gems:GEMS, canAfford,
        });
        if (purchaseHorizon) modelFacts.push(purchaseHorizon);
        const marketChanges = recentMarketChanges(state, seat);
        if (marketChanges) modelFacts.push(marketChanges);
        view.decisionSurface = {
          schemaVersion:'natural-decision-surface-v2',
          currentActions:state.wrapper.phase === 'reserve_discard'
            ? [{action:'discard_gem',availableNow:true}]
            : state.wrapper.phase === 'choose_noble'
              ? [{action:'choose_noble',availableNow:true}]
              : [
                {action:'take_gems',availableNow:availableColors.length > 0},
                {
                  action:'buy_card',
                  availableNow:affordablePurchaseTargets.length > 0,
                },
                {action:'reserve_card',availableNow:reserveAvailable},
              ],
          modelFacts:{
            version:1,
            coverage:'complete-current-decision',
            facts:modelFacts,
          },
          narrativeSections:[
            {
              id:'turn', title:'轮次、行动者与结束条件', lines:[
                `turn=${state.wrapper.turn}; actor seat=${seat}; phase=${state.wrapper.phase}; `
                + `score=${this.engine.score(state, seat)}; distanceTo15=${Math.max(0, 15 - this.engine.score(state, seat))}; `
                + `finalRoundRemainingTurns=${state.wrapper.remainingTurns ?? 'not-started'}`,
                `playerCount=${state.wrapper.playerCount}; cyclicTurnOrder=${state.playerstorage.map((_candidate, pid) => pid).join('→')}→repeat; `
                + `after this DecisionFrame nextActorSeat=${(seat + 1) % state.wrapper.playerCount}; `
                + `opponentActionsBeforeActorReturns=${Math.max(0, state.wrapper.playerCount - 1)}; each DecisionFrame permits exactly one main action family.`,
                `currentMainActions: take_gems=${availableColors.length ? 'available' : 'unavailable'}; reserve_card=${reserveAvailable ? 'available' : 'unavailable'} `
                + `(reservedSlots=${player.storedCards.length}/3); buy_card=${affordablePurchaseTargets.length ? 'available' : 'unavailable'} `
                + `(affordableTargets=[${affordablePurchaseTargets.join(',') || 'none'}]).`,
              ],
            },
            {
              id:'your-state', title:'你的资源、折扣与进度', lines:[
                `seat=${seat}; tokens=${formatResourceMap(playerTokens(player))}; `
                + `bonuses=${playerBonusLine(seat)}; score=${this.engine.score(state, seat)}; `
                + `purchasedCards=${player.boughtCards.length}; reservedCards=${player.storedCards.length}/3; `
                + `tokenCount=${GEMS.reduce((total, color) => total + Number(player[color] || 0), 0)}/10`,
              ],
            },
            {
              id:'opponents', title:'公开对手状态',
              lines:opponentLines.length ? opponentLines : ['none'],
            },
            {
              id:'bank', title:'银行与拿取限制', lines:[
                `bank=${formatResourceMap(Object.fromEntries(GEMS.map(color => [color, Number(state.gamestorage[color] || 0)])))}`,
                'take different colors: when 3 or more non-gold colors are available choose exactly 3 colors and take exactly 1 token of each; when 2 remain choose both and take 1 each; when 1 remains take 1.',
                'take same color: choose exactly two of one non-gold color only when at least four of that color are in the bank before taking.',
                'bank update: subtract only the tokens selected in this action; the player\'s pre-existing tokens never subtract from the bank, and every unselected bank color remains unchanged.',
                'token limit=10 after the action; a gem take appends explicit discards in the same chain, while a reserve reveals its card/replacement first and discards in the next reserve_discard frame.',
              ],
            },
            {
              id:'cards', title:'可见卡牌与折扣后费用',
              lines:[
                `reserveMarketTargets=[${marketCards.map(card => card.id).join(',') || 'none'}]`,
                `reserveDeckTargets=[${reserveDeckLines.join(' | ') || 'none'}]`,
                'buy market or reserved card: payment lists the physical tokens spent after the displayed bonuses are applied; repeat a color per token. Gold G may replace any color. The engine validates the exact cost and owned tokens.',
                'payment=[] means a genuinely free purchase: every payableAfterBonuses value is zero. A zero remainingGap only means affordable, not free.',
                'reserve card: take 1 gold if available; after the card or replacement is revealed, any required token-limit discard is resolved in a separate reserve_discard frame.',
                ...(cardLines.length ? cardLines : ['none']),
              ],
            },
            {
              id:'nobles', title:'贵族要求与当前差距',
              lines:nobleFacts.map(noble => (
                `nobleId=${noble.id}; points=3; requiredBonuses=${formatResourceMap(noble.requiredBonuses)}; `
                + `currentBonuses=${formatResourceMap(noble.currentBonuses)}; `
                + `remainingBonusGap=${formatResourceMap(noble.remainingBonusGap)}`
              )),
            },
            {
              id:'contention', title:'公开卡牌争夺事实',
              lines:[
                `immediatelyContestedMarketCards=[${immediatelyContested.join(' | ') || 'none'}]; a non-empty opponent seat list means that opponent can buy the card before this actor returns.`,
                'payableAfterBonuses is the acting seat printedCost minus its bonuses, floored at zero per color; bonuses are already applied. Opponent affordability uses each opponent own bonuses and tokens; it does not imply a free purchase for this actor.',
                'Only nobleId entries in the nobles section are nobles. A developmentCardId is never a noble requirement; compare only requiredBonuses and remainingBonusGap.',
                ...(contentionLines.length ? contentionLines : ['no face-up card is currently affordable by an opponent']),
              ],
            },
            {
              id:'scoring', title:'官方计分与终局', lines:[
                `current score=${scoreFacts.currentTotal}; card points=${cardPoints}; noble points=${player.boughtNobles.length * 3}.`,
                'Each purchased card scores its printed points immediately; each acquired noble scores 3 points.',
                'For each card, totalImmediateScoreIfBoughtNow already includes printedPoints plus one newly unlocked noble when noblePointsIfBoughtNow=3.',
                'Reaching at least 15 points starts the final round. Highest score wins; tied players compare fewer purchased cards.',
              ],
            },
          ],
          scoringDecisionFacts:structuredClone(scoringDecisionFacts),
          informationBoundaries:[
            'Only visible market cards and this seat own reserved cards are shown.',
            'Deck order and reserved cards owned by other seats remain unknown.',
            'This surface contains authority facts only; the acting player remains responsible for its choice.',
          ],
        };
      }
      view.turnOutcomeSummary = {
        decisionId:view.decisionId,
        enumerationComplete:false,
        coverageStatus:'not_explored',
      };
      return view;
    }

    renderPublicOutcome(outcome) { return publicOutcome.renderPublicOutcome(outcome); }
    _outcome(before, after, pid, action, events) {
      const was = before.playerstorage[pid];
      const now = after.playerstorage[pid];
      const card = action.cardId && !String(action.source || '').startsWith('deck_') ? this.engine.carddb[action.cardId] : null;
      const gemsDelta = GEMS.reduce((out, color) => {
        const delta = (now[color] || 0) - (was[color] || 0);
        if (delta) out[color] = delta;
        return out;
      }, {});
      const gemsAfterTotal = GEMS.reduce(
        (total, color) => total + Number(now[color] || 0),
        0,
      );
      const isBuy = action.type === 'buy_market' || action.type === 'buy_reserved';
      const isMarketAction = action.type === 'buy_market' || action.type === 'reserve_market';
      const gemsPaidTotal = isBuy
        ? GEMS.reduce((total, color) => total + Number(action.payment?.[color] || 0), 0)
        : null;
      if (isBuy) {
        const paidFromDelta = Object.values(gemsDelta)
          .filter(value => value < 0)
          .reduce((total, value) => total - value, 0);
        if (gemsPaidTotal !== paidFromDelta) {
          throw new Error('Splendor payment total drifted from authority gem deltas');
        }
      }
      const opponentSeats = before.playerstorage
        .map((_candidate, candidate) => candidate)
        .filter(candidate => candidate !== pid);
      const replacementRevealed = Boolean(isMarketAction && card && after.gamestorage.cards.some(item => (
        item.location === `market_${card.lvl}`
        && !before.gamestorage.cards.some(previous => previous.id === item.id)
      )));
      const marketBoundary = isMarketAction && card ? {
        opponentBuyNowSeatsBefore:opponentSeats.filter(candidate => (
          this._canAfford(before, candidate, card)
        )),
        opponentReserveIfStillVisibleSeatsBefore:opponentSeats.filter(candidate => (
          before.playerstorage[candidate].storedCards.length < 3
        )),
        selectedMarketCardLeaves:true,
        selectedMarketCardAvailableAfter:after.gamestorage.cards.some(item => item.id === card.id),
        replacementRevealedImmediately:replacementRevealed,
        replacementIdentity:replacementRevealed ? 'unknown' : 'none',
      } : {};
      return {
        scoreDelta: this.engine.score(after, pid) - this.engine.score(before, pid),
        scoreAfter: this.engine.score(after, pid),
        cardsBought: now.boughtCards.length - was.boughtCards.length,
        noblesGained: now.boughtNobles.length - was.boughtNobles.length,
        reservedDelta: now.storedCards.length - was.storedCards.length,
        gemsDelta,
        ...(isBuy ? {gemsPaidTotal} : {}),
        gemsAfterTotal,
        ...marketBoundary,
        ...(card ? {card:{
          id:card.id,
          points:card.points,
          level:card.lvl,
          bonus:GEMS[card.type],
        }} : {}),
        finalRoundStarted: events.some(event => event.type === 'final_round_started'),
        gameFinished: events.some(event => event.type === 'game_finished'),
        winner: after.wrapper.winner,
        winners:structuredClone(after.wrapper.winners || []),
        nextPlayer: after.wrapper.currentPlayer,
        phase: after.wrapper.phase,
        remaining:{
          gems:Object.fromEntries(GEMS.map(color => [color, now[color] || 0])),
          bank:Object.fromEntries(GEMS.map(color => [color, after.gamestorage[color] || 0])),
        },
      };
    }

    _replayDerivedCommitted(snapshot) {
      const replay = new core.SplendorEngine();
      replay.start(snapshot.history.setup);
      if (snapshot.history.setupAdjustments.length) {
        replay.applySetupAdjustments(snapshot.history.setupAdjustments);
      }
      const derived = {};
      for (const entry of snapshot.history.actions) {
        const before = structuredClone(replay.state);
        const result = replay.dispatch(entry.decisionId, entry.chain);
        if (!result.ok) throw new Error('invalid Splendor derived replay');
        derived[entry.decisionId] = {
          transaction:structuredClone(entry.chain),
          outcome:this._outcome(
            before,
            replay.state,
            before.wrapper.currentPlayer,
            result.action,
            result.events,
          ),
        };
      }
      return derived;
    }

    _candidateTakePrograms() {
      const state = this.engine.state;
      const pid = state.wrapper.currentPlayer;
      const hand = state.playerstorage[pid];
      const candidates = [];
      const add = taken => {
        const afterTake = Object.fromEntries(GEMS.map(color => [color, (hand[color] || 0) + (taken[color] || 0)]));
        const requiredDiscard = Math.max(0, GEMS.reduce((total, color) => total + afterTake[color], 0) - 10);
        for (const discard of discardDistributions(afterTake, requiredDiscard)) {
          candidates.push({steps:[
            {op:'begin',action:'take_gems'},
            ...Object.entries(taken).map(([color, count]) => ({op:'take_gem',color,count})),
            ...Object.entries(discard).map(([color, count]) => ({op:'discard_gem',color,count})),
          ]});
        }
      };
      const available = GEMS.slice(0, 5).filter(color => state.gamestorage[color] >= 1);
      const requiredDistinctCount = Math.min(3, available.length);
      if (requiredDistinctCount) for (let mask = 1; mask < (1 << available.length); mask += 1) {
        const colors = available.filter((_, index) => mask & (1 << index));
        if (colors.length === requiredDistinctCount) add(Object.fromEntries(colors.map(color => [color, 1])));
      }
      for (const color of GEMS.slice(0, 5)) if (state.gamestorage[color] >= 4) add({[color]:2});
      return candidates;
    }

    _candidateBuyPrograms() {
      const state = this.engine.state;
      const pid = state.wrapper.currentPlayer;
      const player = state.playerstorage[pid];
      const bonuses = this.engine.bonuses(state, pid);
      const targets = [
        ...state.gamestorage.cards.map(card => ({source:'market', cardId:card.id})),
        ...player.storedCards.map(card => ({source:'reserved', cardId:card.id})),
      ];
      return targets.flatMap(target => {
        const card = this.engine.carddb[target.cardId];
        const required = Object.fromEntries(GEMS.slice(0, 5).map(color => [
          color, Math.max(0, (card.cost[color] || 0) - bonuses[color]),
        ]));
        const vectors = [];
        const enumerate = (index, payment, shortfall) => {
          if (index === GEMS.length - 1) {
            if (shortfall <= (player.G || 0)) vectors.push({...payment, ...(shortfall ? {G:shortfall} : {})});
            return;
          }
          const color = GEMS[index];
          const maximum = Math.min(required[color], player[color] || 0);
          for (let paid = 0; paid <= maximum; paid += 1) {
            if (paid) payment[color] = paid;
            else delete payment[color];
            enumerate(index + 1, payment, shortfall + required[color] - paid);
          }
          delete payment[color];
        };
        enumerate(0, {}, 0);
        return vectors.map(payment => ({steps:[
          {op:'begin',action:'buy_card'}, {op:'select_card',...target},
          ...GEMS.filter(color => payment[color]).map(color => ({op:'pay_gem',color,count:payment[color]})),
        ]}));
      });
    }

    _candidateReservePrograms() {
      const state = this.engine.state;
      const player = state.playerstorage[state.wrapper.currentPlayer];
      if (player.storedCards.length >= 3) return [];
      const targets = [
        ...state.gamestorage.cards.map(card => ({op:'select_card',source:'market',cardId:card.id})),
        ...[1,2,3].filter(level => state.decks[level]?.length).map(level => ({op:'select_deck',level})),
      ];
      return targets.map(target => ({steps:[
        {op:'begin',action:'reserve_card'}, target,
      ]}));
    }

    _candidateReserveDiscardPrograms() {
      const player = this.engine.state.playerstorage[this.engine.state.wrapper.currentPlayer];
      const requiredDiscard = Math.max(0, GEMS.reduce((total, color) => total + (player[color] || 0), 0) - 10);
      return discardDistributions(player, requiredDiscard).map(discard => ({steps:[
        {op:'begin', action:'reserve_card'},
        ...GEMS.filter(color => discard[color]).map(color => ({op:'discard_gem', color, count:discard[color]})),
      ]}));
    }

    _candidateNobleChoicePrograms() {
      const ids = this.engine.state.wrapper.pendingNobles?.ids || [];
      return ids.map(nobleId => ({steps:[
        {op:'begin', action:'buy_card'},
        {op:'choose_noble', nobleId},
      ]}));
    }

    *_validatedProgramIterator(candidates) {
      const decisionId = this.engine.decisionId();
      const programs = [];
      for (const transaction of candidates) {
        const cloneEngine = this.engine.fork();
        let result = cloneEngine.dispatch(decisionId, transaction);
        if (!result.ok && result.code === 'NOBLE_SELECTION_REQUIRED') {
          for (const noble of result.facts.eligibleNobles || []) {
            const withNoble = {steps:[...transaction.steps, {op:'choose_noble',nobleId:noble.id}]};
            const retry = this.engine.fork();
            const committed = retry.dispatch(decisionId, withNoble);
            if (committed.ok) yield {transaction:withNoble, result:committed, after:retry.snapshot()};
          }
        } else if (result.ok) yield {transaction, result, after:cloneEngine.snapshot()};
      }
    }

    _validatedPrograms(candidates) {
      return [...this._validatedProgramIterator(candidates)];
    }

    _rootDescriptor(steps) {
      const family = steps[0].action;
      if (family === 'take_gems') {
        const gems = {};
        for (const step of steps.slice(1)) {
          if (step.op === 'discard_gem') break;
          if (step.op === 'take_gem') gems[step.color] = step.count;
        }
        return {key:JSON.stringify({family,gems}), initialAction:{op:'take_gems', gems}};
      }
      const target = steps[1];
      return {key:JSON.stringify({family,target}), initialAction:{...target}};
    }

    *_turnPrograms(seat = this.engine.state.wrapper.currentPlayer) {
      const decisionId = this.engine.decisionId();
      const before = this.engine.snapshot();
      const phase = this.engine.state.wrapper.phase;
      const candidates = phase === 'reserve_discard'
        ? this._candidateReserveDiscardPrograms()
        : phase === 'choose_noble'
          ? this._candidateNobleChoicePrograms()
          : [
          ...this._candidateTakePrograms(),
          ...this._candidateBuyPrograms(),
          ...this._candidateReservePrograms(),
          ];
      const validated = this._validatedProgramIterator(candidates);
      for (const {transaction, result, after} of validated) {
        const root = this._rootDescriptor(transaction.steps);
        yield {
          rootKey:root.key,
          rootAction:root.initialAction,
          steps:transaction.steps,
          causalTrace:result.events.map((event, step) => ({step:step + 1, source:event.type, effect:event.type})),
          outcome:this._outcome(before, after, seat, result.action, result.events),
          terminalKey:JSON.stringify(after),
          boundaryReason:result.boundaryReason || (after.wrapper.phase === 'finished' ? 'game_finished' : 'turn_passed'),
          termination:'automatic',
        };
      }
    }

    _finiteExplorer(seat = this.engine.state.wrapper.currentPlayer) {
      const decisionId = this.engine.decisionId();
      if (!decisionExplorer?.createFiniteProgramExplorer) throw new Error('AUTHORITY_WORKER_REQUIRED');
      const snapshot = this.engine.snapshot();
      return decisionExplorer.createFiniteProgramExplorer({
        decisionId,
        snapshot,
        pageSize:12,
        programs:() => [...this._turnPrograms(seat)],
      });
    }
    _authorityGatewayFor() {
      if (this._authorityGateway) return this._authorityGateway;
      if (!decisionExplorer?.createAuthorityGateway) throw new Error('AUTHORITY_WORKER_REQUIRED');
      this._authorityGateway = decisionExplorer.createAuthorityGateway({
        currentIdentity:() => ({
          decisionId:this.engine.decisionId(),
          seat:this.engine.state.wrapper.currentPlayer,
        }),
        finiteExplorerForSeat:(seat) => this._finiteExplorer(seat),
        programsForSeat:(seat) => [...this._turnPrograms(seat)],
        decisionMapPageSize:20,
      });
      return this._authorityGateway;
    }

    outcomeIndex(seat = this.engine.state.wrapper.currentPlayer, _request = {}) {
      return {...this._authorityGatewayFor().outcomeIndex(_request), seat};
    }

    enumerateRoutes(request = {}) {
      return this._authorityGatewayFor().enumerateRoutes(request);
    }

    validateTransaction(decisionId, transaction) {
      if (decisionId && typeof decisionId === 'object' && transaction === undefined) {
        transaction = decisionId.action || {steps:decisionId.steps || []};
        decisionId = decisionId.decisionId;
      }
      const requestedId = decisionId || this.engine.decisionId();
      const steps = transaction?.steps;
      if (requestedId !== this.engine.decisionId()) {
        return {
          ok:false, complete:false, stateChanged:false, failedStep:0,
          validatedPrefix:[], code:'STALE_DECISION',
          message:`Current decisionId is ${this.engine.decisionId()}.`,
          correction:'Refresh the current decision before continuing.',
          nextActions:[],
        };
      }
      if (!Array.isArray(steps) || !steps.length) {
        const phase = this.engine.state.wrapper.phase;
        const actionFamilies = phase === 'reserve_discard' ? ['reserve_card']
          : phase === 'choose_noble' ? ['buy_card']
            : phase === 'finished' ? [] : this.view(this.engine.state.wrapper.currentPlayer).actionFamilies;
        return {
          ok:false, complete:false, stateChanged:false, failedStep:0,
          validatedPrefix:[], code:'EXPECTED_BEGIN',
          message:'steps must start with a begin action.',
          correction:'Choose one action family and submit a new check.',
          nextActions:actionFamilies.map(action => ({op:'begin', action})),
        };
      }

      const seat = this.engine.state.wrapper.currentPlayer;
      const sourceState = structuredClone(this.engine.state);
      const directClone = this.engine.fork();
      const directResult = directClone.dispatch(requestedId, {steps:structuredClone(steps)});
      if (directResult.ok) {
        const scoreDelta = this.engine.score(directClone.state, seat)
          - this.engine.score(this.engine.state, seat);
        const outcome = this._outcome(
          sourceState,
          directClone.state,
          seat,
          directResult.action,
          directResult.events || [],
        );
        return {
          ok:true, complete:true, stateChanged:false,
          validatedPrefix:structuredClone(steps),
          events:structuredClone(directResult.events || []),
          causalTrace:(directResult.events || []).map((event, index) => ({
            step:index + 1, source:event.type, effect:event.type,
          })),
          immediateEffects:[...new Set((directResult.events || []).map(event => event.type))],
          immediateScoreDelta:scoreDelta,
          endNowScoreDelta:scoreDelta,
          outcome:structuredClone(outcome),
          scoringEngineChanges:[],
          nextActions:[],
        };
      }

      const programs = [...this._turnPrograms(seat)];
      const sharesPrefix = (program, count) =>
        program.steps.length >= count &&
        steps.slice(0, count).every((step, index) => same(step, program.steps[index]));
      const matching = programs.filter(program => sharesPrefix(program, steps.length));
      const completeProgram = matching.find(program => program.steps.length === steps.length);
      if (completeProgram) {
        const cloneEngine = this.engine.fork();
        const result = cloneEngine.dispatch(requestedId, {steps:structuredClone(steps)});
        return {
          ok:true, complete:true, stateChanged:false,
          validatedPrefix:structuredClone(steps),
          events:structuredClone(result.events || []),
          causalTrace:structuredClone(completeProgram.causalTrace || []),
          immediateEffects:[...new Set((result.events || []).map(event => event.type))],
          immediateScoreDelta:Number(completeProgram.outcome?.scoreDelta || 0),
          endNowScoreDelta:Number(completeProgram.outcome?.scoreDelta || 0),
          outcome:structuredClone(completeProgram.outcome || {}),
          scoringEngineChanges:[],
          nextActions:[],
        };
      }
      if (matching.length) {
        const nextActions = [...new Map(
          matching.map(program => {
            const action = program.steps[steps.length];
            return [JSON.stringify(action), action];
          }),
        ).values()];
        return {
          ok:true, complete:false, stateChanged:false,
          validatedPrefix:structuredClone(steps),
          events:[], causalTrace:[], immediateEffects:[],
          immediateScoreDelta:0, endNowScoreDelta:0,
          scoringEngineChanges:[], nextActions:structuredClone(nextActions),
        };
      }

      let accepted = 0;
      while (accepted < steps.length && programs.some(program => sharesPrefix(program, accepted + 1))) {
        accepted += 1;
      }
      const compatible = programs.filter(program => sharesPrefix(program, accepted));
      const nextActions = [...new Map(
        compatible
          .filter(program => program.steps[accepted])
          .map(program => [JSON.stringify(program.steps[accepted]), program.steps[accepted]]),
      ).values()];
      return {
        ok:false, complete:false, stateChanged:false, failedStep:accepted,
        validatedPrefix:structuredClone(steps.slice(0, accepted)),
        code:'ILLEGAL_STEP', message:'The step is not part of a legal complete turn.',
        correction:'Keep the validated prefix and choose one authoritative next action.',
        events:[], causalTrace:[], immediateEffects:[],
        immediateScoreDelta:0, endNowScoreDelta:0, scoringEngineChanges:[],
        nextActions:structuredClone(nextActions),
      };
    }

    dispatch(decisionId, action) {
      if (decisionId && typeof decisionId === 'object' && action === undefined) {
        action = decisionId.action || {steps:decisionId.steps};
        decisionId = decisionId.decisionId;
      }
      const transaction = structuredClone(action);
      const before = this.engine.state;
      const pid = before.wrapper.currentPlayer;
      const result = this.engine.dispatch(decisionId || this.engine.decisionId(), action);
      if (!result.ok) return result;
      this._authorityGateway?.invalidate();
      if (result.duplicate) {
        if (!this._derivedCommitted[result.decisionId]) {
          this._derivedCommitted = this._replayDerivedCommitted(this.engine.snapshot());
        }
        const derived = this._derivedCommitted[result.decisionId];
        result.transaction = structuredClone(derived.transaction);
        result.outcome = structuredClone(derived.outcome);
        return result;
      }
      result.transaction = transaction;
      result.outcome = this._outcome(before, this.engine.state, pid, result.action, result.events);
      const recorded = this.engine.state.history.actions.at(-1);
      this._derivedCommitted[result.decisionId] = {
        transaction:structuredClone(recorded.chain),
        outcome:structuredClone(result.outcome),
      };
      return result;
    }

    snapshot() {
      return this.engine.snapshot();
    }

    finalResult() {
      return this.engine.finalResult();
    }

    restore(snapshot) {
      if (snapshot && snapshot.gs) {
        const wrapper = snapshot.st || {};
        snapshot = {
          schemaVersion:snapshot.v || 5,
          gamestorage:snapshot.gs,
          playerstorage:snapshot.ps,
          decks:snapshot.dk,
          _decks:snapshot.dk,
          carddb:snapshot.cdb,
          rngState:1,
          committed:{},
          wrapper:{
            playerCount:wrapper.playerCount || snapshot.ps.length,
            currentPlayer:wrapper.currentPlayer || 0,
            phase:wrapper.phase === 'finished' ? 'finished' : (wrapper.phase === 'last_round' ? 'last_round' : 'playing'),
            turn:wrapper.turn || 0,
            remainingTurns:Number.isInteger(wrapper.remainingTurns) ? wrapper.remainingTurns : null,
            winner:wrapper.winner ?? null,
            decisionSeq:wrapper.turn || 0,
          },
        };
      }
      const supplied = structuredClone(snapshot);
      const candidateEngine = new core.SplendorEngine();
      candidateEngine.restore(supplied);
      const candidateSnapshot = candidateEngine.snapshot();
      const derived = this._replayDerivedCommitted(candidateSnapshot);
      const validateDerivedOutcomes = supplied.schemaVersion === 7;
      for (const [decisionId, record] of Object.entries(supplied.committed || {})) {
        if (validateDerivedOutcomes
          && record?.result && Object.hasOwn(record.result, 'outcome')) {
          const expected = derived[decisionId]?.outcome;
          if (!expected || decisionExplorer.canonicalFingerprint(record.result.outcome)
            !== decisionExplorer.canonicalFingerprint(expected)) {
            throw new Error('invalid Splendor derived outcome');
          }
        }
      }
      this.engine.restore(candidateSnapshot);
      this._derivedCommitted = derived;
      this._authorityGateway?.invalidate();
      return this.view(candidateSnapshot.wrapper.currentPlayer);
    }

    subscribe(listener) {
      return this.engine.subscribe(listener);
    }
  }

  return {SplendorBGLabAdapter};
});
