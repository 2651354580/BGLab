import type { GameEvent } from "../core";

interface PieceView { element: HTMLElement; art?: HTMLElement; rect: DOMRect }
export type BoardCapture = Map<string, PieceView>;
const EASE = "cubic-bezier(.22,.7,.24,1)";

function frozenArt(element: HTMLElement): HTMLElement {
  const clone = element.cloneNode(true) as HTMLElement;
  const originals = [element, ...element.querySelectorAll<HTMLElement>("*")];
  const copies = [clone, ...clone.querySelectorAll<HTMLElement>("*")];
  originals.forEach((original, index) => {
    const copy = copies[index];
    const style = getComputedStyle(original);
    for (const property of style) copy.style.setProperty(property, style.getPropertyValue(property));
    for (const attribute of [...copy.attributes]) {
      if (attribute.name === "id" || attribute.name.startsWith("data-") || attribute.name.startsWith("on")) copy.removeAttribute(attribute.name);
    }
    copy.style.animation = "none";
    copy.style.transition = "none";
    copy.style.pointerEvents = "none";
  });
  Object.assign(clone.style, {
    position: "absolute", inset: "0", width: "100%", height: "100%", margin: "0",
    transform: "none", visibility: "visible", outline: "none", boxSizing: "border-box",
  });
  return clone;
}

export function captureBoard(root: ParentNode, events: GameEvent[]): BoardCapture {
  const moving = new Set<string>();
  for (const event of events) {
    if (event.type === "DieDrafted" || event.type === "DiePlaced") moving.add(`die:${event.die.id}`);
    if (event.type === "MemberMoved") moving.add(`member:${event.member}`);
    if (event.type === "CardMoved") {
      moving.add(`card:${event.card}`);
      if (event.from.endsWith("-deck")) moving.add(`deck:${event.from}`);
    }
  }
  return new Map([...root.querySelectorAll<HTMLElement>("[data-motion-key]")].map(element => [
    element.dataset.motionKey!, { element, art: moving.has(element.dataset.motionKey!) ? frozenArt(element) : undefined, rect: element.getBoundingClientRect() },
  ]));
}

function visible(rect: DOMRect): boolean {
  return rect.width > 0 && rect.height > 0 && rect.right > 0 && rect.left < innerWidth && rect.bottom > 0 && rect.top < innerHeight;
}

export class BoardMotion {
  epoch = 0;
  private animations = new Set<Animation>();
  private cleanup = new Set<() => void>();

  constructor() {
    window.addEventListener("resize", () => this.cancel());
    window.addEventListener("scroll", () => this.cancel(), { capture: true, passive: true });
    document.addEventListener("visibilitychange", () => { if (document.hidden) this.cancel(); });
    matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", () => this.cancel());
  }

  enabled(): boolean {
    return !document.hidden && !matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  cancel(): void {
    this.epoch++;
    for (const animation of this.animations) animation.cancel();
    for (const clean of [...this.cleanup]) clean();
  }

  private async animate(element: HTMLElement, frames: Keyframe[], duration: number, easing = EASE): Promise<void> {
    let animation: Animation | undefined;
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      animation = element.animate(frames, { duration, easing, fill: "both" });
      this.animations.add(animation);
      await Promise.race([
        animation.finished.catch(error => {
          if (!(error instanceof DOMException && error.name === "AbortError")) console.warn("Board motion interrupted", error);
        }),
        new Promise<void>(resolve => { timer = setTimeout(resolve, duration + 250); }),
      ]);
    } catch (error) {
      // Rendering is optional; an unavailable animation API cannot block a turn.
      console.warn("Board motion unavailable", error);
    } finally {
      clearTimeout(timer);
      animation?.cancel();
      if (animation) this.animations.delete(animation);
    }
  }

