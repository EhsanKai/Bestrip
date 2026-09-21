/* Detoura product analytics seam.
 *
 * Product code emits typed Detoura events only. This module owns privacy,
 * attribution, consent, adapter selection and deduplication, so future vendors
 * can be connected without putting SDK calls in screens.
 */

import { sanitizeEvent } from "./analyticsPayloads";

const BASE = import.meta.env.VITE_API_BASE || "/api/v1";
const ATTRIBUTION_KEY = "detoura.attribution.v1";
const ATTRIBUTION_MAX_AGE_MS = 1000 * 60 * 60 * 24 * 30;
const MAX_VALUE_LENGTH = 80;

export type ConsentPurpose = "essential" | "analytics" | "marketing";
export type SearchMode = "QUICK" | "SMART" | "DEEP";
export type ServiceTier = "BASIC" | "ALL_IN_ONE";
export type ErrorCategory =
  | "network"
  | "validation"
  | "authentication"
  | "selection_expired"
  | "payment_failed"
  | "payment_unknown"
  | "booking_failed"
  | "recovery_required"
  | "unknown";

export type ScreenName =
  | "landing"
  | "discover"
  | "searching"
  | "results"
  | "journey"
  | "checkout"
  | "my_trips";

export interface AnalyticsConsent {
  analytics: boolean;
  marketing: boolean;
}

export interface AnalyticsConfig {
  consent?: Partial<AnalyticsConsent>;
  diagnostics?: boolean;
}

export interface CampaignAttribution {
  utm_source?: string;
  utm_medium?: string;
  utm_campaign?: string;
  utm_content?: string;
  utm_term?: string;
  referrer_origin?: string;
  landing_path?: string;
  captured_at: string;
}

interface CommonProps {
  screen?: ScreenName;
  viewport_class?: "mobile" | "tablet" | "desktop";
  search_mode?: SearchMode;
  recommendation_rank?: number;
  recommendation_source?: "live" | "synthetic" | "unknown";
  bookable?: boolean;
  result_count?: number;
  leg_count?: number;
  traveler_count?: number;
  currency?: string;
  tier?: ServiceTier;
  error_category?: ErrorCategory;
  no_results?: boolean;
  deeper?: boolean;
}

export interface AnalyticsEventPayloads {
  landing_viewed: { landing_context: "home" | "destinations" | "destination" | "inspiration" };
  search_started: Pick<CommonProps, "search_mode" | "deeper" | "viewport_class">;
  search_completed: Pick<CommonProps, "search_mode" | "deeper" | "result_count" | "no_results" | "recommendation_source" | "currency">;
  search_failed: Pick<CommonProps, "search_mode" | "deeper" | "error_category">;
  recommendation_selected: Pick<CommonProps, "recommendation_rank" | "recommendation_source" | "bookable" | "leg_count" | "currency">;
  journey_viewed: Pick<CommonProps, "recommendation_rank" | "recommendation_source" | "bookable" | "leg_count" | "currency">;
  checkout_started: Pick<CommonProps, "tier" | "recommendation_source" | "leg_count" | "traveler_count" | "currency">;
  traveler_details_completed: Pick<CommonProps, "tier" | "traveler_count">;
  payment_authorization_started: Pick<CommonProps, "tier" | "currency">;
  payment_authorized: Pick<CommonProps, "tier" | "currency"> & { payment_state: "AUTHORIZED" };
  payment_failed: Pick<CommonProps, "tier" | "error_category"> & { payment_state: "FAILED" | "CANCELLED" };
  payment_unknown: Pick<CommonProps, "tier" | "error_category"> & { payment_state: "UNKNOWN" | "RECONCILIATION_REQUIRED" };
  self_service_ready: Pick<CommonProps, "tier">;
  payment_request_failed: Pick<CommonProps, "tier" | "error_category">;
  booking_confirmation_failed: Pick<CommonProps, "tier" | "error_category">;
  service_tier_selected: { tier: ServiceTier };
  promo_applied: { tier: ServiceTier; applied: true };
  booking_confirmation_started: Pick<CommonProps, "tier">;
  booking_confirmed: Pick<CommonProps, "tier"> & { booking_state: "confirmed" };
  booking_recovery_required: Pick<CommonProps, "tier" | "error_category"> & { booking_state: "recovery_required" };
  booking_failed: Pick<CommonProps, "tier" | "error_category"> & { booking_state: "failed" };
  my_trips_viewed: { trip_count?: number; empty?: boolean };
  document_downloaded: { document_type: "receipt" | "invoice" | "credit_note" | "unknown" };
}

