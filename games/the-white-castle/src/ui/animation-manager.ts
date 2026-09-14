export interface AnimationOptions {
  duration?: number;
  rotate?: number;
  beforeAttach?: (element: HTMLElement) => void;
}

function animationAllowed() {
  return !window.matchMedia("(prefers-reduced-motion: reduce)").matches && !document.hidden;
}

/** A small animation layer for moving components across scaled boards. */
export class ComponentAnimationManager {
  private readonly layer: HTMLElement;

  constructor(layer: HTMLElement) {
    this.layer = layer;
  }

  async slideAndAttach(element: HTMLElement, destination: HTMLElement, options: AnimationOptions = {}) {
    const sourceRect = element.getBoundingClientRect();
    const targetRect = destination.getBoundingClientRect();
    if (!sourceRect.width || !targetRect.width) throw new Error(`Cannot animate ${element.id || element.dataset.componentId}: missing source or destination geometry.`);

    const clone = element.cloneNode(true) as HTMLElement;
    clone.removeAttribute("id");
    clone.classList.add("animation-ghost");
    Object.assign(clone.style, {
      position: "fixed",
      left: `${sourceRect.left}px`,
      top: `${sourceRect.top}px`,
      width: `${sourceRect.width}px`,
      height: `${sourceRect.height}px`,
      margin: "0",
      transform: "none",
      zIndex: "1001",
    });
    this.layer.append(clone);

    element.style.visibility = "hidden";
    options.beforeAttach?.(element);
    destination.append(element);
    element.classList.add("component-in-slot");
    element.style.left = "0";
    element.style.top = "0";
    element.style.width = "100%";
    element.style.height = "100%";

    if (animationAllowed()) {
      const dx = targetRect.left - sourceRect.left;
      const dy = targetRect.top - sourceRect.top;
      const scaleX = targetRect.width / sourceRect.width;
      const scaleY = targetRect.height / sourceRect.height;
      await clone.animate([
        { transform: "translate(0, 0) scale(1) rotate(0deg)" },
        { transform: `translate(${dx}px, ${dy}px) scale(${scaleX}, ${scaleY}) rotate(${options.rotate ?? 0}deg)` },
      ], { duration: options.duration ?? 720, easing: "cubic-bezier(.2,.75,.2,1)", fill: "forwards" }).finished;
    }

    clone.remove();
    element.style.visibility = "visible";
  }

  async flip(element: HTMLElement, face: "front" | "back") {
    element.dataset.face = face;
    const sides = element.querySelector<HTMLElement>(".card-sides");
    if (!sides || !animationAllowed()) return;
    await Promise.all(sides.getAnimations().map((animation) => animation.finished));
  }

  async playSequence(steps: Array<() => Promise<void>>) {
    for (const step of steps) await step();
  }

  clear() {
    this.layer.replaceChildren();
  }
}
