import { useCallback, useRef, useState } from "react";
import { api } from "../api/client";
import {
  DetouraApiError,
  type SearchMode,
  type TripSearchRequest,
  type TripSearchResponse,
} from "../api/types";

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
}

const INITIAL: SearchState = {
  status: "idle",
  request: null,
  response: null,
  failure: null,
  deeperPending: false,
};

export function useSearch() {
  const [state, setState] = useState<SearchState>(INITIAL);
  const abort = useRef<AbortController | null>(null);

  const run = useCallback(async (request: TripSearchRequest) => {
    abort.current?.abort();
    const controller = new AbortController();
    abort.current = controller;

    setState({
      status: "searching",
      request,
      response: null,
      failure: null,
      deeperPending: false,
    });

    try {
      const response = await api.search(request, controller.signal);
      setState({
        status: "done",
        request,
        response,
        failure: null,
        deeperPending: false,
      });
      return response;
    } catch (error) {
      if (controller.signal.aborted) return null;
      setState({
        status: "failed",
        request,
        response: null,
        failure:
          error instanceof DetouraApiError
            ? error
            : new DetouraApiError(String(error), 0),
        deeperPending: false,
      });
      return null;
    }
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
    try {
      const response = await api.searchDeeper(request);
      setState({
        status: "done",
        request: { ...request, search_mode: "DEEP" as SearchMode },
        response,
        failure: null,
        deeperPending: false,
      });
      return response;
    } catch (error) {
      // A failed deepening must not destroy the results we already have.
      setState((prev) => ({ ...prev, deeperPending: false }));
      throw error;
    }
  }, [state.request]);

  const reset = useCallback(() => {
    abort.current?.abort();
    setState(INITIAL);
  }, []);

  return { ...state, run, searchDeeper, reset };
}
