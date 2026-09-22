# V9 Limited Beta — Manual Data Subject Request (DSAR) Operational Runbook

This is **not** an automated privacy portal and does not build one. It is a
precise procedure an authorized operator follows, against a **verified**
account or data subject, using the existing (non-public, ownership-scoped)
data accessors already in this codebase. It exists so a manual GDPR
data-subject-access-request process is operationally possible for Limited
Beta, per the policy lock (`docs/V9_LIMITED_BETA_PRIVACY_POLICY_IMPLEMENTATION_REPORT.md`
§0).

**Before running any step below**: verify the requester's identity and
authority over the account through your organization's existing identity
verification process (out of scope for this document — this runbook assumes
that verification has already happened and you are holding a confirmed
`user_id` or account email). Never run these steps against an unverified
request.

**Do not** build a new endpoint or automated export for this. Every step
below is a direct, read-only invocation of an accessor that already exists
in `src/detoura/persistence/*.py`, run by an operator with backend/database
access (e.g., a Python REPL against the production database, or an
equivalent internal ops tool) — not by a consumer-facing API call.

---

## Step 0 — Resolve the account

```python
from detoura.persistence import accounts as store, get_db
db = get_db()
user = store.get_user_by_email(db, "<verified-normalized-email>")
user_id = user["user_id"]
```

If the account was already deleted, `store.get_user(db, user_id)` still
returns the tombstoned row (`status == "DELETED"`) — this is expected and is
itself part of the answer (see Source 1 below): the account's sign-in
identity was anonymized, but its transaction history (Sources 4–9) is not
deleted by account deletion and remains subject to this same runbook.

## Step 1 — Walk every data source for this `user_id`

