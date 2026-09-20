import type { JourneyDrawerModel } from "../components/journey/JourneyDrawer";
import type { TripRecommendation, TripSearchRequest } from "../api/types";
import { dayMonth, hours, money } from "../lib/format";

const STORAGE_KEY = "detoura-journey-draft-v1";
const STORAGE_VERSION = 1;

export interface JourneyDraft {
  version: 1;
  selectedAt: string;
  trip: TripRecommendation;
  searchContext: {
    travelers: number;
    origin?: string;
    dateFrom?: string;
    dateTo?: string;
  };
}

export function makeJourneyDraft(
  trip: TripRecommendation,
  request: TripSearchRequest | null,
): JourneyDraft {
  return {
    version: STORAGE_VERSION,
    selectedAt: new Date().toISOString(),
    trip,
    searchContext: {
      travelers: request?.travelers ?? 1,
      origin: request?.origin,
      dateFrom: request?.date_from,
      dateTo: request?.date_to,
    },
  };
}

export function toDrawerModel(draft: JourneyDraft): JourneyDrawerModel {
  const { trip, searchContext } = draft;
  const estimatedTotal = trip.total_with_known_baggage ?? trip.total_price;
  const baggageLine = baggageDisplay(trip);
  const providerBookable = Boolean(trip.selection_id);
  return {
    id: trip.id,
    status: providerBookable ? "checkout" : "selected",
    stops: trip.stays.length
      ? trip.stays.map((stay) => ({
          city: stay.city,
          nights: stay.nights,
          dates: `${dayMonth(stay.arrival)}-${dayMonth(stay.departure)}`,
        }))
      : trip.cities.map((city, index) => ({
          city,
          nights: trip.nights[index] ?? 0,
          dates: "Dates from search",
        })),
    dates: `${dayMonth(trip.departure)}-${dayMonth(trip.arrival)}`,
    travelers: `${searchContext.travelers} ${searchContext.travelers === 1 ? "traveller" : "travellers"}`,
    duration: `${trip.duration_days.toFixed(0)} days`,
    characteristics: [
      trip.confidence.label,
      `${trip.price_freshness === "UNKNOWN" ? "Estimated" : "Recommendation"} price`,
      `Supply: ${tripSupplyLabel(trip)}`,
      baggageLine,
      `${hours(trip.usable_hours)} usable time`,
    ].filter(Boolean),
    payable: [{ label: "Checkout", amount: "Not started" }],
    payableNow: "Not established",
    estimates: [
      { label: "Transport", amount: money(trip.costs.transport, trip.currency) },
      { label: "Rooms", amount: money(trip.costs.accommodation, trip.currency) },
      { label: "Transfers", amount: money(trip.costs.ground_transfer, trip.currency) },
      { label: "Baggage", amount: baggageLine },
    ],
    estimatedTripTotal: money(estimatedTotal, trip.currency),
  };
}

export function saveJourneyDraft(draft: JourneyDraft | null) {
  try {
    if (!draft) {
      localStorage.removeItem(STORAGE_KEY);
      return;
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(draft));
  } catch {
    /* Persistence is a convenience. The selected journey remains in memory. */
  }
}

export function loadJourneyDraft(): JourneyDraft | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as unknown;
    if (!isJourneyDraft(parsed)) {
      localStorage.removeItem(STORAGE_KEY);
      return null;
    }
    return {
      ...parsed,
      trip: { ...parsed.trip, selection_id: parsed.trip.selection_id ?? null },
    };
  } catch {
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
    return null;
  }
}

function isJourneyDraft(value: unknown): value is JourneyDraft {
  if (!value || typeof value !== "object") return false;
  const draft = value as Partial<JourneyDraft>;
  const trip = draft.trip as Partial<TripRecommendation> | undefined;
  return (
    draft.version === STORAGE_VERSION &&
    typeof draft.selectedAt === "string" &&
    Boolean(trip) &&
    typeof trip?.id === "string" &&
    Array.isArray(trip?.cities) &&
    typeof trip?.total_price === "number" &&
    typeof trip?.currency === "string" &&
    typeof draft.searchContext?.travelers === "number"
  );
}

function baggageDisplay(trip: TripRecommendation): string {
  if (!trip.baggage) return "Baggage unknown";
  if (trip.baggage.note) return trip.baggage.note;
  if (trip.baggage.total_for_display !== null) {
    return money(trip.baggage.total_for_display, trip.baggage.currency);
  }
  if (trip.baggage.completeness === "PARTIAL" && trip.baggage.known_total > 0) {
    return `${money(trip.baggage.known_total, trip.baggage.currency)} known; some baggage unknown`;
  }
  return "Baggage unknown";
}

function tripSupplyLabel(trip: TripRecommendation): string {
  if (trip.price_freshness === "FRESH" || trip.availability === "AVAILABLE") return "current provider result";
  return "estimated or unconfirmed";
}
