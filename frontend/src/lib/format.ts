/* Formatting helpers.
 *
 * Centralised because the brand has opinions the components should not each
 * re-invent: prices are whole euros unless the cents matter, durations read as
 * a person would say them, and a score is never shown as a bare decimal.
 */

import type {
  AvailabilityStatus,
  ConfidenceLevel,
  IntensityBand,
  Interest,
  PriceFreshness,
  ProfileName,
} from "../api/types";

export function money(amount: number, currency = "EUR"): string {
  const symbol = currency === "EUR" ? "€" : `${currency} `;
  // Cents on a trip total are noise; on a delta of a few euros they are the
  // whole point.
  const decimals = Math.abs(amount) < 100 && !Number.isInteger(amount) ? 2 : 0;
  return `${symbol}${amount.toLocaleString("en-GB", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

export function signedMoney(amount: number, currency = "EUR"): string {
  const sign = amount > 0 ? "+" : amount < 0 ? "−" : "";
  return `${sign}${money(Math.abs(amount), currency)}`;
}

export function hours(value: number): string {
  if (value < 1) return `${Math.round(value * 60)}m`;
  return `${value.toFixed(1)}h`;
}

export function minutesAsHours(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  if (h === 0) return `${m}m`;
  if (m === 0) return `${h}h`;
  return `${h}h ${m}m`;
}

/** 0-1 → a percentage the copy can sit next to. */
export function percent(value: number): string {
  return `${Math.round(value * 100)}%`;
}

export function dayMonth(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
  });
}

export function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString("en-GB", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function weekdayLong(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", {
    weekday: "long",
    day: "numeric",
    month: "long",
  });
}

export const INTEREST_LABELS: Record<Interest, string> = {
  culture: "Culture",
  history: "History",
  food: "Food",
  nature: "Nature",
  nightlife: "Nightlife",
  architecture: "Architecture",
  shopping: "Shopping",
  museums: "Museums",
  beaches: "Beaches",
  family_friendly: "Family",
  romance: "Romance",
  adventure: "Adventure",
};

export const PROFILE_LABELS: Record<ProfileName, string> = {
  CHEAPEST: "Cheapest",
  BEST_VALUE: "Best value",
  ADVENTURE: "Adventure",
};

export const PROFILE_BLURBS: Record<ProfileName, string> = {
  CHEAPEST: "Spend as little as the trip allows.",
  BEST_VALUE: "The best trip your money and time can buy.",
  ADVENTURE: "See more places, without the slog.",
};

export const INTENSITY_LABELS: Record<IntensityBand, string> = {
  LOW: "Relaxed",
  MODERATE: "Steady",
  HIGH: "Packed",
};

export const CONFIDENCE_LABELS: Record<ConfidenceLevel, string> = {
  HIGH: "Strong recommendation",
  GOOD: "Good confidence",
  LIMITED: "Limited confidence",
};

export const FRESHNESS_LABELS: Record<PriceFreshness, string> = {
  FRESH: "Price checked just now",
  RECENT: "Price checked recently",
  STALE: "Price may have changed",
  UNKNOWN: "Estimated price",
};

export const AVAILABILITY_LABELS: Record<AvailabilityStatus, string> = {
  AVAILABLE: "Available",
  LIMITED: "Only a few left",
  SOLD_OUT: "Sold out",
  UNKNOWN: "Availability not confirmed",
};

/** A match band, so no screen ever prints "Culture = 0.84". */
export function matchBand(value: number): string {
  if (value >= 0.8) return "Excellent match";
  if (value >= 0.65) return "Strong match";
  if (value >= 0.45) return "Reasonable match";
  return "Limited match";
}

export function cityCountLabel(count: number): string {
  if (count === 1) return "1 city";
  return `${count} cities`;
}

export function joinCities(cities: string[]): string {
  if (cities.length <= 1) return cities[0] ?? "";
  return `${cities.slice(0, -1).join(", ")} + ${cities[cities.length - 1]}`;
}
