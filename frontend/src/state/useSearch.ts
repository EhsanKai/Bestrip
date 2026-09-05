import { useCallback, useRef, useState } from "react";
import { api } from "../api/client";
import {
  DetouraApiError,
  type SearchMode,
  type TripSearchRequest,
  type TripSearchResponse,
} from "../api/types";
import { track } from "../lib/analytics";
import { captureException } from "../lib/errorTracking";

/* DEEP's own estimate is 8-20s, with a 25s hard ceiling on the adaptive beam
 * (search_modes.py's `time_budget_seconds`). 30s sits above that ceiling with
 * headroom, so a normal DEEP search - the slowest mode there is - can never
 * trip this. Anything past it is genuinely unusual, for any mode. */
const SLOW_SEARCH_MS = 30_000;

/**
 * All search state in one place, so no component owns business logic.
 *
 * The important design point is `failure`. A search can end in three
 * distinguishable ways and this hook keeps them apart, because the whole V5.1.1
 * contract collapses if the UI treats them the same:
 *
 *   response.recommendations.length > 0   → results
 *   response.no_results                   → nothing matched; offer relaxations
 *   failure                               → the search did not complete
 *
 * `response.issues` is a fourth, orthogonal thing: results *and* a degraded
 * provider, which shows a banner over real results rather than an error page.
 */
export type SearchStatus = "idle" | "searching" | "done" | "failed";

export interface SearchState {
  status: SearchStatus;
  request: TripSearchRequest | null;
  response: TripSearchResponse | null;
  failure: DetouraApiError | null;
  deeperPending: boolean;
  /** True once the in-flight search has run past `SLOW_SEARCH_MS`. Distinct
   *  from `failure`: nothing has gone wrong yet, so the UI this drives must
   *  never look like an error state. */
  slow: boolean;
}

const INITIAL: SearchState = {
  status: "idle",
  request: null,
  response: null,
  failure: null,
  deeperPending: false,
  slow: false,
};

export function useSearch() {
  const [state, setState] = useState<SearchState>(INITIAL);
  const abort = useRef<AbortController | null>(null);
  const slowTimer = useRef<number | null>(null);

  const clearSlowTimer = () => {
    if (slowTimer.current !== null) {
      window.clearTimeout(slowTimer.current);
      slowTimer.current = null;
    }
  };

  const run = useCallback(async (request: TripSearchRequest) => {
    abort.current?.abort();
    clearSlowTimer();
    const controller = new AbortController();
    abort.current = controller;

    setState({
      status: "searching",
      request,
      response: null,
      failure: null,
      deeperPending: false,
      slow: false,
    });
    track("search_started", { search_mode: request.search_mode ?? "SMART" });

    slowTimer.current = window.setTimeout(() => {
      // Only flip the flag if this is still the search we started - a fresh
      // `run()` already cleared this timer, but a stale callback racing the
      // event loop should not resurrect it.
      if (abort.current === controller) {
        setState((prev) => (prev.status === "searching" ? { ...prev, slow: true } : prev));
      }
    }, SLOW_SEARCH_MS);

    try {
      const response = await api.search(request, controller.signal);
      clearSlowTimer();
      setState({
        status: "done",
        request,
        response,
        failure: null,
        deeperPending: false,
        slow: false,
      });
      track("search_completed", {
        search_mode: request.search_mode ?? "SMART",
        result_count: response.recommendations.length,
        no_results: response.no_results !== null,
      });
      return response;
    } catch (error) {
      clearSlowTimer();
      if (controller.signal.aborted) return null;
      const failure =
        error instanceof DetouraApiError
          ? error
          : new DetouraApiError(String(error), 0);
      setState({
        status: "failed",
        request,
        response: null,
        failure,
        deeperPending: false,
        slow: false,
      });
      track("search_failed", {
        search_mode: request.search_mode ?? "SMART",
        status: failure.status,
        issue_kind: failure.issue?.kind,
      });
      captureException(failure, { request });
      return null;
    }
  }, []);

  /** Dismisses the slow-search notice without touching the request itself -
   *  the search keeps running, the user just no longer wants to be told. */
  const dismissSlow = useCallback(() => {
    setState((prev) => (prev.slow ? { ...prev, slow: false } : prev));
  }, []);

  /**
   * "Search deeper" (Part 15). Deliberately keeps the existing results on
   * screen while it runs: the traveler already has answers, and replacing them
   * with a spinner would make an optional extra feel like a restart.
   */
  const searchDeeper = useCallback(async () => {
    const request = state.request;
    if (!request) return null;
    setState((prev) => ({ ...prev, deeperPending: true }));
    track("search_started", { search_mode: "DEEP", deeper: true });
    try {
      const response = await api.searchDeeper(request);
      setState({
        status: "done",
        request: { ...request, search_mode: "DEEP" as SearchMode },
        response,
        failure: null,
        deeperPending: false,
        slow: false,
      });
      track("search_completed", {
        search_mode: "DEEP",
        deeper: true,
        result_count: response.recommendations.length,
        no_results: response.no_results !== null,
      });
      return response;
    } catch (error) {
      // A failed deepening must not destroy the results we already have.
      setState((prev) => ({ ...prev, deeperPending: false }));
      track("search_failed", { search_mode: "DEEP", deeper: true });
      captureException(error, { request, deeper: true });
      throw error;
    }
  }, [state.request]);

  const reset = useCallback(() => {
    abort.current?.abort();
    clearSlowTimer();
    setState(INITIAL);
  }, []);

  return { ...state, run, searchDeeper, reset, dismissSlow };
}
