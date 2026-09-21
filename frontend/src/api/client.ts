/* The single place the frontend talks to Detoura.
 *
 * Two rules this file exists to enforce:
 *
 *  1. No screen constructs a URL or reads `response.json()` itself, so the
 *     error handling below is the error handling everywhere.
 *  2. A failed request is never flattened into an empty result. The backend
 *     distinguishes "nothing matched your budget" from "the flight provider is
 *     down", and that distinction has to survive the network layer or the UI
 *     cannot honour it either.
 */

import {
  DetouraApiError,
  type BookingIntent,
  type AccountProfile,
  type BudgetSensitivityResponse,
  type CreateBookingIntentRequest,
  type ReoptimizeRequest,
  type ReoptimizeResponse,
  type DestinationSummary,
  type OriginResponse,
  type CommercialPreviewResponse,
  type GuidedMarkRequest,
  type FinancialDocument,
  type FinancialDocumentsResponse,
  type MyTripSummary,
  type MyTripsResponse,
  type Payment,
  type ProfileName,
  type SelfServiceItinerary,
  type SetCommercialOptionsRequest,
  type TripConfirmation,
  type TravelerInput,
  type TravelPass,
  type TripRecheckRequest,
  type TripRecheckResponse,
  type TripSearchRequest,
  type TripSearchResponse,
} from "./types";

/* `||`, not `??`: an unset variable and one set to the empty string both have
 * to mean "same origin". Docker's `ARG VITE_API_BASE=""` and every hosting
 * dashboard that lets you save a blank value produce the empty string, and `??`
 * would accept it as a real base - compiling every request down to `/search`
 * instead of `/api/v1/search`. That failure is invisible to a health check: the
 * server is up, the page renders, and only the searches 405. */
const BASE = import.meta.env.VITE_API_BASE || "/api/v1";
const CSRF_COOKIE = "detoura_csrf";

/* `GET /auth/google/start` is a 302 to Google, and `/callback` is Google's
 * own redirect target - both are full-page browser navigations by design
 * (Google only ever redirects a browser, never answers a fetch/XHR with
 * useful data), never something this client calls with `fetch`. The
 * backend owns the entire OAuth/OIDC exchange - client id, PKCE, state,
 * nonce, token validation, session minting - so the frontend's only job is
 * to send the browser here and later read the plain, non-secret query
 * parameters the backend's callback redirects back with (see
 * `lib/googleAuthReturn.ts`). No Google client secret, authorization code,
 * or token of any kind ever exists in frontend code. */
export function googleAuthStartUrl(): string {
  return `${BASE}/auth/google/start`;
}

interface ApiErrorBody {
  detail?: string | { message?: string; issue?: unknown };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  new Headers(init?.headers).forEach((value, key) => {
    headers[key] = value;
  });
  const method = init?.method?.toUpperCase() ?? "GET";
  if (method !== "GET") {
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }

  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      credentials: "include",
      headers,
    });
  } catch (cause) {
    // The browser could not reach us at all. This is emphatically not
    // "no trips found", and the message says so.
    throw new DetouraApiError(
      "We couldn't reach Detoura. Check your connection and try again.",
      0,
      {
        kind: "UNAVAILABLE",
        provider: "detoura",
        message: String(cause),
        retryable: true,
      },
    );
  }

  if (!response.ok) {
    let body: ApiErrorBody = {};
    try {
      body = (await response.json()) as ApiErrorBody;
    } catch {
      /* a non-JSON error body is still an error; fall through to the status */
    }
    const detail = body.detail;
    const message =
      typeof detail === "string"
        ? detail
        : (detail?.message ?? `Request failed (${response.status})`);
    const issue =
      detail && typeof detail === "object" && "issue" in detail
        ? (detail.issue as DetouraApiError["issue"])
        : undefined;
    throw new DetouraApiError(message, response.status, issue);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

async function requestBlob(path: string, init?: RequestInit): Promise<Blob> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    credentials: "include",
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const body = (await response.json()) as ApiErrorBody;
      const detail = body.detail;
      message =
        typeof detail === "string" ? detail : (detail?.message ?? message);
    } catch {
      /* Binary endpoints may not return JSON errors. */
    }
    throw new DetouraApiError(message, response.status);
  }
  return response.blob();
}

function readCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`;
  const cookie = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix));
  if (!cookie) return null;
  return decodeURIComponent(cookie.slice(prefix.length));
}

export const api = {
  me(signal?: AbortSignal) {
    return request<AccountProfile | null>("/auth/me", { signal });
  },

  login(body: { email: string; password: string }) {
    return request<{ user_id: string }>("/auth/login", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  register(body: { email: string; password: string }) {
    return request<{ user_id: string }>("/auth/register", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  logout() {
    return request<{ ok: true }>("/auth/logout", { method: "POST" });
  },

  /* Google Sign-In: `/auth/google/link/confirm` is the only Google auth
   * call the frontend makes through `fetch` - see `googleAuthStartUrl()`
   * below for why the other two (`start`/`callback`) are full-page
   * navigations instead. Backend contract: `docs/V9_GOOGLE_AUTH_ACCOUNT_LIFECYCLE_REPORT.md`. */
  googleLinkConfirm(body: { link_id: string }) {
    return request<{ ok: true }>("/auth/google/link/confirm", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  search(body: TripSearchRequest, signal?: AbortSignal) {
    return request<TripSearchResponse>("/search", {
      method: "POST",
      body: JSON.stringify(body),
      signal,
    });
  },

  /** The same search at DEEP. Separate method so the UI reads intentionally. */
  searchDeeper(body: TripSearchRequest, signal?: AbortSignal) {
    return request<TripSearchResponse>("/search", {
      method: "POST",
      body: JSON.stringify({ ...body, search_mode: "DEEP" }),
      signal,
    });
  },

  /** Re-price a saved trip. The trip travels with the request because saved
   *  trips live in this browser and the server stores nothing. */
  reoptimize(body: ReoptimizeRequest) {
    return request<ReoptimizeResponse>("/trips/reoptimize", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  recheck(body: TripRecheckRequest, signal?: AbortSignal) {
    return request<TripRecheckResponse>("/trips/recheck", {
      method: "POST",
      body: JSON.stringify(body),
      signal,
    });
  },

  budgetSensitivity(body: TripSearchRequest, steps = 6) {
    return request<BudgetSensitivityResponse>(
      `/budget-sensitivity?steps=${steps}`,
      { method: "POST", body: JSON.stringify(body) },
    );
  },

  origins(query: string) {
    return request<OriginResponse>(`/origins/${encodeURIComponent(query)}`);
  },

  destinations() {
    return request<DestinationSummary[]>("/destinations");
  },

  profiles() {
    return request<{ name: ProfileName; label: string; description: string }[]>(
      "/profiles",
    );
  },

  /* --- Booking flow (V8 Phase 4) --------------------------------------- *
   * The server owns booking truth. These methods send the traveller's
   * intent and poll the server for what actually happened. */

  createBookingIntent(body: CreateBookingIntentRequest) {
    return request<BookingIntent>("/booking-intents", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  submitTravelers(bookingId: string, travelers: TravelerInput[]) {
    return request<BookingIntent>(
      `/booking-intents/${encodeURIComponent(bookingId)}/travelers`,
      { method: "POST", body: JSON.stringify({ travelers }) },
    );
  },

  confirmBooking(
    bookingId: string,
    tolerance: { tolerance_absolute?: number; tolerance_percentage?: number } = {},
  ) {
    return request<BookingIntent>(
      `/booking-intents/${encodeURIComponent(bookingId)}/confirm`,
      { method: "POST", body: JSON.stringify(tolerance) },
    );
  },

  setCommercialOptions(bookingId: string, body: SetCommercialOptionsRequest) {
    return request<BookingIntent>(
      `/booking-intents/${encodeURIComponent(bookingId)}/commercial`,
      { method: "POST", body: JSON.stringify(body) },
    );
  },

  commercialPreview(body: {
    supplier_total: number;
    currency?: string;
    ticket_count?: number;
    promo_code?: string | null;
  }) {
    return request<CommercialPreviewResponse>("/commercial/preview", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  getItinerary(bookingId: string) {
    return request<SelfServiceItinerary>(
      `/booking-intents/${encodeURIComponent(bookingId)}/itinerary`,
    );
  },

  startTicketBooking(bookingId: string, sequence: number) {
    return request<SelfServiceItinerary>(
      `/booking-intents/${encodeURIComponent(bookingId)}/tickets/${sequence}/start-booking`,
      { method: "POST" },
    );
  },

  markTicket(bookingId: string, sequence: number, body: GuidedMarkRequest) {
    return request<SelfServiceItinerary>(
      `/booking-intents/${encodeURIComponent(bookingId)}/tickets/${sequence}/mark`,
      { method: "POST", body: JSON.stringify(body) },
    );
  },

  getBookingIntent(bookingId: string) {
    return request<BookingIntent>(
      `/booking-intents/${encodeURIComponent(bookingId)}`,
    );
  },

  getTravelPass(bookingId: string) {
    return request<TravelPass>(
      `/booking-intents/${encodeURIComponent(bookingId)}/travel-pass`,
    );
  },

  createPayment(body: { booking_id: string; idempotency_key: string }) {
    return request<Payment>("/payments", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  confirmPayment(paymentId: string) {
    return request<Payment>(`/payments/${encodeURIComponent(paymentId)}/confirm`, {
      method: "POST",
    });
  },

  getPayment(paymentId: string) {
    return request<Payment>(`/payments/${encodeURIComponent(paymentId)}`);
  },

  listMyTrips(signal?: AbortSignal) {
    return request<MyTripsResponse>("/me/trips", { signal });
  },

  getMyTrip(bookingId: string, signal?: AbortSignal) {
    return request<MyTripSummary>(
      `/me/trips/${encodeURIComponent(bookingId)}`,
      { signal },
    );
  },

  getTripConfirmation(bookingId: string, signal?: AbortSignal) {
    return request<TripConfirmation>(
      `/me/trips/${encodeURIComponent(bookingId)}/confirmation`,
      { signal },
    );
  },

  listTripDocuments(bookingId: string, signal?: AbortSignal) {
    return request<FinancialDocumentsResponse>(
      `/me/trips/${encodeURIComponent(bookingId)}/documents`,
      { signal },
    );
  },

  getTripDocument(bookingId: string, documentId: string, signal?: AbortSignal) {
    return request<FinancialDocument>(
      `/me/trips/${encodeURIComponent(bookingId)}/documents/${encodeURIComponent(documentId)}`,
      { signal },
    );
  },

  downloadTripDocument(bookingId: string, documentId: string, signal?: AbortSignal) {
    return requestBlob(
      `/me/trips/${encodeURIComponent(bookingId)}/documents/${encodeURIComponent(documentId)}/download`,
      { signal },
    );
  },
};
