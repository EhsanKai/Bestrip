/* Typed mirrors of the Detoura v1 API contract.
 *
 * These deliberately describe the *product* API (`/api/v1/...`), not the
 * engine's internal `PlanResult`. The frontend has no concept of a beam, a
 * Pareto frontier or a search state, and adding one would couple every screen
 * to an optimizer implementation detail.
 */

export type SearchMode = "QUICK" | "SMART" | "DEEP";
export type ProfileName = "CHEAPEST" | "BEST_VALUE" | "ADVENTURE";
export type Interest =
  | "culture"
  | "history"
  | "food"
  | "nature"
  | "nightlife"
  | "architecture"
  | "shopping"
  | "museums"
  | "beaches"
  | "family_friendly"
  | "romance"
  | "adventure";

export type AccommodationPreference = "CHEAPEST" | "BALANCED" | "QUALITY";
export type PriceFreshness = "FRESH" | "RECENT" | "STALE" | "UNKNOWN";
export type AvailabilityStatus = "AVAILABLE" | "LIMITED" | "SOLD_OUT" | "UNKNOWN";
export type ConfidenceLevel = "HIGH" | "GOOD" | "LIMITED";
export type IntensityBand = "LOW" | "MODERATE" | "HIGH";

/** Every way a provider can fail, kept distinct from "nothing matched". */
export type ProviderFailureKind =
  | "NO_RESULTS"
  | "SOLD_OUT"
  | "TIMEOUT"
  | "UNAVAILABLE"
  | "MALFORMED_RESPONSE"
  | "CURRENCY_UNAVAILABLE"
  | "STALE_OFFER"
  | "AUTHENTICATION_FAILED"
  | "RATE_LIMITED";

export interface TripSearchRequest {
  origin: string;
  date_from: string;
  date_to: string;
  duration_days: number;
  date_flexible?: boolean;
  travelers: number;
  budget: number;
  profile?: ProfileName;
  search_mode?: SearchMode;
  interests?: Interest[];
  disliked?: Interest[];
  preferred_destinations?: string[];
  avoided_destinations?: string[];
  previously_visited?: string[];
  accommodation_preference?: AccommodationPreference;
  preferred_city_count?: number | null;
  transport?: string[];
}

export interface CostBreakdown {
  transport: number;
  accommodation: number;
  ground_transfer: number;
  total: number;
}

export interface StaySummary {
  city: string;
  arrival: string;
  departure: string;
  nights: number;
  cost: number;
  name: string | null;
  tier: string | null;
  type: string | null;
  rating: number | null;
  location_score: number | null;
  free_cancellation: boolean;
  usable_minutes: number;
  rooms_available: number | null;
  cheapest_alternative_cost: number | null;
  premium: number;
  /** Prose, only when the data supports it. Null means say nothing. */
  value_note: string | null;
}

export interface LegSummary {
  from: string;
  to: string;
  departure: string;
  arrival: string;
  minutes: number;
  mode: string;
  operator: string;
  price_per_person: number;
  seats_available: number | null;
}

export interface DestinationMatch {
  city: string;
  /** 0-1. The UI shows a band, never the number on its own. */
  match: number;
  quality: number;
  stay_quality: number;
  usable_days: number;
  strengths: Interest[];
  weaknesses: Interest[];
  disliked_present: Interest[];
  previously_visited: boolean;
  note: string;
}

export interface BaselineComparisonDTO {
  destination: string;
  total_price: number;
  nights: number;
  usable_hours: number;
  price_delta: number;
  extra_cities: number;
  extra_usable_hours: number;
  extra_travel_minutes: number;
}

export interface ConfidenceReason {
  label: string;
  positive: boolean;
}

export interface RecommendationConfidence {
  level: ConfidenceLevel;
  /** The human sentence, computed by the backend so the UI never invents it. */
  label: string;
  reasons: ConfidenceReason[];
}

export interface TripRecommendation {
  id: string;
  rank: number;
  route: string;
  route_nodes: string[];
  cities: string[];
  origin_airport: string;
  return_airport: string;
  departure: string;
  arrival: string;
  duration_days: number;
  nights: number[];

  total_price: number;
  price_per_person: number;
  currency: string;
  costs: CostBreakdown;

  usable_hours: number;
  travel_hours: number;
  transfer_minutes: number;

  travel_intensity: number;
  intensity_band: IntensityBand;
  experience_score: number;
  preference_match: number;
  accommodation_score: number;
  travel_value: number;
  profile: ProfileName;

  confidence: RecommendationConfidence;
  price_freshness: PriceFreshness;
  availability: AvailabilityStatus;

  baseline_comparison: BaselineComparisonDTO | null;
  highlights: string[];
  tradeoff: string | null;
  why_we_like_it: string | null;

