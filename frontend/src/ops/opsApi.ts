/* The ops console's own API client. Separate from the product client: it
 * carries a bearer session token and talks only to /api/v1/ops. */

const BASE = (import.meta.env.VITE_API_BASE || "/api/v1") + "/ops";
const TOKEN_KEY = "detoura.ops.session";

export function getToken(): string {
  try {
    return sessionStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

export function setToken(token: string): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode - session stays in memory only for this page load */
  }
}

export class OpsError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
        ...(init?.headers ?? {}),
      },
    });
  } catch (e) {
    throw new OpsError("Could not reach Detoura Ops.", 0);
  }
  if (res.status === 204) return undefined as T;
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    /* ignore */
  }
  if (!res.ok) {
    const detail = (body as { detail?: { message?: string } })?.detail;
    const msg =
      (typeof detail === "object" && detail?.message) ||
      (typeof detail === "string" ? detail : "") ||
      `Request failed (${res.status})`;
    throw new OpsError(msg, res.status);
  }
  return body as T;
}

/* --- types ---------------------------------------------------------------- */

export interface OpsStatus {
  enabled: boolean;
  test_mode: boolean;
}

export interface OpsAction {
  action: string;
  enabled: boolean;
  reason: string;
}

export interface OpsBookingItem {
  sequence: number;
  origin_city: string;
  origin_airport: string;
  destination_city: string;
  destination_airport: string;
  departure: string | null;
  arrival: string | null;
  carrier: string;
  flight_number: string;
  offer_id: string;
  provider: string;
  duffel_order_id: string | null;
  quoted_price: number;
  current_price: number | null;
  booked_price: number | null;
  currency: string;
  cabin_baggage: string;
  checked_baggage: string;
  required: boolean;
  state: string;
  detail: string;
  actions: OpsAction[];
}

export interface OpsBookingSummary {
  booking_id: string;
  session_ref: string;
  journey_reference: string;
  created_at: string;
  updated_at: string;
  mode: string;
  phase: string;
  phase_label: string;
  trip_label: string;
  route_cities: string[];
  party_size: number;
  lead_name: string;
  lead_email: string;
  currency: string;
  service_tier: string;
  discovered_total: number;
  current_total: number | null;
  customer_total: number | null;
  recovery_state: string;
  ticket_count: number;
  confirmed_count: number;
}

export interface OpsEconomics {
  currency: string;
  supplier_cost: number;
  detoura_service_fee: number;
  detoura_markup: number;
  discount: number;
  customer_price: number;
  detoura_gross_revenue: number;
  markup_policy: string;
  promo_code: string | null;
  provider_cost_estimate: number | null;
  payment_cost: number | null;
  refund: number | null;
  recovery_cost: number | null;
  contribution_margin: number | null;
  has_unknown_costs: boolean;
}

export interface OpsAuditEvent {
  id: number;
  ts: string;
  actor: string;
  action: string;
  target_type: string;
  target_id: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  note: string;
}

export interface OpsBookingDetail extends OpsBookingSummary {
  reconfirm_note: string;
  items: OpsBookingItem[];
  economics: OpsEconomics | null;
  audit: OpsAuditEvent[];
}

export interface OpsBookingsPage {
  bookings: OpsBookingSummary[];
  counts_by_phase: Record<string, number>;
  recovery_count: number;
}

export interface OpsRecoveryPage {
  items: OpsBookingSummary[];
  by_state: Record<string, number>;
}

export interface OpsOverview {
  test_mode: boolean;
  total_bookings: number;
  counts_by_phase: Record<string, number>;
  recovery_count: number;
  audit_events: number;
}

/* --- calls -------------------------------------------------------------- */

export const opsApi = {
  status: () => req<OpsStatus>("/status"),
  login: (token: string) =>
    req<{ session_token: string; expires_in: number }>("/session", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),
  logout: () => req<{ ok: boolean }>("/session/logout", { method: "POST" }),
  overview: () => req<OpsOverview>("/overview"),
  bookings: (params: {
    phase?: string;
    recovery?: boolean;
    search?: string;
    limit?: number;
  }) => {
    const q = new URLSearchParams();
    if (params.phase) q.set("phase", params.phase);
    if (params.recovery) q.set("recovery", "true");
    if (params.search) q.set("search", params.search);
    if (params.limit) q.set("limit", String(params.limit));
    const s = q.toString();
    return req<OpsBookingsPage>(`/bookings${s ? `?${s}` : ""}`);
  },
  booking: (id: string) =>
    req<OpsBookingDetail>(`/bookings/${encodeURIComponent(id)}`),
  recovery: () => req<OpsRecoveryPage>("/recovery"),
  audit: (params: { limit?: number; target_id?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.limit) q.set("limit", String(params.limit));
    if (params.target_id) q.set("target_id", params.target_id);
    const s = q.toString();
    return req<OpsAuditEvent[]>(`/audit${s ? `?${s}` : ""}`);
  },
};