export type AnalyticsEventName = keyof AnalyticsEventPayloads;

interface AnalyticsAdapter {
  purpose: ConsentPurpose;
  track(event: AnalyticsEventName, props: EventProps): void;
}

type EventProps = Record<string, string | number | boolean>;

interface TrackOptions {
  dedupeKey?: string;
  /** In-memory measurement scope; never serialized or used as product state. */
  dedupeScope?: object;
  includeAttribution?: boolean;
}

let consent: AnalyticsConsent = { analytics: false, marketing: false };
let adapters: AnalyticsAdapter[] = [];
let initialized = false;
let emitted = new WeakMap<object, Map<AnalyticsAdapter, Set<string>>>();
const sessionScope = {};

export function init(config: AnalyticsConfig = {}): void {
  if (initialized) return;
  initialized = true;
  setAnalyticsConsent(config.consent ?? {});

  if (import.meta.env.DEV && config.diagnostics !== false) {
    adapters.push(devDiagnosticsAdapter);
  }
  if (import.meta.env.VITE_ANALYTICS_FIRST_PARTY === "true") {
    adapters.push(firstPartyAdapter);
  }
}

export function setAnalyticsConsent(next: Partial<AnalyticsConsent>): void {
  try {
    const wasAllowed = consent.analytics;
    if ("analytics" in next) consent.analytics = next.analytics === true;
    if ("marketing" in next) consent.marketing = next.marketing === true;
    if (!consent.analytics) {
      queue = [];
      if (timer !== null) window.clearTimeout(timer);
      timer = null;
      sessionKey = visitorKey = "";
      emitted = new WeakMap();
      for (const [kind, key] of [["sessionStorage", ATTRIBUTION_KEY], ["sessionStorage", "detoura.fk.s"], ["localStorage", "detoura.fk.v"]] as const) {
        try { window[kind].removeItem(key); } catch { /* Storage may be denied. */ }
      }
    } else if (!wasAllowed) captureAttribution();
  } catch { /* Policy plumbing must never interrupt the product. */ }
}

export function track<K extends AnalyticsEventName>(
  event: K,
  props: AnalyticsEventPayloads[K],
  options: TrackOptions = {},
): void {
  try {
    const safe = sanitizeEvent(event, props);
    if (!safe) return;
    safe.viewport_class = viewportClass();
    if (consent.analytics && options.includeAttribution !== false) Object.assign(safe, attributionProps());
    const scope = options.dedupeScope ?? sessionScope;
    const dedupeKey = options.dedupeKey ? `${event}:${options.dedupeKey}` : "";
    for (const adapter of adapters) {
      if (adapter.purpose === "analytics" && !consent.analytics) continue;
      if (adapter.purpose === "marketing" && !consent.marketing) continue;
      try {
        let byAdapter = emitted.get(scope);
        if (!byAdapter) emitted.set(scope, byAdapter = new Map());
        let keys = byAdapter.get(adapter);
        if (!keys) byAdapter.set(adapter, keys = new Set());
        if (dedupeKey && (keys.has(dedupeKey) || keys.size >= 4096)) continue;
        adapter.track(event, safe);
        // Bound observational memory. No booking truth is persisted.
        if (dedupeKey) keys.add(dedupeKey);
      } catch { /* Analytics must never affect product behavior. */ }
    }
  } catch { /* Includes invalid runtime inputs and unavailable browser APIs. */ }
}

