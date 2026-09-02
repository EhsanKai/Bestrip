import { useCallback, useEffect, useState } from "react";
import type { TripRecommendation } from "../api/types";

const KEY = "detoura-saved";

/**
 * Saved trips (Part 17), kept in this browser.
 *
 * The whole recommendation is stored, not just its id, so a saved trip can be
 * re-opened and — once price re-checking exists — compared against a fresh
 * search. `saved_at` is what makes that comparison meaningful later.
 */
export interface SavedTrip extends TripRecommendation {
  saved_at: string;
  saved_price: number;
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

  useEffect(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify(trips));
    } catch {
      /* nothing to do: the session still works, it just will not persist */
    }
  }, [trips]);

  const toggle = useCallback((trip: TripRecommendation) => {
    setTrips((current) =>
      current.some((saved) => saved.id === trip.id)
        ? current.filter((saved) => saved.id !== trip.id)
        : [
            ...current,
            {
              ...trip,
              saved_at: new Date().toISOString(),
              saved_price: trip.total_price,
            },
          ],
    );
  }, []);

  return { trips, ids: trips.map((trip) => trip.id), toggle };
}