  stays: StaySummary[];
  legs: LegSummary[];
  destination_matches: DestinationMatch[];
}

export interface SearchDiagnostics {
  mode: SearchMode;
  elapsed_seconds: number;
  itineraries_considered: number;
  alternatives_evaluated: number;
  destinations_explored: number;
  rounds: number;
  deeper_search_available: boolean;
  /** Set when the engine could not search as fully as it wanted to. */
  notes: string[];
}

export interface ProviderIssue {
  kind: ProviderFailureKind;
  provider: string;
  message: string;
  /** True when it is safe to tell the user "try again". */
  retryable: boolean;
}

export interface TripSearchResponse {
  request_id: string;
  origin: string;
  origin_airports: string[];
  currency: string;
  profile: ProfileName;
  recommendations: TripRecommendation[];
  baseline: BaselineComparisonDTO | null;
  diagnostics: SearchDiagnostics;
  /** Non-empty means some data was missing or degraded, NOT that nothing matched. */
  issues: ProviderIssue[];
  /** Only set when nothing matched; carries the relaxations worth offering. */
  no_results: NoResultsGuidance | null;
}

export interface RelaxationSuggestion {
  label: string;
  description: string;
  /** A partial request the UI can merge and re-run. */
  patch: Partial<TripSearchRequest>;
}

export interface NoResultsGuidance {
  reason: string;
  closest_price: number | null;
  requested_budget: number;
  suggestions: RelaxationSuggestion[];
}

export interface BudgetStep {
  budget: number;
  feasible: boolean;
  trips_found: number;
  best_price: number | null;
  best_route: string | null;
  unlocks: string[][];
  is_threshold: boolean;
}

export interface BudgetSensitivityResponse {
  currency: string;
  profile: ProfileName;
  minimum_feasible_budget: number | null;
  steps: BudgetStep[];
}

export interface OriginAirport {
  code: string;
  name: string;
  city: string;
  distance_km: number;
  transfer_price: number | null;
  transfer_minutes: number | null;
}

export interface OriginResponse {
  origin: string;
  airports: OriginAirport[];
}

export interface DestinationSummary {
  id: string;
  name: string;
  country: string;
  recommended_min_days: number;
  recommended_max_days: number;
  strengths: Interest[];
}

/** Thrown by the client for a non-2xx response, carrying the typed issue. */
export class DetouraApiError extends Error {
  status: number;
  issue?: ProviderIssue;

  constructor(message: string, status: number, issue?: ProviderIssue) {
    super(message);
    this.name = "DetouraApiError";
    this.status = status;
    this.issue = issue;
  }
}

/* ------------------------------------------------------------------ */
/* Re-checking a saved trip (V6.1)                                     */
/* ------------------------------------------------------------------ */

export type RecheckStatus =
  | "UNCHANGED"
  | "PRICE_CHANGED"
  | "PARTIALLY_UNAVAILABLE"
  | "UNAVAILABLE"
  /** A failure of ours, not news about the trip. Must never be drawn as bad
   *  news: the trip may be perfectly bookable and we simply could not look. */
  | "UNVERIFIABLE";

export type RecheckComponentState =
  | "FOUND"
  | "SOLD_OUT"
  | "GONE"
  | "UNVERIFIABLE"
  /** Included at its saved price and deliberately not re-checked. */
  | "CARRIED";

export interface RecheckLeg {
  from: string;
  to: string;
  departure: string;
  operator: string;
  price_per_person: number;
}

export interface RecheckStay {
  city: string;
  arrival: string;
  departure: string;
  cost: number;
  name: string | null;
}

/** The trip's transfers at their saved price. Sent so the totals reconcile —
 *  leaving it out makes the re-checked total low by exactly this amount, and
 *  an unchanged trip is then announced as a saving. */
export interface RecheckTransfer {
  cost: number;
  label?: string;
}

export interface TripRecheckRequest {
  trip_id: string;
  travelers: number;
  saved_price: number;
  saved_at?: string;
  legs: RecheckLeg[];
  stays: RecheckStay[];
  transfers: RecheckTransfer[];
}

export interface RecheckComponent {
  label: string;
  state: RecheckComponentState;
  saved_price: number;
  current_price: number | null;
  change: number | null;
  detail: string;
}

export interface TripRecheckResponse {
  trip_id: string;
  status: RecheckStatus;
  message: string;
  checked_at: string;
  saved_price: number;
  /** Null whenever any part could not be priced. Never a partial sum. */
  current_price: number | null;
  price_change: number | null;
  price_change_pct: number | null;
  price_freshness: PriceFreshness;
  legs: RecheckComponent[];
  stays: RecheckComponent[];
  transfers: RecheckComponent[];
  issues: ProviderIssue[];
}
