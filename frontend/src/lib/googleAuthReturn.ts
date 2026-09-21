/* Reads the plain, non-secret query parameters the backend's
 * `GET /api/v1/auth/google/callback` redirects the browser back with (see
 * `src/detoura/api/auth_google.py::_redirect_with` and
 * `docs/V9_GOOGLE_AUTH_ACCOUNT_LIFECYCLE_REPORT.md` §4 "Frontend contract
 * required"). Nothing here ever handles a token, code, or secret - those
 * stay entirely server-side; this module only classifies which of the
 * backend's own documented outcomes just happened.
 *
 * `GOOGLE_POST_LOGIN_REDIRECT_URL` is a single, fixed, operator-configured
 * URL (default `/`) - the backend has no per-attempt "return to X" concept,
 * so this module deliberately has none either (V9 Google Sign-In consumer
 * UI integration §8: "if backend does not support safe return-to
 * semantics, do not invent a client-side redirect protocol").
 */

export type GoogleReturnOutcome =
  | { kind: "success" }
  | { kind: "link_required"; linkId: string }
  | { kind: "error"; reason: "cancelled" | "failed" | "link_conflict" | "unknown" };

/** Matches `persistence/accounts.py::new_link_id()` - `"glink_" +
 * secrets.token_urlsafe(16)` - url-safe base64 plus the fixed prefix.
 * Anything else is treated as malformed rather than passed on trust. */
const LINK_ID_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

export function readGoogleReturnOutcome(search: string): GoogleReturnOutcome | null {
  const params = new URLSearchParams(search);

  if (params.get("google_link_required") === "1") {
    const linkId = params.get("link_id") ?? "";
    return LINK_ID_PATTERN.test(linkId)
      ? { kind: "link_required", linkId }
      : { kind: "error", reason: "unknown" };
  }

  const outcome = params.get("google_auth");
  if (outcome === "success") return { kind: "success" };
  if (outcome === "error") {
    const reason = params.get("reason");
    // Exactly the three reasons `auth_google.py::google_callback` emits.
    if (reason === "missing_parameters") return { kind: "error", reason: "cancelled" };
    if (reason === "failed") return { kind: "error", reason: "failed" };
    if (reason === "link_conflict") return { kind: "error", reason: "link_conflict" };
    return { kind: "error", reason: "unknown" };
  }
  return null;
}

/** Strips the Google-return parameters from the address bar without a
 * reload, so refreshing the page never repeats the notice and a shared/
 * bookmarked URL never carries a one-time link id. */
export function clearGoogleReturnParams(): void {
  if (typeof window === "undefined") return;
  try {
    const url = new URL(window.location.href);
    let changed = false;
    for (const key of ["google_auth", "google_link_required", "link_id", "reason"]) {
      if (url.searchParams.has(key)) {
        url.searchParams.delete(key);
        changed = true;
      }
    }
    if (changed) window.history.replaceState(null, "", `${url.pathname}${url.search}${url.hash}`);
  } catch {
    /* Never let URL bookkeeping break the app. */
  }
}
