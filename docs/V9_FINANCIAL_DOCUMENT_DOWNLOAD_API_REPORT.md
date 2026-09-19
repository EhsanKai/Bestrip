# V9 — Financial Document Download API Slice

**Starting HEAD**: `07d1b1e57ca738d3ef9e48025224911a7b586d59`
**Final HEAD**: this slice's checkpoint commit (see bottom)

Backend-only, document-delivery-only, per this slice's scope instructions.
No frontend file was read as work to fix, modified, or staged. A parallel
Codex session was concurrently editing `frontend/**` and (unexpectedly)
a handful of unrelated backend files (`api/assembler.py`, `api/contracts.py`,
`api/v1.py`, `services/live_search.py`) during this session; none of those
were inspected, touched, or included in this slice's commit.

## 1. The gap this slice closes

The V9 Limited Beta Reality Audit (`docs/V9_LIMITED_BETA_REALITY_AUDIT.md`,
§11/§24) and the Phase 5 Final Report both independently confirmed: financial
document **generation, persistence, immutability, and ownership** are REAL &
CONNECTED, but **PDF bytes were never served by any route** — an explicit
stale comment in the code itself said so:

```
# Return document metadata. PDF byte serving can be added here once the
# financial_documents module provides get_document_bytes or similar.
# Agent 6: wire actual PDF serving once module is merged.
```

This was the "already partial download endpoint" the task asked to be
completed rather than duplicated (§3.J / §4). No new architecture was
created — the existing `api/me_trips.py` router, the existing
`persistence/financial_documents.py` ownership-scoped reads, and the
existing `FinancialDocument` domain model are all unchanged except for one
new route.

## 2. Existing document architecture (as found, verified against code)

- **Document types**: `RECEIPT`, `INVOICE`, `CREDIT_NOTE`
  (`models/financial_document.py::FinancialDocumentType`) — exactly these
  three, nothing else.
- **Identity**: `document_id` (opaque `findoc_<token>`, internal) and
  `document_number` (human-facing, server-allocated, e.g. `RCPT-2026-000123`
  — see `persistence/financial_documents.py::allocate_document_number`).
- **PDF storage**: a `pdf_blob` BLOB column on the `financial_documents`
  table, committed in the same transaction as the row
  (`persistence/financial_documents.py`, schema in `persistence/db.py`).
  Never a filesystem path.
- **Regeneration**: never. A document is rendered once, at issuance
  (`services/financial_document_service.py::_issue`), and the stored bytes
  are the only bytes ever served. There is no `update_document` anywhere.
- **Ownership**: `FinancialDocument.user_id` is captured at issuance from
  the *paying* account (`_payment_facts` in
  `financial_document_service.py`), not re-derived from booking ownership.
  Ownership-scoped reads (`get_document_for_user`,
  `get_document_pdf_for_user`) return `None` uniformly for "does not exist"
  and "exists but not yours" (anti-enumeration, matching the existing
  `payments.get_payment_for_user` pattern).
- **Anonymous bookings**: a booking with no `trip_ownership` row has
  `get_trip_owner() -> None`. No secure anonymous-retrieval mechanism exists
  anywhere in the codebase, so none was invented — anonymous documents
  remain reachable only by whoever holds Ops access, not by any consumer
  route. This is a documented, deliberate gap, not an oversight.
- **Ops access**: separate, pre-existing, and untouched
  (`api/ops_confirmations.py` lists documents; it already had its own
  access path and was out of this slice's scope).
- **Supplier invoice vs. customer document**: no supplier/provider invoice
  is ever represented in `financial_documents` — `_itinerary()` in
  `financial_document_service.py` is the enforced privacy boundary that
  strips `provider_order_id`/`offer_id`/`provider` before anything reaches
  the document or its renderer.
- **Existing partial endpoint completed**: `api/me_trips.py` already had
  `GET /api/v1/me/trips/{booking_id}/documents` (list) and
  `GET /api/v1/me/trips/{booking_id}/documents/{document_id}` (metadata
  only) wired to real ownership checks. Only the PDF-byte-serving half was
  missing.

## 3. What was implemented

One new route, in the existing router (`src/detoura/api/me_trips.py`):

```
GET /api/v1/me/trips/{booking_id}/documents/{document_id}/download
```

- Authenticated-owner-only (`Depends(require_session)`), same as every
  other route in this file. No CSRF check — this is a read, matching the
  file's existing convention (only the mutating `resend` route requires it).
- **Two-level ownership check**, identical to and copy-verified against the
  pre-existing metadata route: (1) `booking_id` must be owned by the
  session's account (`accounts.get_trip_owner`); (2) the resolved document
  must itself belong to that exact `booking_id`
  (`doc.booking_id != booking_id` → 404). Both checks run before the PDF
  blob is ever read.
- Reads bytes via `financial_documents.get_document_pdf_for_user` — an
  ownership-scoped, pure-`SELECT` read; no write path is reachable from this
  route.
- Response: `media_type="application/pdf"`,
  `Content-Disposition: attachment; filename="detoura-<type>-<number>.pdf"`,
  `Cache-Control: no-cache, no-store, must-revalidate` (the exact string
  already used for other private content in `api/static.py`).
- **Filename sanitization**: a new `_safe_filename_component()` helper
  strips every character outside `[A-Za-z0-9._-]` before it can reach the
  `Content-Disposition` header — defensive-in-depth even though
  `document_number`/`document_type` are always server-generated, never
  client input.
- **Safe error handling**: missing document, wrong owner, cross-booking
  substitution, and a document whose `pdf_blob` is unexpectedly `NULL`
  (corrupt/incomplete row) all return a static `404` with a fixed message —
  never a raw exception, SQL text, or filesystem path.
- **Metadata extended** (`_document_dto`, used by both the list and
  single-document routes): added `download_available` and `download_url`
  fields so a Slice C frontend can render a working download link without
  knowing the URL shape or internal storage — the "smallest coherent
  consumer API" the task asked for. No internal field (blob, storage path,
  provider ids, idempotency key) was added; the DTO already excluded them
  and continues to.

