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
  carrier_name: string;
  operating_carrier: string;
  operating_flight_number: string;
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

export interface TicketOperation {
  operation_id: string;
  booking_id: string;
  sequence: number;
  kind: "CANCELLATION" | "CHANGE" | "RECOVERY";
  state: string;
  provider: string;
  provider_order_id: string | null;
  reason: string;
  actor: string;
  created_at: string;
  updated_at: string;
  quote: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  is_terminal: boolean;
}

export interface OpsBookingDetail extends OpsBookingSummary {
  reconfirm_note: string;
  items: OpsBookingItem[];
  economics: OpsEconomics | null;
  audit: OpsAuditEvent[];
  operations: TicketOperation[];
}

export interface AirlineStat {
  iata_code: string;
  name: string;
  logo_key: string;
  tickets_total: number;
  tickets_issued: number;
  tickets_failed: number;
  bookings: number;
  supplier_spend: number;
  avg_fare: number | null;
  issuance_failure_rate: number | null;
  detoura_revenue_gross: number;
  detoura_margin_known: number | null;
  margin_bookings: number;
  cancellations: number;
  changes: number;
  recoveries: number;
  cancellation_rate: number;
  change_rate: number;
  currency: string;
}

export interface AirlineReport {
  dimension: "marketing" | "operating";
  test_data: boolean;
  window: { since: string | null; until: string | null };
  airlines: AirlineStat[];
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

export interface MarkupPolicyConfig {
  basic_percentage: number;
  basic_fixed_fee: number;
  all_in_one_percentage: number;
  all_in_one_fixed_fee: number;
  max_percentage: number;
  max_fixed_fee: number;
  min_total_fee: number;
  max_total_fee: number;
}

export interface MarkupPolicy {
  policy_id: string;
  version: number;
  label: string;
  active: boolean;
  created_at: string;
  config: MarkupPolicyConfig | null;
  bookings_priced: number;
}

export interface MarkupPreviewLine {
  tier: string;
  supplier_total: number;
  detoura_service_fee: number;
  detoura_markup: number;
  detoura_fee_total: number;
  customer_total: number;
  bounded: boolean;
  explanation: string[];
}

export interface MarkupPreview {
  policy_ref: string;
  invariant_ok: boolean;
  lines: MarkupPreviewLine[];
}

export interface OpsPromo {
  code: string;
  label: string;
  enabled: boolean;
  kind: string;
  value: number;
  currency: string;
  target: string;
  starts_at: string | null;
  ends_at: string | null;
  global_limit: number | null;
  per_user_limit: number | null;
  min_order_value: number;
  max_discount: number | null;
  eligible_tiers: string[];
  redemptions: number;
  discount_total: number;
  revenue_impact: number;
  bookings_with_code: number;
}

export interface OpsPromoDetail extends OpsPromo {
  redemption_log: {
    booking_id: string;
    discount_amount: number;
    currency: string;
    redeemed_at: string;
  }[];
}

export interface FinanceSummary {
  bookings: number;
  currency: string;
  gross_booking_value: number;
  supplier_cost: number;
  detoura_revenue_gross: number;
  detoura_revenue_net: number;
  detoura_service_fees: number;
  detoura_markup: number;
  promo_discounts: number;
  provider_cost_estimate_known: number;
  provider_cost_estimate_unknown_bookings: number;
  payment_cost_known: number;
  payment_cost_unknown_bookings: number;
  refund_known: number;
  refund_unknown_bookings: number;
  recovery_cost_known: number;
  recovery_cost_unknown_bookings: number;
  gross_contribution_known: number;
  bookings_with_unknown_costs: number;
  avg_detoura_fee: number;
  avg_margin_known: number | null;
  avg_margin_excluded_bookings: number;
  by_tier: Record<
    string,
    {
      bookings: number;
      gross_booking_value: number;
      detoura_revenue_gross: number;
      detoura_revenue_net: number;
      avg_detoura_fee: number;
      conversion: number | null;
      tier_selected: number;
    }
  >;
  test_data: boolean;
  window: { since: string | null; until: string | null };
}

export interface AnalyticsReport {
  test_data: boolean;
  window: { since: string | null; until: string | null };
  event_counts: Record<string, number>;
  funnel: {
    stage: string;
    sessions: number;
    drop_from_prev: number | null;
    rate_from_prev: number | null;
  }[];
  tier_selection: Record<
    string,
    { selected: number; booked: number; conversion: number | null }
  >;
  promo_impact: {
    sessions_applied_promo: number;
    of_those_booked: number;
    conversion: number | null;
  };
  repeat_search: {
    visitors_who_searched: number;
    repeat_searchers: number;
    repeat_rate: number | null;
    avg_searches_per_visitor: number;
  };
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