export function classifyAnalyticsError(error: unknown): ErrorCategory {
  const status = typeof error === "object" && error !== null && "status" in error
    ? Number((error as { status?: unknown }).status)
    : undefined;
  const issue = typeof error === "object" && error !== null && "issue" in error
    ? (error as { issue?: { kind?: string } }).issue
    : undefined;
  if (status === 0) return "network";
  if (status === 401 || status === 403) return "authentication";
  if (status === 400 || status === 422) {
    if (issue?.kind === "STALE_OFFER") return "selection_expired";
    return "validation";
  }
  return "unknown";
}

export function recommendationSource(value?: string | null): "live" | "synthetic" | "unknown" {
  if (value === "LIVE") return "live";
  if (value === "SYNTHETIC") return "synthetic";
  return "unknown";
}

function captureAttribution(): void {
  if (!consent.analytics || typeof window === "undefined") return;
  const params = new URLSearchParams(window.location.search.slice(0, 4096));
  const next: Partial<CampaignAttribution> = {};
  for (const key of ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"] as const) {
    const value = normalizeCampaignValue(params.get(key));
    if (value) next[key] = value;
  }
  // Preserve a permitted campaign across reloads without campaign parameters.
  if (Object.keys(next).length === 0 && readAttribution()) return;
  const referrer = referrerOrigin(document.referrer);
  if (referrer) next.referrer_origin = referrer;
  const landingPath = normalizeLandingPath(window.location.pathname);
  if (landingPath) next.landing_path = landingPath;
  if (Object.keys(next).length <= (next.captured_at ? 1 : 0)) return;
  writeAttribution({ ...next, captured_at: new Date().toISOString() });
}

function attributionProps(): EventProps {
  const attribution = readAttribution();
  if (!attribution) return {};
  const out: EventProps = {};
  for (const key of ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"] as const) {
    const value = normalizeCampaignValue(attribution[key]);
    if (value) out[key] = value;
  }
  if (["internal", "external"].includes(attribution.referrer_origin ?? "")) out.referrer_origin = attribution.referrer_origin!;
  const path = normalizeLandingPath(attribution.landing_path ?? "");
  if (path) out.landing_path = path;
  return out;
}

function readAttribution(): CampaignAttribution | null {
  try {
    if (!consent.analytics) return null;
    const raw = sessionStorage.getItem(ATTRIBUTION_KEY);
    if (!raw) return null;
    if (raw.length > 2048) throw new Error();
    const parsed = JSON.parse(raw) as CampaignAttribution;
    const age = Date.now() - Date.parse(parsed.captured_at);
    if (!Number.isFinite(age) || age < 0 || age > ATTRIBUTION_MAX_AGE_MS) throw new Error();
    return parsed;
  } catch {
    try { sessionStorage.removeItem(ATTRIBUTION_KEY); } catch { /* unavailable */ }
    return null;
  }
}

function writeAttribution(value: CampaignAttribution): void {
  try {
    sessionStorage.setItem(ATTRIBUTION_KEY, JSON.stringify(value));
  } catch {
    /* Storage denial just means no cross-screen attribution. */
  }
}

function normalizeCampaignValue(value: unknown): string | undefined {
  if (typeof value !== "string" || value.length > MAX_VALUE_LENGTH) return undefined;
  const cleaned = value.trim().toLowerCase();
  // Campaign labels, never URLs, encoded content, arbitrary text or identifiers.
  if (!/^[a-z][a-z0-9_-]{0,79}$/.test(cleaned) || /\d{7,}/.test(cleaned)) return undefined;
  return cleaned;
}

function normalizeLandingPath(path: string): string | undefined {
  if (path === "/" || path === "/destinations") return path;
  if (/^\/destinations\/[a-z-]{1,60}$/.test(path)) return "/destinations/:slug";
  if (/^\/inspiration\/[a-z-]{1,60}$/.test(path)) return "/inspiration/:slug";
  if (path === "/destinations/:slug" || path === "/inspiration/:slug") return path;
  return undefined;
}

function referrerOrigin(referrer: string): string | undefined {
  try {
    if (!referrer || referrer.length > 4096) return undefined;
    const url = new URL(referrer);
    if (url.protocol !== "http:" && url.protocol !== "https:") return undefined;
    return url.origin === window.location.origin ? "internal" : "external";
  } catch { return undefined; }
}