  async play(events: GameEvent[], before: BoardCapture, root: ParentNode, epoch: number, paced = false): Promise<boolean> {
    if (!this.enabled() || epoch !== this.epoch) return false;
    const after = captureBoard(root, events);
    const moves: Array<{ key: string; from: PieceView; to: PieceView; flip: boolean; card: boolean; rotation: number }> = [];
    for (const event of events) {
      let key: string | undefined;
      let source: string | undefined;
      let flip = false;
      if (event.type === "DieDrafted" || event.type === "DiePlaced") key = `die:${event.die.id}`;
      if (event.type === "MemberMoved") key = `member:${event.member}`;
      if (event.type === "CardMoved") {
        key = `card:${event.card}`;
        source = event.from.endsWith("-deck") ? `deck:${event.from}` : key;
        flip = event.to === "lantern" || event.from.endsWith("-deck");
      }
      if (!key) continue;
      const from = before.get(source ?? key), to = after.get(key);
      if (from?.art && to?.art && visible(from.rect) && visible(to.rect)) {
        moves.push({ key, from, to, flip, card: event.type === "CardMoved", rotation: event.type === "CardMoved" && event.to === "lantern" ? -90 : 0 });
      }
    }

    const moving = new Set(moves.map(move => move.key));
    const parallel: Promise<void>[] = [];
    // Recenter the remaining dice and compact existing stacks without snapping.
    for (const [key, to] of after) {
      if (moving.has(key) || !/^(die|member|marker):/.test(key)) continue;
      const from = before.get(key);
      if (!from || !visible(to.rect)) continue;
      const dx = from.rect.left - to.rect.left, dy = from.rect.top - to.rect.top;
      if (Math.abs(dx) + Math.abs(dy) > 1) parallel.push(this.animate(to.element, [
        { transform: `translate(${dx}px,${dy}px)` }, { transform: "translate(0,0)" },
      ], 220));
    }

    const layer = document.createElement("div");
    layer.className = "board-motion-layer";
    layer.setAttribute("aria-hidden", "true");
    layer.inert = true;
    document.body.append(layer);
    const cleanups: Array<() => void> = [];
    const clean = () => {
      for (const restore of cleanups) restore();
      layer.remove();
      this.cleanup.delete(clean);
    };
    this.cleanup.add(clean);
    try {
      // Preserve each old card until its own move starts, and conceal its destination.
      const prepared = moves.map(move => {
        const ghost = document.createElement("div");
        ghost.className = `board-motion-piece${move.card ? " board-motion-piece--card" : ""}`;
        ghost.dataset.motionEvent = move.key;
        Object.assign(ghost.style, {
          left: `${move.from.rect.left}px`, top: `${move.from.rect.top}px`,
          width: `${move.from.rect.width}px`, height: `${move.from.rect.height}px`,
        });
        ghost.append(move.from.art!);
        layer.append(ghost);
        const oldVisibility = move.to.element.style.visibility;
        move.to.element.style.visibility = "hidden";
        cleanups.push(() => { move.to.element.style.visibility = oldVisibility; });
        return { ...move, ghost, oldVisibility };
      });
      for (const move of prepared) {
        if (epoch !== this.epoch) break;
        const { from, to, ghost, card, flip } = move;
        const dx = to.rect.left + to.rect.width / 2 - from.rect.left - from.rect.width / 2;
        const dy = to.rect.top + to.rect.height / 2 - from.rect.top - from.rect.height / 2;
        const scale = Math.min(to.rect.width / (move.rotation ? from.rect.height : from.rect.width), to.rect.height / (move.rotation ? from.rect.width : from.rect.height));
        const landing = `translate(${dx}px,${dy}px) scale(${scale}) rotate(${move.rotation}deg)`;
        const lift = Math.min(card ? 18 : 12, 5 + Math.hypot(dx, dy) * .045);
        const duration = Math.min(card ? 460 : 380, 220 + Math.hypot(dx, dy) * .18);
        await this.animate(ghost, [
          { transform: "translate(0,0) scale(1)", offset: 0 },
          { transform: `translate(${dx * .48}px,${dy * .48 - lift}px) scale(${1 + (scale - 1) * .48}) rotate(${move.rotation * .48 + (card ? -1.2 : 1)}deg)`, offset: .48 },
          { transform: landing, offset: 1 },
        ], duration);
        if (epoch !== this.epoch) break;
        // Freeze at the landing pose before turning over a card or changing dimensions.
        ghost.style.transform = landing;
        if (flip) {
          await this.animate(from.art!, [{ transform: "perspective(700px) rotateY(0deg)" }, { transform: "perspective(700px) rotateY(90deg)" }], 100, "ease-in");
          if (epoch !== this.epoch) break;
          Object.assign(ghost.style, { left: `${to.rect.left}px`, top: `${to.rect.top}px`, width: `${to.rect.width}px`, height: `${to.rect.height}px`, transform: "none" });
          ghost.replaceChildren(to.art!);
          await this.animate(to.art!, [{ transform: "perspective(700px) rotateY(-90deg)" }, { transform: "perspective(700px) rotateY(0deg)" }], 140, "ease-out");
        }
        if (epoch !== this.epoch) break;
        to.element.style.visibility = move.oldVisibility;
        ghost.remove();
        if (paced && prepared.at(-1) !== move) await new Promise(resolve => setTimeout(resolve, 120));
      }

      if (epoch === this.epoch) {
        const changes = new Map<string, number>();
        for (const event of events) {
          if (event.type !== "ResourceChanged" && event.type !== "InfluenceChanged"
            && !(event.type === "EffectResolved" && event.effect.type === "gainPoints")) continue;
          const resource = event.type === "ResourceChanged" ? event.resource : event.type === "InfluenceChanged" ? "influence" : "points";
          const amount = event.type === "EffectResolved" && event.effect.type === "gainPoints" ? event.effect.amount : (event as Extract<GameEvent, { type: "ResourceChanged" | "InfluenceChanged" }>).amount;
          const key = `counter:${event.player}:${resource}`;
          changes.set(key, (changes.get(key) ?? 0) + amount);
        }
        for (const [key, amount] of changes) {
          const target = after.get(key);
          if (!amount || !target || !visible(target.rect)) continue;
          const feedback = document.createElement("span");
          feedback.className = `board-motion-delta${amount < 0 ? " board-motion-delta--cost" : ""}`;
          feedback.textContent = `${amount > 0 ? "+" : "−"}${Math.abs(amount)}`;
          Object.assign(feedback.style, { left: `${target.rect.right - 12}px`, top: `${target.rect.top - 12}px` });
          layer.append(feedback);
          parallel.push(this.animate(feedback, [
            { opacity: 0, transform: "translateY(3px)", offset: 0 },
            { opacity: 1, transform: "translateY(0)", offset: .2 },
            { opacity: 1, transform: "translateY(-2px)", offset: .72 },
            { opacity: 0, transform: "translateY(-5px)", offset: 1 },
          ], 360));
        }
      }
      await Promise.all(parallel);
    } finally { clean(); }
    return epoch === this.epoch;
  }
}
