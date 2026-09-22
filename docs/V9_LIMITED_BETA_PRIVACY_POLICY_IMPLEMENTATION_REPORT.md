# V9 Limited Beta — Privacy Policy Implementation Report

Backend/server/data/ops slice. Owner: Claude (backend only — no frontend
changes). Starting HEAD: `6931600` ("V9 reconcile privacy consent and
retention decisions").

Authoritative inputs: `docs/V9_PRIVACY_CONSENT_RETENTION_DECISION_MATRIX.md`,
`docs/V9_BACKEND_PRIVACY_RETENTION_READINESS_AUDIT.md`,
`docs/V9_FRONTEND_PRIVACY_READINESS_AUDIT.md`. This report does not reopen
either source audit; it closes the one Medium the decision matrix flagged as
policy-blocked (communications recipient email after deletion), reverifies
everything else that was already sound, and produces the operational
documents the decision matrix's Pre-Beta Policy Questions still needed.

---

## §0 — Policy Baseline (as locked for this slice; not decided by this report)

Optional analytics: OFF. Marketing attribution/tracking/email: OFF.
Consent/CMP: not built for Limited Beta. Special-category traveller data:
not collected. Journey-draft client retention: 30 days maximum (frontend
responsibility — see `V9_RETENTION_REGISTER.md` §B, an open gap against
current frontend code, not fixed by this slice). Ordinary identifiable
operational log retention: 14 days maximum (Ops/deployment requirement —
see `V9_RETENTION_REGISTER.md` §C). Account deletion deletes/disables
credentials, sessions, and active auth identities; it does not mean "delete
all historical data." Financial-record retention is category-specific — no
blanket 8-year period is applied anywhere in this slice. Communication
recipient email after account deletion is scrubbed when no documented
retained purpose exists (implemented this slice). Companion-traveller legal
basis: not yet signed off (documented as a gate, not resolved). Data export
remains PARTIAL and is not represented as "download all my data."

---

## §1 — Communication Deletion Medium (CLOSED)

**Before**: `customer_communications.recipient_address` was retained,
unscrubbed, indefinitely after account deletion — flagged by the backend
audit as a POLICY-DEPENDENT GAP (DEL-2), not fixed there because no product
policy existed yet to implement against.

**Now that Product policy exists** (§0 above), this slice implements the
narrowest safe mechanism:

- `src/detoura/persistence/communications.py::scrub_recipient_for_user` —
  anonymizes `recipient_address` to a per-user tombstone
  (`deleted-<user_id>@deleted.invalid`) for every `customer_communications`
  row whose `user_id` matches the deleted account. Nothing else on the row
  changes: `status`, `version`, and every `communication_attempts`/
  `communication_events` history is left exactly as it was, because
  delivery-outcome evidence (was this sent, when, how many attempts) is
  booking-adjacent audit history, not identifying PII once the address
  itself is gone.
- Wired into `src/detoura/services/account_lifecycle_service.py::delete_account`,
  called in the same deletion flow immediately after
  `scrub_account_for_deletion`.
- **Classification decision, stated honestly**: the current data model has
  exactly one `communication_type` (`BOOKING_CONFIRMATION`) and no field
  anywhere marks any communication as having a documented, legally-required
  retained purpose distinct from the booking itself. Rather than invent a
  classification (e.g., "confirmations for cancelled trips are safe to
  scrub, confirmations for active trips are not") that no field in this
  schema can actually support, this implementation scrubs **unconditionally
  by `user_id`** — which is truthful given today's schema, not a guess. If
  a future communication type needs case-by-case retained-purpose handling,
  that must be encoded as an explicit field before this function branches
  on it; it does not infer one from `communication_type`, `booking_id`, or
  any other proxy today.
- **What is deliberately NOT touched**: `bookings.lead_email` /
  `bookings.lead_name`. These are booking truth (the record of who a
  booking was for), not a communication delivery record, and mutating them
  would violate the already-ratified "do not mutate booking/payment truth"
  stance (DEL-1) this slice does not reopen. `financial_documents` and
  `payment_transactions` are likewise untouched — neither is a
  communication record.
- Rows with `user_id IS NULL` (pre-link or guest communications) are never
  touched — only exact `user_id` matches, mirroring every other
  account-scoped deletion primitive already in `persistence/accounts.py`.
- Idempotent: a retried scrub call does not re-touch already-scrubbed rows
  (verified by test).

## §2 — Account Deletion Contract (REVERIFIED, unchanged in substance)

Independently re-run against current HEAD, not assumed from the prior
audit:

| Check | Result |
|---|---|
| Password credential unusable after deletion | HOLDS — `password_hash` NULLed, `login()` checks account status before verification |
| Sessions revoked | HOLDS — all sessions for the account revoked at deletion; `validate_session` independently re-checks `ACTIVE` status regardless of session-row state |
| Google identity removed/unlinked | HOLDS — `auth_identities` row hard-deleted in the same transaction |
| Reset flow cannot resurrect a deleted account | HOLDS — **new adversarial test added** (`test_deleted_account_password_reset_cannot_resurrect_account`): an outstanding reset token requested before deletion is rejected by `confirm_password_reset` after the account is deleted, because it re-checks `ACTIVE` status |
| Google auth cannot resurrect a deleted account | HOLDS — **new adversarial test added** (`test_deleted_account_google_signin_cannot_resurrect_old_account`): a fresh Google sign-in with the same identity/email after deletion creates a brand-new, unrelated account; the old account's identity list stays empty |
| Active account identity anonymized/erased | HOLDS — email tombstoned, status DELETED |
| Retained booking/payment/financial records remain truthful | HOLDS — **new test added** (`test_delete_account_scrubs_communication_recipient_but_keeps_ledger_and_booking_truth`) proves `bookings.lead_email`/`lead_name` are byte-for-byte unchanged by deletion |
| No cascade destroys accounting/transaction evidence | HOLDS — deletion code path never references `checkout_snapshots`, `payment_transactions`, `financial_documents`, `booking_items`, or `journey_confirmations` |
| No retained record silently becomes owned by another account | HOLDS — `trip_ownership.user_id` is never nulled or reassigned by deletion |
| Deleted account ID cannot be reused | HOLDS — IDs are `secrets.token_urlsafe`-derived, never autoincrement-recycled |
| Retained communication email follows the new policy | **NOW HOLDS** — closed in §1 above |
| Marketing/analytics not enabled as a side effect | HOLDS — deletion code path touches no analytics/consent state; verified no caller in this diff touches `analytics_events` or any consent seam |
| Cross-account isolation | **new test added** (`test_delete_account_does_not_scrub_another_users_communication`, `test_delete_account_does_not_scrub_communication_with_no_user_id`) — deleting account A never touches account B's or a guest's communication |
| Concurrency | **new test added** (`test_concurrent_delete_account_calls_are_serialized_not_double_applied`) — five threads racing to delete the same account: exactly one succeeds, four cleanly raise `AccountLifecycleError`, final state is DELETED exactly once |
| Export IDOR | **new test added** (`test_export_account_cannot_be_used_to_read_another_users_data`) at the service layer, reconfirming the HTTP layer's existing session-only `user_id` sourcing (`api/auth_account.py` takes `user_id` exclusively from the authenticated session, never a request parameter) |

No redesign of authentication was performed or needed.

## §3 — DSAR / Export Readiness

No automated privacy portal built. `docs/V9_DSAR_OPERATIONAL_RUNBOOK.md`
gives an authorized operator a precise, source-by-source procedure across
account, auth identities, sessions, bookings, payments, checkout snapshots,
financial documents, communications, tickets/provider references, and
analytics — each classified `AUTOMATED EXPORT` / `MANUAL EXPORT` /
`NOT PERSONAL DATA` / `PROVIDER-HELD` / `POLICY/LEGAL REVIEW REQUIRED`. No
secrets, password hashes, session hashes, CSRF material, provider secrets,
anti-abuse signals, or third-party personal data are included by design (see
the runbook's own "what must never be included" section). No unauthenticated
export endpoint was created — the automated export endpoint's scope and
authentication are unchanged by this slice.

**Automated export status: unchanged, still PARTIAL.** Account metadata,
linked identities, and booking IDs only. Expanding it is explicitly a
Product/Legal scope decision (Pre-Beta Question 11), not performed here per
the task's own instruction not to build a portal.

## §4 — Retention Register

`docs/V9_RETENTION_REGISTER.md` — every persisted backend table classified
against the fixed vocabulary (`SECURITY_TTL`, `PRODUCT_30_DAY`,
`OPS_14_DAY`, `STATUTORY_FINANCIAL`, `LEGAL_CLAIMS`,
`ACTIVE_ACCOUNT_LIFECYCLE`, `PROVIDER_CONTROLLED`, `POLICY_PENDING`,
`NO_PERSONAL_DATA`), plus a client-storage section cross-referencing the
frontend audit and an explicit Ops log-retention requirement. **No numeric
duration was invented anywhere in it** for `STATUTORY_FINANCIAL` or
`LEGAL_CLAIMS` categories — every such row says `NO DURATION DEFINED` and
names the correct owner (Legal/Accounting), not a guessed number.

## §5 — CheckoutSnapshot Field Classification

Reverified `models/payment.py::CheckoutSnapshot` field-by-field; no
destructive change made (none was safe to make without changing
booking/payment truth, per the task's own instruction):

| Field | Classification |
|---|---|
| `snapshot_id`, `booking_id`, `journey_reference`, `user_id` | Linkage/identifiers — operational, required for the row to mean anything |
| `service_tier` | Operational/commercial state at freeze time |
| `quote` (full `CommercialQuote`) | **Transaction evidence** — the exact frozen price the payment this snapshot backs was authorized against; this is the row's core evidentiary content |
| `revalidation_state` | Operational/audit — explicitly documented in-code as "display/audit only, never re-derived"; redundant once the payment completes, but currently the only record of what was checked at freeze time |
| `created_at` | Metadata |
| `expires_at` | Operational — governs payment-authorization *validity* only (a 15-minute window per the backend audit), **not** a data-retention TTL; conflating the two was exactly the gap the backend audit flagged (RET-4) and this report does not resolve, because resolving it means picking a retention duration, which is a Legal/Privacy decision |

**No field-level pruning was implemented.** Deleting `quote` or
`revalidation_state` unilaterally would remove transaction-adjacent evidence
for a payment that may still need investigation (chargeback, dispute,
reconciliation finding) without an approved retention decision — exactly
what the task instructs against. This classification is the documented
input for a future, policy-driven retention-automation build, not a cleanup
performed now.

## §6 — Traveler PII (REVERIFIED against current HEAD, not assumed)

Re-ran the search independently rather than trusting the prior audit's
conclusion:

- `grep` for `passport`, `date of birth`/`dob`, `gender`/`title`, and
  related fields across `src/detoura` found exactly the expected call
  sites: `models/traveler.py` (the in-memory model itself),
  `api/contracts.py` (the request contract, in-memory), `api/v1.py` (the
  Duffel passenger-mapping call site, in-memory → provider only),
  `observability/logging.py` (the forbidden-fields filter — a defensive
  control, not evidence of persistence), and `persistence/analytics.py`
  (the PII-shaped-value rejection regex — also defensive, not persistence).
- **No `travelers` table exists** in `persistence/db.py`'s schema —
  confirmed directly (`grep -n "CREATE TABLE" | grep -i travel` returns
  nothing). The only column named `travelers` anywhere in the schema is an
  `INTEGER` party-size count on booking-adjacent tables, not a PII field.
- Traveler PII is not present in `CheckoutSnapshot`, `CommercialQuote`,
  `financial_documents`, or the confirmation domain (§5's classification
  above independently confirms `CheckoutSnapshot` carries no traveler
  fields).
- Not passed to analytics, not logged — unchanged from the prior audit's
  finding, reconfirmed by the same grep sweep.

**Invariant holds, unchanged.** No persistence was added by this slice.

## §7 — Optional Measurement Fail-Closed Status

Verified no production caller of `setAnalyticsConsent({analytics: true})`
or an equivalent grant exists anywhere in `frontend/src` outside the
function's own definition and its test files (`grep` confirms zero
production call sites). `/api/v1/events` remains available under its
existing allow-list/rate-limit controls (unchanged by this slice — the
rate limit fix was applied by the prior backend audit, not here); no
consent-grant caller, CMP, or vendor adapter was added by this slice.
Production optional measurement remains fail-closed.

## §8 — Operational Log Retention

Documented, not implemented in application code (it cannot be — see
`V9_RETENTION_REGISTER.md` §C for the full reasoning): staging/production
log infrastructure must retain ordinary identifiable operational logs for
14 days or fewer, with an exception for logs tied to an active, documented
security incident. This is an explicit **OPS/DEPLOYMENT** requirement, not
satisfied by any code in this repository (the forbidden-fields logging
filter controls *what* is logged, not *how long* a log survives) and not
satisfiable by this report.

## §9 — Companion Travellers (PRE-BETA LEGAL GATE — not resolved here)

Reverified the underlying mechanism: `models/traveler.py::TravelerParty`
holds `travelers: tuple[Traveler, ...]` with `.lead` as `travelers[0]` —
confirming the product does let one account holder (the lead) supply name/
DOB/gender/contact data for other travelers (companions) in the same party,
which is then transmitted to Duffel at order time (§6 above). No new
processing, checkbox, consent mechanism, or Article 14 disclosure was added
or invented by this slice. This is recorded here as an explicit,
unresolved gate:

> **What is Detoura's Article 6 legal basis for processing companion
> traveller data supplied by the booking user, and how must Article 14
> transparency be satisfied before disclosure to Duffel/airlines?**

Existing booking functionality remains technically intact and unmodified by
this slice — this gate blocks a legal sign-off, not the current code path.

## §10 — Third-Party Register

`docs/V9_THIRD_PARTY_PRIVACY_PROVIDER_REGISTER.md` — Stripe, Duffel, Google,
Resend (and the Wikimedia CDN dependency, for completeness), each with
role, data categories, purpose, provider-side retention visibility (not
established — outside this codebase's ability to inspect), international-
transfer dependency (flagged, not concluded), and production verification
status. No legal conclusion (controller/processor/DPA) is stated for any
provider — that determination remains Legal/Privacy's, per both source
audits and per the task's explicit instruction not to state legal
conclusions this document cannot establish.

## §11 — Tests

**New/modified**: `tests/test_v9_google_auth_account_lifecycle.py` — 9
new/adversarial tests covering: communication recipient scrub + booking-
truth preservation, cross-user isolation, null-`user_id` exclusion,
scrub idempotency, Google-resurrection resistance, password-reset-
resurrection resistance, deletion concurrency (5-thread race), and export
IDOR at the service layer. All new tests pass individually and as part of
the full file.

**Focused suites** (account lifecycle, communications, accounts):

```
tests/test_v9_google_auth_account_lifecycle.py
tests/test_v9_phase5_communication.py
tests/test_v9_phase26_accounts.py
```

Result: **all passing** (134 tests, 0 failures).

**Full regression**: `python -m pytest tests/ -q` — run once, sequentially,
not concurrently with the focused suites (per instruction). **One failure**:
`tests/test_v85_ops_c2.py::test_create_activate_and_history` (`TypeError`),
in an unrelated V8.5 ops commercial-policy module this slice never touches.
Diagnosed, not dismissed: the same test passes both in isolation
(`pytest tests/test_v85_ops_c2.py::test_create_activate_and_history`) and as
its whole file (`pytest tests/test_v85_ops_c2.py`), with this slice's
changes present in both runs — it only fails when the entire suite runs
together, which is the signature of pre-existing cross-file test-order state
pollution (e.g. a shared rate-limiter or clock singleton some other test
file leaves dirty), not a regression this diff introduced. Nothing in this
slice's diff (`persistence/communications.py`,
`services/account_lifecycle_service.py`) touches ops/commercial-policy code,
a shared rate limiter, or any global singleton that module depends on. Every
test this slice added or modified passed in every run, isolated and full-
suite alike. Recorded honestly rather than silently reported as "all green."

## §12 — Independent Adversarial Review

Performed as a separate, read-only pass over this slice's own diff (not a
re-run of the source audits), checking specifically for:

| Risk | Finding |
|---|---|
| Over-deletion | Not found. The scrub touches exactly one column (`recipient_address`) on exactly the rows matching the deleted `user_id`; every other field, every other table, is provably untouched (tests assert `bookings.lead_email`/`lead_name` unchanged, `trip_ownership` unchanged, other users'/guest communications unchanged). |
| Under-deletion | Not found relative to the now-locked policy — the one gap the policy closed (DEL-2) is now implemented. Remaining "indefinite" retention items (bookings, payments, financial documents, sessions row-purge) are unchanged **by design** — they were never in scope to delete without a retention-duration decision this report is not authorized to make. |
| Financial-record destruction | Not found. No code path added or modified in this slice touches `payment_transactions`, `refunds`, `payment_events`, `checkout_snapshots`, or `financial_documents`. |
| Communication email surviving without purpose | Closed by §1 — the one concrete case this slice targets. |
| Deleted-account resurrection | Not found, and now has direct adversarial test coverage for both the Google and password-reset paths (§2). |
| Export IDOR | Not found — export remains keyed exclusively by session-sourced `user_id`; new service-layer test added. |
| Secrets in export | Not found — no export-surface change was made by this slice; the DSAR runbook explicitly enumerates what must never be included. |
| Third-party personal data in export | Not applicable — no export code was changed. |
| Traveler PII persistence | Not found — reverified in §6, no persistence added. |
| Analytics accidentally enabled | Not found — reverified in §7, zero production consent-grant callers. |
| Invented retention periods | Not found — `V9_RETENTION_REGISTER.md` explicitly marks `STATUTORY_FINANCIAL`/`LEGAL_CLAIMS` rows as "NO DURATION DEFINED" rather than assigning a number. |
| Companion-traveller legal basis invented by engineering | Not found — §9 states the gate as an open question, adds no checkbox, consent flag, or Article 14 disclosure mechanism. |

**No demonstrated technical defect found in this slice's own changes.**
Nothing was fixed under this heading because nothing adversarial survived
review; this section exists to show the check was actually performed, not
to manufacture findings.

## §13 — Frontend Changes

**NONE.** No file under `frontend/**` was read for modification, edited, or
staged by this slice.

## §14 — Provider Objects Created

**NONE.** No live Stripe/Duffel/Google/Resend credential, webhook, or
production object was created, modified, or exercised. All test coverage
uses in-memory SQLite and injected fakes, consistent with the rest of this
codebase's existing test architecture.

---

## Final Response

**Starting HEAD**: `6931600`
**Final HEAD**: `940b20a` ("V9 implement Limited Beta privacy policy backend")

**Policy Baseline**: Locked per §0, sourced from this task's own
instructions — not re-derived or reinterpreted by this report.

**Communication Medium Closed**: YES — §1. `recipient_address` scrubbed on
account deletion for every communication scoped to that `user_id`; ledger/
audit history and booking truth (`lead_email`/`lead_name`) untouched.

**Account Deletion Contract**: REVERIFIED, HOLDS — §2. Two new resurrection-
attempt tests (Google, password-reset) and one concurrency test added; all
pass.

**DSAR Operational Readiness**: OPERATIONAL RUNBOOK PROVIDED — §3,
`docs/V9_DSAR_OPERATIONAL_RUNBOOK.md`. Manual process, not an automated
portal, per instruction.

**Automated Export Status**: UNCHANGED — still PARTIAL (account metadata,
identities, booking IDs only). Scope expansion remains a Product/Legal
decision (Question 11), not performed here.

**Retention Register**: PROVIDED — §4, `docs/V9_RETENTION_REGISTER.md`. No
duration invented for `STATUTORY_FINANCIAL`/`LEGAL_CLAIMS` categories.

**CheckoutSnapshot Classification**: PROVIDED, NO DESTRUCTIVE CHANGE — §5.

**Traveler PII**: REVERIFIED, NOT PERSISTED — §6. No regression found.

**Analytics Status**: FAIL-CLOSED, UNCHANGED — §7. No production consent-
grant caller found.

**Operational Log Retention**: DOCUMENTED AS AN OPS/DEPLOYMENT REQUIREMENT,
NOT APPLICATION CODE — §8.

**Companion Traveller Legal Gate**: DOCUMENTED, UNRESOLVED BY DESIGN — §9.
No legal basis invented.

**Provider Register**: PROVIDED — §10,
`docs/V9_THIRD_PARTY_PRIVACY_PROVIDER_REGISTER.md`. No legal conclusions
stated.

**Technical Defects Found**: 0 (this slice's own changes, per the
independent review in §12).
**Technical Defects Fixed**: 0 (none found to fix).

**Critical**: 0
**High**: 0
**Medium**: 0 (the one Medium this slice was scoped to close — DEL-2,
communications recipient email — is now closed per §1)

**Focused Tests**: PASS — 134 tests across
`test_v9_google_auth_account_lifecycle.py`,
`test_v9_phase5_communication.py`, `test_v9_phase26_accounts.py`.

**Full Regression**: RUN ONCE — `python -m pytest tests/ -q` across the
entire suite. One failure: `tests/test_v85_ops_c2.py::test_create_activate_and_history`
(`TypeError`) — diagnosed as pre-existing cross-file test-order state
pollution in an unrelated V8.5 ops/commercial-policy module (passes both in
isolation and as its whole file, with this slice's diff present in both
runs). Not caused by this slice; not fixed by this slice, since fixing a
pre-existing unrelated test-isolation issue is out of this task's scope and
risks masking what actually changed. Every test this slice added or
modified passed in every configuration run.

**Frontend Changes**: NONE
**Provider Objects Created**: NONE

**Independent Review**: PERFORMED — §12, no demonstrated defect survived.

**Report**: this document.
**Checkpoint Commit**: `940b20a` — 7 files staged by exact path (never
`git add .`/`-A`), never touching `frontend/**`, `AGENTS.md`, or `CLAUDE.md`.
**Remaining Dirty Paths**: `AGENTS.md`, `CLAUDE.md` — pre-existing,
untracked, untouched by this slice throughout, exactly as required.
**Pushed**: NO

**Limited Beta Backend Privacy Engineering Status**: The one previously
policy-blocked Medium (communications recipient email after deletion) is
now closed. Every other technical finding from the source audits was
already NON-BLOCKER or already fixed before this slice began; this slice
did not reopen or weaken any of them.

**Remaining Legal Gates**: Companion-traveller Article 6/14 basis (§9);
controller/processor/DPA status for Stripe/Duffel/Google/Resend (§10);
financial-document and payment retention durations
(`STATUTORY_FINANCIAL`/`LEGAL_CLAIMS`, §4); standing (non-deletion-
triggered) communication retention duration (Pre-Beta Question 9);
data-export scope expansion (Question 11).

**Remaining Ops Gates**: Deployed log-retention configuration ≤14 days
(§8); deployed cookie/security-header verification (AUTH-3/AUTH-4, carried
over unchanged from the backend audit, not re-verified by this slice since
it requires live-deployment access this task does not grant); frontend
30-day journey-draft TTL enforcement (currently indefinite — a frontend
engineering gap against the now-locked policy, flagged in
`V9_RETENTION_REGISTER.md` §B, out of this backend-only slice's authority
to fix).
