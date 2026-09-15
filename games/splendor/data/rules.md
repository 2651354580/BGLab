## Game Rules: Splendor

### Resources

- 5 gem colors: C=White, S=Blue, E=Green, R=Red, O=Black.
- G=Gold (wild/joker). Max 5 gold in supply.
- Gem supply: 4 per color for 2p, 5 for 3p, 7 for 4p.
- Max 10 gems in hand. If taking gems would exceed 10, choose which gems to discard before the turn ends. When reserving a market card would exceed 10, first reserve the card, take Gold if available, and refill its market slot; then discard down to 10 using the now-visible state.

### Market

- 3 levels of development cards: Lv1 (40), Lv2 (30), Lv3 (20).
- 4 cards of each level are visible. Empty slots refill from the deck.
- A market slot refills immediately after its face-up card is purchased or reserved, before any excess-token discard or multiple-Noble choice at the end of that turn.
- Cards cost gems and give permanent gem discounts plus victory points.

### Actions

Choose exactly one action per turn:

1. `take_3`: Take 1 gem of each available different color, up to 3. When 3 or more colors remain, take exactly 3; when 2 remain, take both; when 1 remains, take it.
2. `take_2`: Take 2 gems of the same color; supply must have at least 4.
3. `buy_market`: Buy a market card, pay its cost, and gain its permanent discount and points.
4. `buy_reserved`: Buy a card from your reserved hand; at most 3 cards may be reserved.
5. `reserve_market`: Take a market card and 1 gold if available.
6. `reserve_deck`: Blind-draw from any deck level and take 1 gold if available.

### Card discounts

Bought cards give permanent discounts of their color. Discounts stack. For example, owning 2 Blue cards reduces every future Blue cost by 2.

After applying those discounts, choose which tokens to spend. Gold can substitute for any gem color, including a color you hold. Return all spent tokens to the supply. Having enough tokens is different from owing zero tokens.

### Nobles

A noble visits automatically when card discounts meet its requirements. Each noble is worth 3 points. At most one noble is awarded at the end of a turn, after any required token discard. This applies after taking gems and reserving cards as well as buying cards: eligible nobles left over from a previous turn remain eligible. If several nobles are eligible, the active player chooses exactly one. After a market purchase, refill the market slot before making that choice; a reserved-card purchase does not refill the market.

### Win condition

The first player to reach 15 points triggers the final round so all players receive equal turns. Highest score wins. Only when scores are tied, the player who bought fewer development cards wins; having more development cards is a disadvantage.