No document-generation logic, no PDF rendering logic, no persistence schema,
and no payment/booking architecture was touched.

## 4. Ownership / IDOR

Proven with real HTTP requests through a real session cookie
(`tests/test_v9_financial_document_download_api.py`):

- Booking A's owner cannot download Booking B's document, even guessing a
  real document id.
- A user's own document from booking X is refused under booking Y's URL
  (document-to-booking substitution), even when the same user owns both.
- Combined booking+document substitution across two different users' real
  bookings is refused in every wrong combination and only succeeds for the
  one legitimate pairing.
- A spoofed `X-User-Id`/`X-Traveler-Email` header has no effect — the route
  takes no `Request` parameter at all; identity comes exclusively from
  `SessionContext.user_id`, resolved server-side from the session cookie.

An independent, tool-restricted read-only reviewer (an agent type with no
Edit/Write tools available, specifically to prevent a repeat of an earlier,
unrelated incident on this project where a "read-only" reviewer used edit
tools it was told not to use) attacked all of: cross-user IDOR,
booking/document substitution, anonymous global access, traveler-email/
client-identity authorization, filename/header injection and path
traversal, supplier/internal data exposure, document mutation, payment-state
misrepresentation, document enumeration, error/stack-trace leakage, and
cache/privacy headers. **Verdict: APPROVED**, zero Critical/High/Medium
findings, with one cosmetic-only observation (see §9).

## 5. Anonymous booking semantics

Unchanged, and confirmed unchanged: a booking with no claimed owner
(`trip_ownership` row absent) is refused for every caller, authenticated or
not — `owner is None` fails the same ownership check as `owner != current
user`. No new anonymous-access path was added. This is the honest contract
gap the task instructions anticipated (§8): authenticated-owner retrieval is
complete; anonymous retrieval remains not built, by design, because no
secure anonymous-session mechanism exists elsewhere in the codebase to hang
it off.

## 6. Immutability / refund / credit-note behavior

Tested directly: issuing a credit note against a receipt leaves the
receipt's downloaded bytes byte-for-byte identical to before the credit note
existed (`test_historical_document_bytes_are_stable_after_a_credit_note_is_issued`).
A credit note is independently listed and downloadable, distinct from the
receipt it adjusts. No code path in the new route writes to
`financial_documents` — verified structurally (only `SELECT`-backed
persistence functions are called) and by the independent reviewer.

## 7. Privacy / cache behavior

`Cache-Control: no-cache, no-store, must-revalidate` is set on every
download response, reusing the repository's own existing convention for
private content (`api/static.py::_NO_STORE`) rather than inventing a new
one. No public/static URL was introduced; the route is session-gated on
every request.

## 8. Error semantics

| Case | Response |
|---|---|
| Booking not found / not owned | `404 {"message": "No such trip."}` |
| Document not found / not owned / wrong booking | `404 {"message": "No such document."}` |
| Document exists, owned, but no PDF stored (corrupt row) | `404 {"message": "No document artifact available."}` |
| No session | `401` (existing `require_session` behavior, unchanged) |

No case leaks a stack trace, SQL text, or filesystem path (there is no
filesystem path anywhere in this design — bytes come from a DB column).

## 9. Independent review — non-blocking observations

