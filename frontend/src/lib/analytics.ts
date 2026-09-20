/* Detoura product analytics seam.
 *
 * Product code emits typed Detoura events only. This module owns privacy,
 * attribution, consent, adapter selection and deduplication, so future vendors
 * can be connected without putting SDK calls in screens.
 */

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
  landing_viewed: { landing_context: "home" };
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
  booking_confirmation_started: Pick<CommonProps, "tier">;
  booking_confirmed: Pick<CommonProps, "tier"> & { booking_state: "confirmed" | "self_service_ready" };
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
  includeAttribution?: boolean;
}

let consent: AnalyticsConsent = { analytics: false, marketing: false };
let adapters: AnalyticsAdapter[] = [];
let initialized = false;
const emitted = new Set<string>();

export function init(config: AnalyticsConfig = {}): void {
  if (initialized) return;
  initialized = true;
  consent = { ...consent, ...config.consent };
  captureAttribution();

  if (import.meta.env.DEV || config.diagnostics || import.meta.env.VITE_ANALYTICS_DEBUG === "true") {
    adapters.push(devDiagnosticsAdapter);
  }
  if (import.meta.env.VITE_ANALYTICS_FIRST_PARTY === "true") {
    adapters.push(firstPartyAdapter);
  }
}

export function setAnalyticsConsent(next: Partial<AnalyticsConsent>): void {
  consent = { ...consent, ...next };
}

export function track<K extends AnalyticsEventName>(
  event: K,
  props: AnalyticsEventPayloads[K],
  options: TrackOptions = {},
): void {
  const dedupeKey = options.dedupeKey ? `${event}:${options.dedupeKey}` : "";
  if (dedupeKey) {
    if (emitted.has(dedupeKey)) return;
    emitted.add(dedupeKey);
  }
  const safe = sanitizeProps({
    ...props,
    viewport_class: "viewport_class" in props ? props.viewport_class : viewportClass(),
    ...(options.includeAttribution === false ? {} : attributionProps()),
  });
  for (const adapter of adapters) {
    if (adapter.purpose === "analytics" && !consent.analytics) continue;
    if (adapter.purpose === "marketing" && !consent.marketing) continue;
    try {
      adapter.track(event, safe);
    } catch {
      /* Analytics must never affect product behavior. */
    }
  }
}

export function classifyAnalyticsError(error: unknown): ErrorCategory {
  const status = typeof error === "object" && error !== null && "status" in error
    ? Number((error as { status?: unknown }).status)
    : 0;
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
  if (typeof window === "undefined") return;
  const params = new URLSearchParams(window.location.search);
  const next: Partial<CampaignAttribution> = {};
  for (const key of ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"] as const) {
    const value = normalizeCampaignValue(params.get(key));
    if (value) next[key] = value;
  }
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
  return sanitizeProps({
    utm_source: attribution.utm_source,
    utm_medium: attribution.utm_medium,
    utm_campaign: attribution.utm_campaign,
    utm_content: attribution.utm_content,
    utm_term: attribution.utm_term,
    referrer_origin: attribution.referrer_origin,
    landing_path: attribution.landing_path,
  });
}

function readAttribution(): CampaignAttribution | null {
  try {
    const raw = sessionStorage.getItem(ATTRIBUTION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as CampaignAttribution;
    if (Date.now() - Date.parse(parsed.captured_at) > ATTRIBUTION_MAX_AGE_MS) {
      sessionStorage.removeItem(ATTRIBUTION_KEY);
      return null;
    }
    return parsed;
  } catch {
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

function normalizeCampaignValue(value: string | null): string | undefined {
  const cleaned = (value ?? "").trim().slice(0, MAX_VALUE_LENGTH);
  if (!cleaned || /[<>{}"'`\\]/.test(cleaned) || /@|\+?\d{7,}/.test(cleaned)) return undefined;
  return cleaned.replace(/[^\w .:/+-]/g, "").trim() || undefined;
}

function normalizeLandingPath(path: string): string | undefined {
  if (!path || path.length > MAX_VALUE_LENGTH) return undefined;
  return path.startsWith("/") && !/[<>{}"'`\\]/.test(path) ? path : undefined;
}

function referrerOrigin(referrer: string): string | undefined {
  try {
    if (!referrer) return undefined;
    const url = new URL(referrer);
    if (url.origin === window.location.origin) return undefined;
    return normalizeCampaignValue(url.origin);
  } catch {
    return undefined;
  }
}

function viewportClass(): "mobile" | "tablet" | "desktop" {
  const width = window.innerWidth;
  if (width < 720) return "mobile";
  if (width < 1024) return "tablet";
  return "desktop";
}

function sanitizeProps(input: Record<string, unknown>): EventProps {
  const out: EventProps = {};
  for (const [key, value] of Object.entries(input)) {
    if (value === undefined || value === null) continue;
    if (typeof value === "boolean") out[key] = value;
    else if (typeof value === "number" && Number.isFinite(value)) out[key] = Math.round(value * 100) / 100;
    else if (typeof value === "string") {
      const cleaned = normalizeCampaignValue(value);
      if (cleaned) out[key] = cleaned;
    }
  }
  return out;
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
  if (queue.length === 0) return;
  const body = JSON.stringify({
    session_key: sessionKey,
    visitor_key: visitorKey,
    events: queue.slice(0, 50),
  });
  queue = [];
  if (navigator.sendBeacon) {
    navigator.sendBeacon(`${BASE}/events`, new Blob([body], { type: "application/json" }));
    return;
  }
  void fetch(`${BASE}/events`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body,
    keepalive: true,
  }).catch(() => undefined);
}

function ensureKeys(): void {
  if (sessionKey || visitorKey) return;
  sessionKey = storageKey(sessionStorage, "detoura.fk.s");
  visitorKey = storageKey(localStorage, "detoura.fk.v");
}

function storageKey(store: Storage, name: string): string {
  try {
    const existing = store.getItem(name);
    if (existing) return existing;
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
    case "checkout_started": return { event: "TIER_SELECTED", tier, props: legacyProps };
    case "traveler_details_completed": return { event: "REVIEW", tier, props: legacyProps };
    case "booking_confirmation_started": return { event: "CONFIRM", tier, props: legacyProps };
    case "booking_confirmed": return { event: "BOOKED", tier, props: legacyProps };
    case "booking_failed":
    case "booking_recovery_required": return { event: "FAILED", tier, props: legacyProps };
    default: return null;
  }
}

if (typeof window !== "undefined") {
  window.addEventListener("pagehide", flushFirstParty);
  window.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flushFirstParty();
  });
}