  // --- C2: commercial + promo management, finance, analytics ---
  policies: () => req<MarkupPolicy[]>("/commercial/policies"),
  createPolicy: (body: MarkupPolicyConfig & { label: string; activate: boolean }) =>
    req<MarkupPolicy>("/commercial/policies", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  activatePolicy: (policyId: string, version: number) =>
    req<MarkupPolicy>(
      `/commercial/policies/${encodeURIComponent(policyId)}/${version}/activate`,
      { method: "POST" },
    ),
  previewPolicy: (body: {
    supplier_total: number;
    ticket_count?: number;
    currency?: string;
    policy_id?: string;
    version?: number;
    draft?: MarkupPolicyConfig & { label?: string };
  }) =>
    req<MarkupPreview>("/commercial/preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  promos: () => req<OpsPromo[]>("/promos"),
  promo: (code: string) =>
    req<OpsPromoDetail>(`/promos/${encodeURIComponent(code)}`),
  upsertPromo: (body: Record<string, unknown>) =>
    req<OpsPromoDetail>("/promos", { method: "POST", body: JSON.stringify(body) }),
  setPromoEnabled: (code: string, enabled: boolean) =>
    req<OpsPromoDetail>(
      `/promos/${encodeURIComponent(code)}/${enabled ? "enable" : "disable"}`,
      { method: "POST" },
    ),

  // --- C3: ticket operations (cancellation / change / recovery) ---
  ticketOp: {
    cancellationEligibility: (bookingId: string, seq: number) =>
      req<TicketOperation>(
        `/bookings/${encodeURIComponent(bookingId)}/tickets/${seq}/cancellation/eligibility`,
        { method: "POST" },
      ),
    changeCapability: (bookingId: string, seq: number) =>
      req<TicketOperation>(
        `/bookings/${encodeURIComponent(bookingId)}/tickets/${seq}/change/capability`,
        { method: "POST" },
      ),
    startRecovery: (bookingId: string, seq: number, reason: string) =>
      req<TicketOperation>(
        `/bookings/${encodeURIComponent(bookingId)}/tickets/${seq}/recovery`,
        { method: "POST", body: JSON.stringify({ reason }) },
      ),
    step: (
      operationId: string,
      kind: "cancellation" | "change" | "recovery",
      step: "approve" | "execute" | "candidate" | "abandon",
      body: Record<string, unknown> = {},
    ) =>
      req<TicketOperation>(
        `/operations/${encodeURIComponent(operationId)}/${kind}/${step}`,
        { method: "POST", body: JSON.stringify(body) },
      ),
    get: (operationId: string) =>
      req<TicketOperation>(`/operations/${encodeURIComponent(operationId)}`),
  },
  airlines: (
    params: { dimension?: "marketing" | "operating"; since?: string; until?: string } = {},
  ) => {
    const q = new URLSearchParams();
    if (params.dimension) q.set("dimension", params.dimension);
    if (params.since) q.set("since", params.since);
    if (params.until) q.set("until", params.until);
    const s = q.toString();
    return req<AirlineReport>(`/airlines${s ? `?${s}` : ""}`);
  },

  finance: (win: { since?: string; until?: string } = {}) => {
    const q = new URLSearchParams();
    if (win.since) q.set("since", win.since);
    if (win.until) q.set("until", win.until);
    const s = q.toString();
    return req<FinanceSummary>(`/finance${s ? `?${s}` : ""}`);
  },
  analytics: (win: { since?: string; until?: string } = {}) => {
    const q = new URLSearchParams();
    if (win.since) q.set("since", win.since);
    if (win.until) q.set("until", win.until);
    const s = q.toString();
    return req<AnalyticsReport>(`/analytics${s ? `?${s}` : ""}`);
  },
};
