import "server-only";
import type { LiveEvent } from "./types";

type Subscriber = (e: LiveEvent) => void;

/**
 * In-memory fan-out from the webhook route to open SSE streams. Held on
 * globalThis so Next's dev HMR doesn't orphan subscribers on reload.
 */
const store = globalThis as unknown as {
  __pennyBus?: Set<Subscriber>;
  __pennyRecent?: LiveEvent[];
};

store.__pennyBus ??= new Set<Subscriber>();
store.__pennyRecent ??= [];

const RECENT_LIMIT = 50;

export function publish(event: LiveEvent) {
  store.__pennyRecent!.push(event);
  if (store.__pennyRecent!.length > RECENT_LIMIT) store.__pennyRecent!.shift();

  for (const sub of store.__pennyBus!) {
    try {
      sub(event);
    } catch {
      // A dead stream shouldn't take down delivery to the others.
    }
  }
}

export function subscribe(fn: Subscriber): () => void {
  store.__pennyBus!.add(fn);
  return () => store.__pennyBus!.delete(fn);
}
