# V9 Frontend Reconnection Slice B Report

## Scope

Final closure pass for Slice B: consumer checkout and purchase reconnection.
This report covers verification, read-only review, and the isolated checkpoint
commit criteria. It does not start Slice C.

## Baselines

- Starting frontend baseline: `07d1b1e57ca738d3ef9e48025224911a7b586d59`
- Financial document backend: `cb5e10f59062a93a6d08b339548d81f7893e8949`
- Backend selection contract: `2b26d130711df18e87141a0aff81d9ba6445acfc`
- Task-start HEAD: `2b26d130711df18e87141a0aff81d9ba6445acfc`

## A. Real And Connected Code-Level Integration

- `TripRecommendation.selection_id` is represented in the frontend contract as
  `string | null`.
- `BookingExperience` passes a selected recommendation's `trip.selection_id`
  unchanged to `POST /api/v1/booking-intents` when present.
- The frontend does not manufacture, hash, or synthesize a `selection_id`.
- Null-selection recommendations fall back to demo booking-intent fields and
  cannot enter provider-backed All-in-One ticketing.
- Traveler details are submitted to the booking intent as one journey-level
  traveler party.
- Commercial display uses the backend `BookingIntent.commercial` summary and
  `commercial.breakdown.customer_total` for Pay Now.
- Payment creation is bound to the exact `booking_id`; amount and currency are
  not supplied by the client.
- All non-GET API requests attach the CSRF token when the CSRF cookie exists.
- Commercial changes, tier switches, and reconfirmation reset local payment
  authorization state and idempotency.
- Managed-path confirmation is blocked unless local payment status is
  `AUTHORIZED`.
- `UNKNOWN` and `RECONCILIATION_REQUIRED` payment states are treated as
  unresolved and do not advance to confirmation.
- The frontend waits for backend `pass_available` before fetching a travel pass;
  payment authorization and worker start are not treated as booking success.

## B. Browser And Runtime Verified Behavior

Environment:

- Backend: local `127.0.0.1:8000`
- Frontend: local Vite `127.0.0.1:5173`

Verified:

- Frontend origin search successfully reached the backend through the Vite
  proxy.
- Direct backend search returned status `200`, `supply: SYNTHETIC`,
  `selection_id: null`, route `CGN -> Munich -> Vienna -> CGN`, total `409.74`
  `EUR`, and `3` legs.
- Browser search returned synthetic recommendations and labeled them as not live
  bookable.
- Selecting a synthetic recommendation populated Your Journey without treating
  it as booked.
- Your Journey separated Payable Now from estimated transport, accommodation,
  transfer, and baggage costs.
- Unknown baggage was not rendered as zero.
- Synthetic/null selection disabled All-in-One with the message: "Real
  ticketing is available only for live provider results."
- Basic remained selectable for the null-selection journey.
- Traveler validation rendered required-name, email, and phone errors.
- A two-traveler submission advanced to review as one journey-level party.
- Review used server commercial truth for ticket total, Detoura fee, and Pay Now.
- Basic review copy stated that Detoura does not take payment or issue tickets
  for Basic.
- The browser URL did not contain traveler, payment, auth, or selection secrets.
- Desktop flow was verified through Basic review on the synthetic path.
- 360px mobile flow was visually checked for the journey drawer, trip details,
  service selection, disabled All-in-One state, primary action reachability, and
  horizontal overflow.

Runtime partials:

- Browser storage inspection was blocked by the browser harness returning
  unavailable storage objects. Code review verified that traveler, payment, and
  auth secrets are not persisted by the Slice B journey draft.
- Browser request interception was blocked by a non-extensible page object, so
  exact browser network body equality for live `selection_id` could not be
  captured.
- Live provider `selection_id` equality could not be runtime-verified because
  live Duffel/search configuration was unavailable.

## C. Statically Verified Behavior

- `selection_id` is nullable in the frontend DTO and migration-normalized to
  `null` for older saved drafts.
- Persisted journey draft stores selected trip/search context only; it does not
  store traveler party, payment, or authorization data.