- `download_available: True` in the document DTO is a hardcoded literal
  rather than a per-row check of whether `pdf_blob` is `NULL`. In practice
  every document issued through `financial_document_service` always has a
  stored PDF (enforced by that module's own design), so this is accurate by
  invariant rather than by a defensive per-row query; the download route
  itself independently re-checks and 404s safely on the corrupt-row case
  regardless. Not changed, per the reviewer's own "non-blocking" framing —
  changing it would require reading the blob column on every list call,
  which is the exact cost the persistence layer's own design deliberately
  avoids for listing.
- If a booking's `trip_ownership` owner and the account that actually paid
  for it ever diverge (a pre-existing, out-of-scope edge case around
  claiming an anonymous booking after the fact), `list_documents` could
  advertise a `download_url` that then 404s for that same session. This is
  inherited unchanged from the pre-existing metadata route's identical
  ownership check, not introduced by this slice, and fails closed (a 404,
  never a leak) rather than fails open.

## 10. Tests

New file: `tests/test_v9_financial_document_download_api.py` — 20 tests,
all real HTTP requests through `fastapi.testclient.TestClient` with real
session cookies (register/login), covering:

- Listing and downloading an owned document; correct `application/pdf`
  content type; PDF bytes matching the stored blob exactly.
- Safe, non-injectable `Content-Disposition` (no CR/LF, no raw internal
  `document_id` in the filename); `Cache-Control: no-store`.
- Cross-user IDOR, document-id substitution across a user's own bookings,
  booking-id substitution against another user's real booking, and combined
  booking+document substitution (all four combinations).
- Unauthenticated request → 401 (matches existing security model);
  anonymous (unclaimed) booking documents are not globally downloadable by
  an authenticated stranger; spoofed identity headers have no effect.
- Historical document byte-stability across a credit note being issued;
  credit note separately retrievable and distinct from the original.
- Missing document and corrupt/`NULL`-blob artifact both fail safely
  (`404`, no stack trace).
- Download performs no mutation of booking phase or payment status, across
  repeated and concurrent (20-thread) downloads; all concurrent downloads
  return byte-identical content matching the stored blob.
- No supplier/internal field (`pdf_blob`, `provider_order_id`, `offer_id`,
  `provider`, `idempotency_key`, storage path) ever appears in the JSON
  metadata response.
- `_safe_filename_component` unit-tested directly against a CRLF/
  path-traversal payload.

**Targeted run** (this file + every Phase 5/6 financial-document, booking-
ownership, and payment-security test file — `test_v9_phase5_me_trips_api.py`,
`test_v9_phase5_financial_documents.py`, `test_v9_phase5_integration.py`,
`test_v9_phase6_ownership_wiring.py`, `test_v9_phase6_booking_security.py`,
`test_v9_phase6_pii_security.py`, `test_v9_payment_booking_coupling.py`):
**green, 0 failures.**

**Full regression** (`pytest tests/`, 2,429 tests collected across the
whole suite): **green, exit code 0, no failures** (only pre-existing,
environmental skips — Redis-dependent and live-network-gated tests, unrelated
to this slice).

## 11. Independent security review

A fresh, tool-restricted read-only reviewer (no Edit/Write tool access at
all, by deliberate choice — see §4) inspected the new route, the ownership
persistence functions it calls, the document model, and the auth session
model, and attempted all eleven required attack classes.

**Verdict: APPROVED.** Zero Critical/High/Medium findings. Two Low/cosmetic
observations, both addressed in §9 above (neither is a disclosure or
access-control gap).

## 12. Remaining gaps for frontend Slice C

- The frontend has no code calling this route yet (correctly out of this
  slice's scope) — Slice C can now hit
  `GET /api/v1/me/trips/{booking_id}/documents` for a list with a ready-made
  `download_url` per document, and `GET <download_url>` to stream the PDF.
- Anonymous-booking document retrieval remains an open product gap, not a
  security gap: a guest who never creates/claims an account has no way to
  retrieve their own receipt today. Closing it would require a secure
  anonymous-session or claim-token mechanism that does not exist elsewhere
  in the codebase yet — out of this slice's scope per the task's own
  instruction not to invent one.
- Ops-side document download (as opposed to the existing Ops document
  *listing*) was not built — out of scope; Ops recovery flows were not
  touched.

## 13. Git safety

Only `src/detoura/api/me_trips.py`, `tests/test_v9_financial_document_download_api.py`,
and this report were staged and committed. `frontend/**`, `AGENTS.md`,
`CLAUDE.md`, and the concurrent session's unrelated backend edits
(`api/assembler.py`, `api/contracts.py`, `api/v1.py`,
`services/live_search.py`, `tests/test_v9_search_selection_booking_contract.py`)
were left exactly as found — not staged, not inspected as work to fix, not
reverted, not stashed. Verified via `git diff --cached --name-status` before
committing.