| # | Source | Accessor | Classification | Notes |
|---|---|---|---|---|
| 1 | Account | `store.get_user(db, user_id)` | **AUTOMATED EXPORT** (already surfaced by `GET /api/v1/auth/account/export`) | Exclude `password_hash` even though it is only a hash — not meaningful to a data subject and not required for a DSAR. |
| 2 | Auth identities | `store.list_identities_for_user(db, user_id)` | **AUTOMATED EXPORT** | `provider_subject` is deliberately excluded from the consumer export (integration detail, not the user's own data) — for a full DSAR, include it; it identifies the linked Google account. |
| 3 | Active/revoked sessions | `store.get_session_by_token_hash` is lookup-by-token only; there is **no `list_sessions_for_user`** accessor today | **NOT PERSONAL DATA WORTH EXPORTING** in the DSAR sense — session rows are pseudonymous token hashes with no reversible identity value; their *existence/count* is operationally knowable via a direct read of `auth_sessions WHERE user_id=?` if a request specifically asks "what devices/sessions exist," but the token hash itself is not disclosable (it is a security credential, not the subject's data) | If ever needed: direct SQL read, never disclose the hash itself. |
| 4 | Owned bookings | `store.list_trip_ids_for_user(db, user_id)` → list of `booking_id` | **AUTOMATED EXPORT** (booking IDs already in the consumer export) | Opaque IDs only from this call — see Source 5 for the booking content itself. |
| 5 | Booking detail | For each `booking_id` from Source 4: `bookings.get(db, booking_id)` (module: `persistence/bookings.py`) | **MANUAL EXPORT** (accessor exists, ownership-checked by cross-referencing against Source 4's list; not wired into the automated export endpoint) | Contains `lead_name`/`lead_email`, route, party size, pricing — this is the operational booking record. |
| 6 | Payments | For each `booking_id`: `payments.list_payments_for_booking(db, booking_id)` (module: `persistence/payments.py`) | **MANUAL EXPORT** | Financial ledger rows: amounts, status, Stripe object IDs (never card data — Detoura never receives or stores it). |
| 7 | Payment events / refunds | `payments.list_events(db, payment_id)`, `payments.list_refunds_for_payment(db, payment_id)` for each payment from Source 6 | **MANUAL EXPORT** | Append-only ledger; refund history. |
| 8 | Checkout snapshots | Query `checkout_snapshots WHERE booking_id=?` for each booking from Source 4 (no dedicated accessor exists yet — direct read) | **MANUAL EXPORT** | Frozen quote/pricing detail at the moment payment began — see the CheckoutSnapshot field classification in the Implementation Report §6 for what's transaction evidence vs. transient. |
| 9 | Financial documents | `financial_documents.list_documents_for_booking(db, booking_id)` for each booking from Source 4 | **MANUAL EXPORT** | Receipts/invoices/credit notes, including the PDF (`get_document_pdf`). Redact nothing — these are the subject's own billing records. |
| 10 | Communications | `communications.list_communications_for_booking(db, booking_id)` for each booking from Source 4 | **MANUAL EXPORT** | `recipient_address` here is the actual delivery address at time of send. **If the account has since been deleted, this will already read as the scrubbed tombstone value** (`deleted-<user_id>@deleted.invalid`) per the Communication Deletion Medium implemented in this slice — that is correct and expected, not a bug to work around. |
| 11 | Communication events/attempts | `communications.list_attempts_for_communication` / `list_events` for each communication from Source 10 | **MANUAL EXPORT** (delivery status/ledger only — no PII in `communication_events.data` by construction) | |
| 12 | Tickets / provider order references | `booking_items` rows for each booking (via `bookings.get(db, booking_id).items`) | **MANUAL EXPORT** | IDs/prices/states only — no traveler PII (see Source 13). |
| 13 | Traveler party (name/DOB/passport/etc.) | **NOT PERSISTED** — no `travelers` table exists in the schema (`models/traveler.py` data lives only in-memory on `BookingRun.party` for the duration of a live booking run) | **NOT PERSONAL DATA [PERSISTED]** | There is nothing to export here for any booking that has already completed — confirm this against the live code at the time of the request (re-verify traveler non-persistence per the Implementation Report §7 before answering "we hold none of this," since it is a factual claim about the current codebase, not a permanent guarantee). |
| 14 | Analytics/events | `analytics_events` keyed by session/visitor key, not `user_id` — there is **no linkage from `user_id` to a visitor/session key** anywhere in the schema | **NOT PERSONAL DATA (NO LINKAGE EXISTS)** | Analytics identifiers are anonymous-by-construction and cannot be joined to an account by any accessor in this codebase. If a request specifically supplies a visitor/session key, that is a separate, narrower request outside this account-based runbook. |
| 15 | Support/feedback data | No support-ticket or feedback-storage table exists in this backend's schema | **NOT PERSONAL DATA [PERSISTED IN THIS SYSTEM]** | If Detoura's support workflow uses a third-party helpdesk, that is `PROVIDER-HELD` and outside this runbook — confirm whether one is in use before answering definitively. |
| 16 | Provider-held data (Stripe vault objects, live Duffel order detail) | Not queryable from this codebase — Detoura holds only opaque references | **PROVIDER-HELD** | See `docs/V9_THIRD_PARTY_PRIVACY_PROVIDER_REGISTER.md`. A full DSAR response may need a parallel request to Stripe/Duffel directly, per their own DSAR processes, if the requester's rights extend there. |

## Step 2 — What must never be included in a DSAR response

Regardless of source, exclude:

- `password_hash` (Argon2id hash — a security credential, not the subject's
  "data" in the DSAR sense, and disclosing it creates risk with no benefit
  to the subject).
- `token_hash` / `csrf_token_hash` values on any session row.
- `provider_subject` from Stripe/Duffel/Google raw integration identifiers,
  unless specifically required to prove identity-linkage (Google's is
  already the one deliberate exception the automated export makes, per
  audit finding).
- Internal anti-abuse/rate-limit counters (not personal data about the
  subject's booking/account activity — they are Detoura's own operational
  signal).
- Any other user's data reached incidentally (e.g., if a booking somehow
  has multiple linked users — verify `trip_ownership.user_id` matches
  before including any booking).

## Step 3 — Assemble and deliver

There is no automated compiler for the above — an operator manually
assembles Sources 1–12 (as applicable) into a response document. Because
this is manual, double-check every `booking_id` used in Sources 5–12 came
from Step 1's own `list_trip_ids_for_user` call for the correct `user_id`
— never from a value supplied by the requester themselves, to avoid
manually re-introducing the IDOR class of bug this codebase's automated
paths already guard against structurally.

## Step 4 — Log the request (operational hygiene, not built here)

This runbook does not create a DSAR-tracking table. Record the request,
the verifying operator, the date, and what was produced in your
organization's existing operational/ticketing system — this is a process
requirement, not a new engineering deliverable.

---

## Explicit non-conclusions

This runbook does not decide:

- Whether Sources 5–12 (currently `MANUAL EXPORT` only) should be promoted
  to the automated `GET /api/v1/auth/account/export` endpoint — that is
  Pre-Beta Policy Question 11 (Data Export Contract), a Product/Legal
  decision, not an engineering one.
- Any retention duration for the sources it walks — see
  `docs/V9_RETENTION_REGISTER.md`.
- Legal sufficiency of this procedure as a complete GDPR Article 15
  response process — that is a Legal/Privacy determination.
