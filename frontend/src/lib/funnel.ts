/* Product-funnel events — Detoura's own, anonymous, batched.
 *
 * A random per-tab session key and a random per-browser visitor key. No
 * traveller PII is ever passed here; the server also strips anything
 * PII-shaped and only keeps a whitelist of prop keys. Events are batched and
 * flushed with `navigator.sendBeacon` where possible so they never block or
 * fail a navigation.
 */

const BASE = import.meta.env.VITE_API_BASE || "/api/v1";

type Props = Record<string, string | number | boolean>;
interface Ev {
  event: string;
  tier?: string;
  props?: Props;
}

function randKey(): string {
  const a = new Uint8Array(12);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
}

function keyed(store: Storage, name: string): string {
  try {
    let v = store.getItem(name);
    if (!v) {
      v = randKey();
      store.setItem(name, v);
    }
    return v;
  } catch {
    return "";
  }
}

let sessionKey = "";
let visitorKey = "";
try {
  sessionKey = keyed(sessionStorage, "detoura.fk.s");
  visitorKey = keyed(localStorage, "detoura.fk.v");
} catch {
  /* private mode: events just won't be attributed */
}

let queue: Ev[] = [];
let timer: number | null = null;

function flush(): void {
  if (queue.length === 0) return;
  const body = JSON.stringify({
    session_key: sessionKey,
    visitor_key: visitorKey,
    events: queue.slice(0, 50),
  });
  queue = [];
  try {
    if (navigator.sendBeacon) {
      navigator.sendBeacon(`${BASE}/events`, new Blob([body], { type: "application/json" }));
      return;
    }
  } catch {
    /* fall through to fetch */
  }
  void fetch(`${BASE}/events`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
    keepalive: true,
  }).catch(() => undefined);
}

/** Record one funnel event. Fire-and-forget. */
export function funnel(event: string, opts: { tier?: string; props?: Props } = {}): void {
  queue.push({ event, tier: opts.tier, props: opts.props });
  if (queue.length >= 12) {
    flush();
    return;
  }
  if (timer === null) {
    timer = window.setTimeout(() => {
      timer = null;
      flush();
    }, 2500);
  }
}

if (typeof window !== "undefined") {
  window.addEventListener("pagehide", flush);
  window.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush();
  });
}