function viewportClass(): "mobile" | "tablet" | "desktop" {
  const width = window.innerWidth;
  if (width < 720) return "mobile";
  if (width < 1024) return "tablet";
  return "desktop";
}

const devDiagnosticsAdapter: AnalyticsAdapter = {
  purpose: "essential",
  track(event, props) {
    if (import.meta.env.DEV) {
      // eslint-disable-next-line no-console
      console.debug("[detoura:analytics]", event, props);
    }
  },
};

const firstPartyAdapter: AnalyticsAdapter = {
  purpose: "analytics",
  track(event, props) {
    const mapped = toLegacyFunnelEvent(event, props);
    if (!mapped) return;
    enqueueFirstParty(mapped);
  },
};

interface FirstPartyEvent {
  event: string;
  tier?: ServiceTier;
  props?: EventProps;
}

let sessionKey = "";
let visitorKey = "";
let queue: FirstPartyEvent[] = [];
let timer: number | null = null;

function enqueueFirstParty(event: FirstPartyEvent): void {
  ensureKeys();
  queue.push(event);
  if (queue.length >= 12) flushFirstParty();
  else if (timer === null) {
    timer = window.setTimeout(() => {
      timer = null;
      flushFirstParty();
    }, 2500);
  }
}

function flushFirstParty(): void {
  try {
    if (!consent.analytics) { queue = []; return; }
    if (queue.length === 0) return;
    const body = JSON.stringify({ session_key: sessionKey, visitor_key: visitorKey, events: queue.slice(0, 50) });
    queue = [];
    try {
      if (navigator.sendBeacon?.(`${BASE}/events`, new Blob([body], { type: "application/json" }))) return;
    } catch { /* Fall back to fetch if beacon is denied. */ }
    void fetch(`${BASE}/events`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true,
    }).catch(() => undefined);
  } catch { /* Timers and pagehide must also be non-throwing. */ }
}

function ensureKeys(): void {
  if (sessionKey || visitorKey) return;
  try { sessionKey = storageKey(sessionStorage, "detoura.fk.s"); } catch { /* unavailable */ }
  try { visitorKey = storageKey(localStorage, "detoura.fk.v"); } catch { /* unavailable */ }
}

function storageKey(store: Storage, name: string): string {
  try {
    const existing = store.getItem(name);
    if (existing && /^[a-f0-9]{24}$/.test(existing)) return existing;
    const value = randomKey();
    store.setItem(name, value);
    return value;
  } catch {
    return "";
  }
}

function randomKey(): string {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function toLegacyFunnelEvent(event: AnalyticsEventName, props: EventProps): FirstPartyEvent | null {
  const tier = props.tier === "BASIC" || props.tier === "ALL_IN_ONE" ? props.tier : undefined;
  const legacyProps = {
    search_mode: props.search_mode,
    result_count: props.result_count,
    rank: props.recommendation_rank,
    outcome: props.booking_state ?? props.payment_state ?? props.error_category,
    repeat: props.deeper,
  };
  switch (event) {
    case "search_started": return { event: "SEARCH", props: legacyProps };
    case "search_completed": return { event: "RESULT_VIEW", props: legacyProps };
    case "journey_viewed": return { event: "TRIP_OPEN", props: legacyProps };
    case "service_tier_selected": return { event: "TIER_SELECTED", tier, props: legacyProps };
    case "traveler_details_completed": return { event: "REVIEW", tier, props: legacyProps };
    case "booking_confirmation_started": return { event: "CONFIRM", tier, props: legacyProps };
    case "booking_confirmed": return { event: "BOOKED", tier, props: legacyProps };
    case "promo_applied": return { event: "PROMO_APPLIED", tier, props: { outcome: "applied" } };
    case "booking_failed": return { event: "FAILED", tier, props: legacyProps };
    default: return null;
  }
}

if (typeof window !== "undefined") {
  window.addEventListener("pagehide", flushFirstParty);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushFirstParty();
  });
}
