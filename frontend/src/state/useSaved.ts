import { useCallback, useEffect, useState } from "react";
import { api } from "../api/client";
import { DetouraApiError } from "../api/types";
import type {
  TripRecheckResponse,
  TripRecommendation,
} from "../api/types";

const KEY = "detoura-saved";

/**
 * Saved trips (Part 17), kept in this browser.
 *
 * The whole recommendation is stored, not just its id, so a saved trip can be
 * re-opened and re-priced. `saved_at` and `saved_price` are what make the
 * comparison meaningful: a saved trip is a snapshot, and re-checking it (V6.1)
 * is the only thing entitled to say whether it still holds.
 *
 * `travelers` is recorded at save time rather than derived from the trip,
 * because the re-check needs the party size to ask about seats and rooms, and
 * inferring it from `total_price / price_per_person` would be arithmetic
 * standing in for a fact we already have.
 */
export interface SavedTrip extends TripRecommendation {
  saved_at: string;
  saved_price: number;
  travelers: number;
}

/** The last re-check for a trip, and whether one is in flight. */
export interface RecheckState {
  status: "idle" | "checking" | "done" | "failed";
  result?: TripRecheckResponse;
  error?: string;
}

function read(): SavedTrip[] {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as SavedTrip[]) : [];
  } catch {
    // Private browsing, cleared storage, blocked site data: an empty list is
    // the correct render, not an error.
    return [];
  }
}

export function useSaved() {
  const [trips, setTrips] = useState<SavedTrip[]>(read);
  const [rechecks, setRechecks] = useState<Record<string, RecheckState>>({});

  useEffect(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify(trips));
    } catch {
      /* nothing to do: the session still works, it just will not persist */
    }
  }, [trips]);

  const toggle = useCallback((trip: TripRecommendation, travelers = 2) => {
    setTrips((current) =>
      current.some((saved) => saved.id === trip.id)
        ? current.filter((saved) => saved.id !== trip.id)
        : [
            ...current,
            {
              ...trip,
              saved_at: new Date().toISOString(),
              saved_price: trip.total_price,
              travelers,
            },
          ],
    );
  }, []);

  const recheck = useCallback(async (trip: SavedTrip) => {
    setRechecks((current) => ({ ...current, [trip.id]: { status: "checking" } }));
    try {
      const result = await api.recheck({
        trip_id: trip.id,
        travelers: trip.travelers,
        saved_price: trip.saved_price,
        saved_at: trip.saved_at,
        legs: trip.legs.map((leg) => ({
          from: leg.from,
          to: leg.to,
          departure: leg.departure,
          operator: leg.operator,
          price_per_person: leg.price_per_person,
        })),
        stays: trip.stays.map((stay) => ({
          city: stay.city,
          arrival: stay.arrival,
          departure: stay.departure,
          cost: stay.cost,
          name: stay.name,
        })),
        // Sent even though it is not re-quoted: without it the re-checked
        // total is low by exactly this much, and an unchanged trip reads as a
        // price drop. The server refuses the comparison if the parts do not
        // add up, so omitting this is caught rather than believed.
        transfers: trip.costs.ground_transfer
          ? [{ cost: trip.costs.ground_transfer }]
          : [],
      });
      setRechecks((current) => ({
        ...current,
        [trip.id]: { status: "done", result },
      }));
    } catch (error) {
      // A failed re-check says nothing about the trip, so the saved price is
      // left exactly as it was and the error is reported as ours.
      setRechecks((current) => ({
        ...current,
        [trip.id]: {
          status: "failed",
          error:
            error instanceof DetouraApiError
              ? error.message
              : "We couldn't re-check this trip.",
        },
      }));
    }
  }, []);

  return {
    trips,
    ids: trips.map((trip) => trip.id),
    toggle,
    recheck,
    rechecks,
  };
}
