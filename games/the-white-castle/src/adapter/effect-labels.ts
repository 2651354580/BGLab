/** Die color and printed light/dark tone are different engine selectors. */
export function describeCastleRowSelection(selector: string): string {
  if (selector === "black" || selector === "white" || selector === "coral") {
    return `choose one castle card row with die-color=${selector}`;
  }
  if (selector === "light") return "choose one light-tone castle card row (any die color)";
  if (selector === "any") return "choose any castle card row";
  throw new Error(`Unknown castle row selector: ${selector}`);
}
