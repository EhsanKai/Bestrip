// Runtime privacy boundary for the existing analytics seam, not a delivery API.
import type { AnalyticsEventName } from "./analytics";

type Scalar = string | number | boolean;
type Validator = (value: unknown) => value is Scalar;
const oneOf = (...values: string[]): Validator => (v): v is string => typeof v === "string" && values.includes(v);
const count: Validator = (v): v is number => typeof v === "number" && Number.isInteger(v) && v >= 0 && v <= 10000;
const boolean: Validator = (v): v is boolean => typeof v === "boolean";
const tier = oneOf("BASIC", "ALL_IN_ONE");
const category = oneOf("network", "validation", "authentication", "selection_expired", "payment_failed", "payment_unknown", "booking_failed", "recovery_required", "unknown");
const currency: Validator = (v): v is string => typeof v === "string" && /^(EUR|USD|GBP|CHF|CAD|AUD|JPY|SEK|NOK|DKK|PLN|CZK)$/.test(v);
const search = { search_mode: oneOf("QUICK", "SMART", "DEEP"), deeper: boolean };
const source = oneOf("live", "synthetic", "unknown");
const recommendation = { recommendation_rank: count, recommendation_source: source, bookable: boolean, leg_count: count, currency };
const error = { tier, error_category: category };
const schemas: Record<AnalyticsEventName, Record<string, Validator>> = {
  landing_viewed: { landing_context: oneOf("home", "destinations", "destination", "inspiration") },
  search_started: search,
  search_completed: { ...search, result_count: count, no_results: boolean, recommendation_source: source, currency },
  search_failed: { ...search, error_category: category },
  recommendation_selected: recommendation,
  journey_viewed: recommendation,
  checkout_started: { tier, recommendation_source: source, leg_count: count, traveler_count: count, currency },
  traveler_details_completed: { tier, traveler_count: count },
  payment_authorization_started: { tier, currency },
  payment_authorized: { tier, currency, payment_state: oneOf("AUTHORIZED") },
  payment_failed: { ...error, payment_state: oneOf("FAILED", "CANCELLED") },
  payment_unknown: { ...error, payment_state: oneOf("UNKNOWN", "RECONCILIATION_REQUIRED") },
  payment_request_failed: error,
  booking_confirmation_started: { tier },
  booking_confirmation_failed: error,
  self_service_ready: { tier },
  service_tier_selected: { tier },
  promo_applied: { tier, applied: (v): v is boolean => v === true },
  booking_confirmed: { tier, booking_state: oneOf("confirmed") },
  booking_recovery_required: { ...error, booking_state: oneOf("recovery_required") },
  booking_failed: { ...error, booking_state: oneOf("failed") },
  my_trips_viewed: { trip_count: count, empty: boolean },
  document_downloaded: { document_type: oneOf("receipt", "invoice", "credit_note", "unknown") },
};

export function sanitizeEvent(event: AnalyticsEventName, input: unknown): Record<string, Scalar> | null {
  if (!Object.hasOwn(schemas, event) || !input || typeof input !== "object") return null;
  const data = input as Record<string, unknown>;
  const out: Record<string, Scalar> = {};
  for (const [key, validate] of Object.entries(schemas[event])) {
    const value = data[key];
    if (validate(value)) out[key] = value;
  }
  // Reject malformed terminal events instead of publishing an ambiguous conversion.
  for (const key of ["payment_state", "booking_state", "document_type"] as const) {
    if (key in schemas[event] && !(key in out)) return null;
  }
  return out;
}