- Payment DTO includes authoritative backend fields including `booking_id`,
  `currency`, `customer_total`, `authorized_amount`, status, provider, and
  optional `checkout_snapshot_id`.
- Frontend payment creation calls `/payments` with only `booking_id` and an
  idempotency key.
- Frontend payment confirmation calls `/payments/{payment_id}/confirm`.
- Final booking confirmation calls the existing booking-intent confirmation
  endpoint after eligible authorization on the managed path.
- `PAYMENT_UNKNOWN` and `RECONCILIATION_REQUIRED` states do not trigger blind
  retry or success.
- `reconfirm_required` clears payment authorization and returns the user to
  checkout for a fresh authorization.
- Travel pass rendering uses the backend travel-pass status instead of treating
  worker start as success.

## D. Provider Test Mode Not Verified

Real Stripe Test Mode:
NOT VERIFIED - CREDENTIALS UNAVAILABLE

Real Duffel Test Mode:
NOT VERIFIED - CREDENTIALS UNAVAILABLE

No fixture success was created to compensate for provider credentials.

## Reload And Replay

- Double-click final confirmation: PARTIAL. Buttons are disabled while busy and
  managed confirmation is authorization-gated; no live All-in-One confirmation
  could be exercised.
- Reload before payment: PARTIAL. Synthetic Basic path does not persist traveler
  or payment details; live checkout replay could not be tested without provider
  credentials.
- Reload after authorization: BLOCKED. Stripe authorization could not be
  performed without credentials.
- Reload during booking: BLOCKED. Live booking execution could not be performed
  without Duffel/Stripe credentials.
- Browser back/forward: PARTIAL. The synthetic browser path remained usable;
  live provider checkout could not be exercised.
- Network interruption: PARTIAL. Error branches are present, but live
  interruption during payment/booking was not performed.

## Storage And Privacy

- TravelerParty PII persistence: NONE found in Slice B journey draft.
- Payment secrets in browser storage: NONE found in Slice B code.
- Auth/session secrets in application storage: NONE found in Slice B code.
- `selection_id` may be stored only as part of the non-sensitive selected-trip
  journey draft.

## Accessibility

Runtime Accessibility: PARTIAL

Verified form labels, disabled states, visible validation messages, readable
payment/status messaging, and reachable primary actions on desktop and 360px.
Focus-after-error and explicit validation error association were not fully
verified.

## Build, Lint, And Tests

- `npm run build`: PASS
- `npm run lint`: PASS with existing warnings
- Frontend Automated Tests: NOT AVAILABLE

Existing lint warnings observed in unrelated files:

- `react-hooks/set-state-in-effect`
- `react-refresh/only-export-components`
- `@typescript-eslint/no-unused-vars`

## Independent Read-Only Review

Verdict: APPROVED

Open Critical: 0
Open High: 0
Open Medium: 0

Reviewed attack areas:

- Manufactured `selection_id`: no finding.
- Wrong selection binding: no finding.
- Null selection treated as provider-bookable: no finding.
- Synthetic/prior purchase leakage: no finding.
- Client-authoritative price/currency: no finding.
- Wrong payment-to-booking binding: no finding.
- Confirmation before authorization: no finding.
- Unpaid All-in-One path: no finding.
- Duplicate payment/confirmation: partial runtime coverage; no Critical, High,
  or Medium code finding.
- `PAYMENT_UNKNOWN` blind retry: no finding.
- `RECOVERY_REQUIRED` shown as success: no Critical, High, or Medium frontend
  finding; provider runtime remains unverified.
- Authorization shown as booking success: no finding.
- TravelerParty PII persistence: no finding.
- Auth/payment secret persistence: no finding.
- Fixture leakage: no finding.
- Desktop/mobile regressions: no blocking finding.
- Accessibility regressions: partial runtime coverage; no Critical, High, or
  Medium finding.

## Final Status

- Backend changes by Codex in this Slice B closure: NONE.
- Missing backend contracts for Slice B closure: NONE.
- Provider E2E remains limited by unavailable credentials.
- Slice B status: CLOSED, pending checkpoint commit.
