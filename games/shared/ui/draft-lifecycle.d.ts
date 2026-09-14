export interface DraftLifecycle<T> {
  start(value: T): void;
  push(value: T): boolean;
  undo(current: T): T;
  restart(): T;
  lock(reason: string): void;
  clear(): void;
  readonly lockedReason: string;
  readonly canUndo: boolean;
  readonly depth: number;
}

export function create<T>(initial?: T | null): DraftLifecycle<T>;
