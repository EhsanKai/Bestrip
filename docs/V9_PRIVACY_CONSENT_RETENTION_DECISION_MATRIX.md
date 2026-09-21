# V9 Privacy / Consent / Retention Decision Matrix

Reconciles `docs/V9_FRONTEND_PRIVACY_READINESS_AUDIT.md` (checkpoint `b7108ad`)
and `docs/V9_BACKEND_PRIVACY_RETENTION_READINESS_AUDIT.md` (checkpoint
`b577c26`) into one decision-tracking document for Detoura Limited Beta.

This is a **report-only reconciliation**. No application code, frontend
code, consent UI, or retention job was implemented or changed to produce
this document. No legal requirement, retention duration, or consent policy
is decided here — every such item is left as an open, precisely-worded
question for its stated owner.

---

## Executive Summary

Both source audits independently reached the same shape of conclusion: the
**technical** foundation is sound (no Critical/High policy-independent
defect remains in either layer after each audit's own fixes), and the
remaining work is almost entirely **policy decisions**, not engineering
defects. Analytics/measurement stays fail-closed in production today (no
consumer consent grant exists), traveler PII is not persisted anywhere
server-side, account deletion and session security hold up under adversarial
review, and no IDOR exists in export or document access.

What remains open is the same theme repeated across both layers: **no
retention policy is defined for almost any table or browser store holding
account, booking, payment, or communication data**, the **data export
feature is partial** (metadata/identity/booking-IDs only), and **consent UI,
purpose approval, and wording do not yet exist**. None of this is a security
defect — it is undecided policy sitting on top of an already fail-closed,
already-safe technical default.

This document does not conclude that Detoura is legally or compliance-ready
for Limited Beta. It concludes that Detoura is **technically able to launch
with its current fail-closed defaults** while the listed policy decisions
are made in parallel — see §12 for the explicit distinction between
technical and legal/policy readiness that this document preserves from the
backend audit.

---

## Authoritative Inputs

- Frontend privacy audit: `docs/V9_FRONTEND_PRIVACY_READINESS_AUDIT.md`, checkpoint `b7108ad` ("V9 audit frontend privacy readiness")
- Backend privacy/retention audit: `docs/V9_BACKEND_PRIVACY_RETENTION_READINESS_AUDIT.md`, checkpoint `b577c26` ("V9 audit backend privacy and retention readiness")
- Both audits are treated as closed and authoritative for their own scope. Neither is reopened here; current code was inspected only to resolve the handful of cross-layer questions noted in §Cross-Layer Reconciliation.

---

## Cross-Layer Reconciliation

**NO MATERIAL CROSS-LAYER CONTRADICTIONS FOUND.**

Every claim that both audits touch is consistent: session/cookie handling,
Google OAuth ticket/expiry behavior, password-reset flow, traveler PII
non-persistence, and third-party fail-closed defaults are described the same
way (at whatever level of detail each layer chose) by both reports. Neither
report claims a deletion or export behavior that the other contradicts —
notably, the frontend audit does not describe any consumer-facing account
deletion or data-export UI at all, so there is no frontend claim of
completeness for either capability to check against the backend's PARTIAL
findings.

The following are **reconciliation notes, not contradictions** — places
where combining the two reports surfaces something worth flagging that
neither report states incorrectly on its own:

1. **Analytics revocation does not reach already-recorded server data.**
   Frontend: revoking consent clears the local queue, timers, and both
   measurement identifiers (`detoura.fk.s`/`detoura.fk.v`) and removes
   attribution — but only ever for *pending, not-yet-sent* data; already
   transmitted requests cannot be recalled (frontend audit, §4). Backend:
   `analytics_events` has no cleanup mechanism at all and no
   deletion-by-session/visitor-key path exists (backend audit, Retention
   Matrix, Analytics/Event Backend). Neither report is wrong; combined, they
   show that today's "revoke" is a **forward-only** control, not an erasure
   of anything already recorded. See ANL-5.

2. **"Saved" means two different things in the two layers.** The frontend's
   `detoura-saved` / journey-draft browser storage is pre-booking planning
   data that never leaves the browser except as ordinary search/booking API
   calls (frontend audit, §2–3). The backend's `trip_ownership` table
   records actual booked-trip ownership after a real booking exists (backend
   audit, Backend Personal Data Inventory). These are different records with
   no shared identifier or lifecycle; the naming overlap ("saved
   trip"/"saved recommendation") is worth avoiding in future consumer copy
   to prevent confusing "what's in your browser" with "what Detoura's
   server owns."

3. **Audit coverage gap, not a contradiction: manual support diagnostics.**
   The frontend audit flags (B5) that `ErrorState`/`SlowSearchNotice` can
   build user-visible diagnostic copy containing a full URL and
   search/provider context for the user to send to support, and states that
   "recipient/minimization/retention needs policy review." The backend audit
   does not address a support-diagnostic ingestion path at all — its
   Communications section only covers `customer_communications` (booking
   confirmations). It is not established by either audit whether this
   diagnostic content ever reaches a Detoura-controlled backend store (e.g.,
   a support inbox, a logged ticket) or stays entirely client-side (e.g., a
   pre-filled `mailto:` link the user sends from their own client). This is
   an **open coverage gap** between the two audits, not a resolved fact in
   either direction — see EXT-3.

4. **Deployed configuration is asserted by neither audit.** Both audits are
   explicit that they reviewed source code, not a live deployment. Cookie
   attributes (`Secure`/`HttpOnly`/`SameSite`), live OAuth/email provider
   configuration, and security headers are described as *configured in
   code* by the backend audit and flagged as *unverified in production* by
   the frontend audit (C1–C3). This is not a contradiction — both audits
   agree on the same unresolved verification gap, just from their own side.

---

## Unified Decision Matrix

Columns: **ID | Topic | Current Technical Truth | Current Safety State |
Decision Needed | Decision Owner | Engineering Needed After Decision? |
Limited Beta Classification | Failure-Safe State Today | Recommended Next
Action**

### A. Analytics / Measurement

| ID | Topic | Current Technical Truth | Current Safety State | Decision Needed | Decision Owner | Eng. After Decision? | Limited Beta Classification | Failure-Safe State Today | Recommended Next Action |
|---|---|---|---|---|---|---|---|---|---|
| ANL-1 | Optional analytics activation | Frontend defaults `{analytics:false, marketing:false}`; no production consent-grant caller exists; backend endpoint allow-lists event names/props, rejects PII-shaped values, now rate-limited | FAIL-CLOSED (disabled in production) | Which optional analytics purposes, if any, are approved for Limited Beta, and what consent mechanism gates them | MULTI-OWNER (PRODUCT, LEGAL/PRIVACY) | YES — consent UI (read/subscribe/persist/revoke wiring); no further backend change expected | FAIL-CLOSED ACCEPTABLE PENDING DECISION | Safe — analytics cannot run without a consent-UI grant that does not yet exist | Decide purposes + wording; lock Product contract; only then design Consent UI |
| ANL-2 | Attribution / UTM capture | sessionStorage, permission-gated, 30-day TTL checked on read, bounded label set; inactive in production absent a consent grant | FAIL-CLOSED | Is attribution an approved purpose for Limited Beta, and what TTL/scope | MULTI-OWNER (PRODUCT, LEGAL/PRIVACY) | Minor — TTL/governance already coded, only the approved duration/scope needs confirming | FAIL-CLOSED ACCEPTABLE PENDING DECISION | Safe — inactive without consent | Decide alongside ANL-1 |
| ANL-3 | Marketing purpose | Boolean seam (`marketing`) exists with no adapter; no marketing-communication capability found anywhere in the backend | INACTIVE (no adapter, no backend feature) | Is marketing an active purpose for Limited Beta at all | MULTI-OWNER (PRODUCT, LEGAL/PRIVACY) | Only if activated; none needed if it stays inactive | POST-BETA FOLLOW-UP | Safe — inactive | Confirm marketing stays out of scope for Limited Beta |
| ANL-4 | Backend analytics/event retention | `analytics_events` is INDEFINITE with no cleanup mechanism at all | Not itself a PII exposure (no PII persisted) but an unbounded-retention gap | What retention duration applies once analytics is enabled, and does it require automated deletion | MULTI-OWNER (LEGAL/PRIVACY, PRODUCT, ENGINEERING) | YES eventually — no scheduler infrastructure exists in the backend today | FAIL-CLOSED ACCEPTABLE PENDING DECISION | Safe while analytics stays disabled | Decide duration alongside ANL-1; defer automation build until analytics is actually enabled |
| ANL-5 | Revocation reaching already-recorded server data | Frontend revoke clears local queue/identifiers/attribution but cannot recall already-transmitted events; backend has no delete-by-session/visitor-key path | Gap — revocation is forward-only, not erasure | Must revocation also delete already-recorded backend analytics rows tied to a session/visitor key, and how | MULTI-OWNER (LEGAL/PRIVACY, PRODUCT, ENGINEERING) | YES if required — no such deletion path exists today | FAIL-CLOSED ACCEPTABLE PENDING DECISION | Safe while analytics stays disabled (no rows are being created) | Decide as part of the same ANL-1 policy package before any activation |

### B. Authentication / Account

Core auth security (sessions, password, Google, reset, logout) was
independently verified sound by both audits, with no defect remaining after
each audit's own fixes — it carries no decision, no owner, and no Limited
Beta classification because there is no open gate item to classify. It is
noted here only so its resolution is not mistaken for an oversight.

| ID | Topic | Current Technical Truth | Current Safety State | Decision Needed | Decision Owner | Eng. After Decision? | Limited Beta Classification | Failure-Safe State Today | Recommended Next Action |
|---|---|---|---|---|---|---|---|---|---|
| AUTH-2 | Google OAuth PKCE verifier/nonce stored raw (not hashed) | Minor inconsistency vs. hashed `state`; short-lived, single-use, no reuse value | Low-risk | None — engineering hardening choice, not a policy question | ENGINEERING | Optional hardening | POST-BETA FOLLOW-UP | Acceptable | Backlog hardening ticket, non-urgent |
| AUTH-3 | Deployed cookie/security-header configuration | Code defaults documented (HttpOnly, 14-day session TTL, etc.); neither audit verified the deployed values | UNKNOWN until verified | Confirm deployed cookie attributes and headers match the coded defaults | OPERATIONS | Possibly, if deployed config diverges | PRE-BETA IMPLEMENTATION REQUIRED | Unverified — must confirm | Ops verifies deployed configuration before real users onboard |
| AUTH-4 | Live Google OAuth / email provider configuration and log redaction | Code fails closed with no configuration; live behavior (real Google project, real Resend sending) not verified end-to-end by either audit | Fails closed by default | Confirm live provider configuration and redaction behave as coded, in the deployed environment | MULTI-OWNER (OPERATIONS, EXTERNAL PROVIDER) | Possibly | PRE-BETA IMPLEMENTATION REQUIRED | Safe by default (fails closed without config) | Ops/staging verification pass before enabling live sending/sign-in for real users |

### C. Account Deletion

| ID | Topic | Current Technical Truth | Current Safety State | Decision Needed | Decision Owner | Eng. After Decision? | Limited Beta Classification | Failure-Safe State Today | Recommended Next Action |
|---|---|---|---|---|---|---|---|---|---|
| DEL-1 | Deletion does not cascade into bookings/payments/financial docs | Deliberate, documented current behavior — deletion anonymizes the account and erases sessions/Google identity, but never touches booking/payment/financial truth | SAFE technically; ownership checks correct; but this is a retention posture, not an erasure guarantee | Ratify that bookings/payments/financial truth are retained after account deletion, and confirm/duration | MULTI-OWNER (PRODUCT, LEGAL/PRIVACY) | NO (current behavior already matches the documented stance) | PRE-BETA DECISION REQUIRED (before any consumer-facing deletion copy ships) | Safe as coded | Legal/Privacy + Product ratify current behavior as the intended contract, or direct a change |
| DEL-2 | Communications recipient email / booking lead-contact retained after deletion | `customer_communications.recipient_address` and `bookings.lead_email`/`lead_name` are not scrubbed on account deletion | Policy-dependent gap, not a security defect; retaining is the conservative default | Should recipient email / lead-contact fields be anonymized, retained as-is, or handled differently after deletion | MULTI-OWNER (PRODUCT, LEGAL/PRIVACY) | YES if scrubbing is decided | FAIL-CLOSED ACCEPTABLE PENDING DECISION | Safe (no unauthorized access; just retained) | Decide alongside DEL-1 |
| DEL-3 | Deleted account's email released for immediate re-registration | Tombstone frees the original email for a new registrant right away; a later Google sign-in with that email creates a distinct new account, never re-links to the deleted one | Standard consumer-app behavior; not a security defect | Confirm this matches product/legal intent | MULTI-OWNER (PRODUCT, LEGAL/PRIVACY) | NO unless the decision reverses current behavior | FAIL-CLOSED ACCEPTABLE PENDING DECISION | Safe as coded | Explicit sign-off, no code change expected |
| DEL-4 | Feature gaps: single-session revocation, Google unlink, per-trip deletion | Only "current session" or "all sessions" revocation exists; no Google-unlink-only endpoint; no per-trip delete | Not a privacy defect — a capability gap | None (Product backlog prioritization only) | PRODUCT | YES if prioritized | POST-BETA FOLLOW-UP | Safe (capability absence, not exposure) | Backlog per Product priority |

### D. Data Export

| ID | Topic | Current Technical Truth | Current Safety State | Decision Needed | Decision Owner | Eng. After Decision? | Limited Beta Classification | Failure-Safe State Today | Recommended Next Action |
|---|---|---|---|---|---|---|---|---|---|
| EXP-1 | Export scope is PARTIAL (metadata/identity/booking-IDs only) | Exports account metadata, linked identities, and opaque booking IDs; payments, financial documents, communications, tickets, saved-trip detail are technically available (user-scoped accessors already exist) but not wired in; no IDOR | Safe (no unauthorized access); scope is honestly limited, not overclaimed by the code or its own docstring | What data-subject export scope must Detoura provide for Limited Beta | MULTI-OWNER (PRODUCT, LEGAL/PRIVACY) | YES if scope expands — described by the backend audit as "a wiring exercise," not a new-capability build | FAIL-CLOSED ACCEPTABLE PENDING DECISION, **contingent on export never being described to users as complete** | Safe — but must not be marketed as "download all your data" | Decide scope; if unchanged, ensure any user-facing label says "account & identity export," not "all your data" |

### E. Retention

| ID | Topic | Current Technical Truth | Current Safety State | Decision Needed | Decision Owner | Eng. After Decision? | Limited Beta Classification | Failure-Safe State Today | Recommended Next Action |
|---|---|---|---|---|---|---|---|---|---|
| RET-1 | Sessions / password-reset / Google OAuth transient state — no row purge | TTLs are checked logically at read/consume time; rows are never deleted after expiry/consumption | Safe (expired rows can't authenticate) but accumulate indefinitely | What retention duration applies to these rows, and is automated purge required | MULTI-OWNER (LEGAL/PRIVACY, PRODUCT, ENGINEERING) | YES if automated purge is required — no scheduler infra exists | FAIL-CLOSED ACCEPTABLE PENDING DECISION | Safe (expiry enforced logically) | Decide duration; treat automation as its own engineering item, likely post-Beta |
| RET-2 | Saved trips / bookings / journey confirmations — indefinite | No table-level policy or cleanup | Safe (no exposure) | Retention duration for booking-lifecycle records | MULTI-OWNER (LEGAL/PRIVACY, PRODUCT) | YES if automated purge is required | FAIL-CLOSED ACCEPTABLE PENDING DECISION | Safe | Decide alongside DEL-1 |
| RET-3 | Payments / refunds / payment events — indefinite | No table-level policy or cleanup | Safe (no exposure; financial ledger integrity requires care with any deletion) | Retention duration for financial transaction records | MULTI-OWNER (LEGAL/PRIVACY, PRODUCT) | YES if automated purge is required, with care for ledger integrity | FAIL-CLOSED ACCEPTABLE PENDING DECISION | Safe | Legal/Privacy decide with accounting input |
| RET-4 | CheckoutSnapshot — no data-retention TTL (only a payment-validity window) | `expires_at` governs price-authorization validity, not row deletion; rows persist indefinitely | Safe (no exposure) | Retention duration independent of the existing validity window | MULTI-OWNER (LEGAL/PRIVACY, PRODUCT) | YES if automated purge is required | FAIL-CLOSED ACCEPTABLE PENDING DECISION | Safe | Decide duration; distinct question from the 15-minute validity window |
| RET-5 | Financial documents (receipts/invoices/credit notes) — indefinite, immutable by design | No TTL/archival policy; VAT/legal-invoice compliance flagged elsewhere in-code as unresolved | Safe (no exposure; ownership-checked downloads) | Retention duration required by accounting/tax obligations | LEGAL/PRIVACY | YES if automated purge/archival is required | FAIL-CLOSED ACCEPTABLE PENDING DECISION | Safe | Legal/Privacy confirm with accounting/tax input |
| RET-6 | Communications — indefinite | See DEL-2 | Safe (no exposure) | Retention duration for delivered-communication records | MULTI-OWNER (LEGAL/PRIVACY, PRODUCT) | YES if automated purge is required | FAIL-CLOSED ACCEPTABLE PENDING DECISION | Safe | Decide alongside DEL-2 |
| RET-7 | Analytics/events — indefinite, no cleanup at all | See ANL-4 | Safe while analytics is disabled | Retention duration once analytics is enabled | MULTI-OWNER (LEGAL/PRIVACY, PRODUCT, ENGINEERING) | YES eventually | FAIL-CLOSED ACCEPTABLE PENDING DECISION | Safe (analytics disabled) | Decide alongside ANL-1 |
| RET-8 | Browser/shared-device planning-state retention (theme, saved recommendations, journey draft) | INDEFINITE in browser storage, no TTL/count cap; frontend audit notes shared-device visibility is unaddressed | Safe from a server standpoint (client-only, not authoritative); a shared-device privacy question for the person using that browser | Should saved/draft planning data get a TTL, size cap, or shared-device-aware handling | MULTI-OWNER (PRODUCT, LEGAL/PRIVACY, FRONTEND) | YES if a cap/TTL/clear-on-shared-device behavior is required | FAIL-CLOSED ACCEPTABLE PENDING DECISION | Safe (not server-authoritative) | Decide as part of the client retention policy set |
| RET-9 | Non-PII search intelligence (price observations, search traces, market priors) | The one category with a real, working retention mechanism — 180/365-day TTL, one automatic, one manual-ops-only | DEFINED (technically) | None from a technical standpoint; confirm this is not being relied upon as a stand-in for a real data-retention policy elsewhere | ENGINEERING (documentation only) | NO | POST-BETA FOLLOW-UP | Safe | No action; cited here only so it is not confused with the PII-bearing tables above |

### F. Traveler PII

Traveler PII (name, DOB, gender, nationality, passport) was independently
reverified end-to-end by this reconciliation and by the backend audit: it is
not persisted to any database table, exists only in-memory for the duration
of a booking run, is not logged, not passed to analytics, and does not
appear in any generated document. This carries no privacy decision and no
Limited Beta classification, for the same reason as AUTH-1 above — there is
no open gate item.

One **functional, non-privacy** flag surfaced during reverification:
passport fields are collected from the traveler but are currently never
transmitted to Duffel even when populated — the provider adapter drops
`identity_documents` for the sandbox flows this build exercises. This is
relevant to whoever owns a future live-Duffel rollout requiring passport
data (silently omitted, not a leak), and is an engineering follow-up item
for that rollout, not a privacy gap for this document to gate.

### G. Third Parties

| ID | Topic | Current Technical Truth | Current Safety State | Decision Needed | Decision Owner | Eng. After Decision? | Limited Beta Classification | Failure-Safe State Today | Recommended Next Action |
|---|---|---|---|---|---|---|---|---|---|
| EXT-1 | Stripe / Duffel / Google / Resend default posture | All four fail closed to sandbox/test mode; each requires an explicit, separate kill-switch for production credentials | SAFE — strongest consistency finding across both audits | Controller/processor role and DPA status per provider (explicitly a legal determination, not assessed by either audit) | LEGAL/PRIVACY | NO | FAIL-CLOSED ACCEPTABLE PENDING DECISION | Safe | Legal confirms DPA/processor status before or during Beta as needed |
| EXT-2 | External media (Wikimedia image CDN) | Recommendation/gallery images loaded near-viewport from Wikimedia; browser IP/metadata and city-image path reach Wikimedia; `no-referrer` policy set, no analytics gate | Third-party network exposure inherent to the feature, not a code defect | Disclosure/hosting/gating policy for external image loading | MULTI-OWNER (PRODUCT, LEGAL/PRIVACY, OPERATIONS) | Possibly (e.g., proxy/self-host) if decided | FAIL-CLOSED ACCEPTABLE PENDING DECISION | Acceptable as-is (no-referrer set) | Decide disclosure approach; engineering only if hosting model changes |
| EXT-3 | Manual support diagnostic tool (ErrorState/SlowSearchNotice) | Frontend can build user-visible diagnostic copy containing full URL and search/provider context for the user to send to support; whether this reaches any Detoura-controlled backend store is not established by either audit (coverage gap, see Cross-Layer Reconciliation) | UNKNOWN until the actual delivery path is confirmed | Confirm where this diagnostic content goes, and if it lands anywhere Detoura controls, approve recipient/redaction/retention | MULTI-OWNER (PRODUCT, LEGAL/PRIVACY, OPERATIONS) | Possibly, once the delivery path is confirmed | PRE-BETA DECISION REQUIRED (to close the coverage gap before relying on this as a support workflow) | Unknown — must confirm | Engineering confirms the actual delivery mechanism first, then Product/Legal decide |
| EXT-4 | Browser history / autofill / download / clipboard retention | Outside either app's storage keys; explicitly UNKNOWN/browser-controlled per the frontend audit, which itself classifies this as NON-BLOCKER for the code audit | Outside Detoura's technical control | User-facing policy statement about browser-level retention, if any is desired | MULTI-OWNER (PRODUCT, LEGAL/PRIVACY) | NO | POST-BETA FOLLOW-UP | N/A — not Detoura-controlled | Decide only if a public-facing privacy notice needs to address it |

---

## Pre-Beta Policy Questions

Each item below is a precise, unanswered question for its owner. None are
answered here.

1. **Optional analytics / measurement** — Which optional analytics/measurement purposes, if any, are approved for the Limited Beta, and what user permission mechanism is required before those purposes may be enabled?
2. **Attribution** — Is attribution/campaign tracking (UTM capture, referrer categorization, campaign labels) an approved purpose for the Limited Beta, and if so, what retention TTL and cross-session/cross-account scope apply?
3. **Marketing purpose** — Is any marketing-communication or marketing-measurement purpose active for the Limited Beta, or does the existing `marketing` boolean seam remain unused and out of scope?
4. **Account deletion semantics** — When a user deletes their account, which of the following must additionally be anonymized, deleted, or newly time-bounded beyond current behavior: saved trips (`trip_ownership`), bookings, payments, checkout snapshots, financial documents, and communications — or is the current "anonymize identity, retain transaction history" behavior the approved contract?
5. **Communication recipient email after deletion** — After account deletion, for how long may Detoura retain the recipient email stored on historical `CustomerCommunication` records, and should it remain linked to the anonymized account/booking, be anonymized itself, or be deleted?
6. **Booking/payment retention** — What is the required or approved retention duration for `bookings`, `booking_items`, `journey_confirmations`, `payment_transactions`, `refunds`, and `payment_events`, and does that duration differ for active vs. deleted-account users?
7. **CheckoutSnapshot retention** — Independent of the existing 15-minute payment-validity window, how long may a `CheckoutSnapshot` row (containing full frozen pricing/quote detail tied to a user) be retained before deletion, and is deletion required at all?
8. **Financial-document retention** — What retention duration do accounting/tax obligations require for issued receipts, invoices, and credit notes, and does this differ by jurisdiction?
9. **Communication retention** — Independent of the account-deletion question in #5, what is the standing retention duration for `customer_communications`/`communication_attempts`/`communication_events` for accounts that are never deleted?
10. **Analytics/event retention** — Once (if) optional analytics is enabled, what retention duration applies to `analytics_events`, and must session/visitor-key-linked rows be deletable on request (see #11)?
11. **Data-export scope** — Does Detoura's Limited Beta privacy commitment require exporting bookings, payments, checkout snapshots, financial documents, communications, and tickets/provider references in addition to the currently-exported account metadata, linked identities, and booking IDs — or is the current metadata-only export sufficient for Limited Beta?
12. **Browser/shared-device planning-state retention** — Should client-side saved recommendations and journey-draft state (currently indefinite, no TTL or count cap, no shared-device-aware clearing) receive a retention limit, size cap, or shared-device protection, and is this a technical requirement or a disclosure-only requirement?
13. **External media / third-party browser requests** — Is loading destination images directly from the Wikimedia CDN (which necessarily exposes the browser's IP/metadata to Wikimedia) acceptable for Limited Beta as-is, or does it require a disclosure, a proxy, or a different hosting approach?
14. **Operational log retention** — Both audits treat structured-log retention as out of their own scope (log sink, not a database table); does Detoura need an explicit operational-log retention policy for Limited Beta, and if so, what duration?
15. **Provider-held personal data boundaries** — For each of Stripe, Duffel, Google, and Resend, what is the agreed data-processing/controller-processor relationship, and is a Data Processing Agreement required before or during Limited Beta?

Two additional questions surfaced by the reconciliation itself:

16. **Revocation completeness** — When a user revokes analytics consent, must Detoura also delete any analytics events already recorded under that session/visitor key, or is clearing the local, forward-looking state sufficient?
17. **Support-diagnostic delivery path** — Where does the diagnostic content a user can copy from `ErrorState`/`SlowSearchNotice` (full URL, search/provider context) actually go once sent, and if it reaches a Detoura-controlled destination, who is the approved recipient and what is the retention period?

---

## Analytics / Consent Decisions

Summarized from the matrix above (ANL-1 through ANL-5): analytics and
attribution are both fail-closed today (no production consent grant exists,
so no data is currently being collected under either purpose). Marketing has
a code seam but no adapter and no backend feature — effectively inert. The
backend endpoint that would receive any of this data is technically sound
(input minimization, PII-shaped-value rejection, and — since this audit's
predecessor — rate-limited), but has no retention policy and no mechanism to
delete already-recorded rows tied to a revoked session/visitor key. None of
this requires an engineering fix to remain safe during Limited Beta, because
the fail-closed default already prevents collection. It does require a
policy decision (Questions 1–3, 10, 16) before any of these purposes may be
turned on.

---

## Account Deletion Contract

Based strictly on the backend audit's Account Deletion Matrix and
Deleted-Account Security findings, the **current, truthful** contract is:

**What is erased or anonymized:**
- Account identity: email is tombstoned, password hash is cleared, status becomes `DELETED`.
- Sessions: all active sessions for the account are revoked and independently barred from re-authenticating.
- Google identity link: the linked-identity record is hard-deleted; a later Google sign-in with the same Google account creates a new, unrelated account rather than resurrecting the old one.

**What is retained, unchanged, indefinitely:**
- Saved/owned trip records (`trip_ownership`).
- All booking, journey-confirmation, and provider order/ticket records.
- All payment, refund, and checkout-snapshot records.
- All financial documents (receipts, invoices, credit notes).
- All communication records, **including the recipient email address** used to deliver them, and the booking's `lead_name`/`lead_email`.
- The original account's password-reset token row, if one happened to be outstanding (harmless — it can no longer be used, since a deleted account cannot pass the reset flow's active-status check).

**What is released for reuse:**
- The original email address becomes available for a new person to register immediately.

**Where consumer-facing wording could accidentally overstate this:** any
phrase resembling "we delete all your data," "your data is permanently
erased," or "your account and its history are removed" would misstate the
current contract — bookings, payments, financial documents, and
communications (including the recipient email) all survive deletion by
design. Accurate wording must distinguish "your account and sign-in
credentials are deleted/anonymized" from "your transaction and financial
history is retained." This distinction is a required input to future UX and
legal copy, not a copy recommendation from this document.

Neither audit found or reviewed an existing consumer-facing account-deletion
screen or its wording — if one already ships, it must be checked against
this contract before Limited Beta; if one does not yet exist, this contract
is the input for writing it once Questions 4–5 are answered.

---

## Data Export Contract

The current, truthful contract is:

**What is exported now:** account metadata (user ID, email, status,
`has_password`, created/last-login timestamps), linked auth identities
(provider, provider email, linked-at — internal `provider_subject`
deliberately excluded), and a bare list of owned booking IDs (opaque
identifiers only, no itinerary/traveler/financial content).

**What is technically available but not exported:** saved-trip/itinerary
detail, traveler-party data (though see PII-1 — none of this is actually
persisted beyond the live booking flow, so there would be nothing to export
for travelers specifically), payments, checkout snapshots, financial
documents (including PDFs), communications, and tickets/provider order
references. User-scoped, ownership-checked accessor functions already exist
for most of these in the codebase; extending export is described by the
backend audit as a wiring exercise, not new capability development.

**What is correctly out of scope:** data that lives only at Stripe or Duffel
(e.g., Stripe's stored payment-method vault objects, Duffel's live order
detail beyond the mirrored reference) — Detoura only holds references to
these, not the provider-side objects themselves.

**Required framing:** this feature must not be described to users as
"download all your data" or "complete data export" unless and until Question
11 is answered in favor of expanding its scope and the corresponding
engineering is done. Until then, the accurate label is an **account &
identity export**, not a full data-subject export.

---

## Retention Decisions

See the Unified Decision Matrix §E (RET-1 through RET-9) and Pre-Beta Policy
Questions #6–10, #12, #14 for the itemized state. The single unifying fact,
carried forward unchanged from the backend audit and not reinterpreted here:
**no retention policy is defined for any PII-bearing or financial table in
this backend**, and **no scheduler/cron/background-job infrastructure exists
anywhere in the backend today** — the only working automated retention
mechanism in the whole system governs non-PII search-intelligence data
(price observations and search traces), and even that has no PII-table
equivalent. Any decision that requires automated enforcement (rather than a
documented duration alone) will require new infrastructure that does not
exist yet — this is inherently a larger engineering item than a duration
decision alone, and should be scoped and estimated separately once policy is
set.

---

## Third-Party / External Dependencies

See Unified Decision Matrix §G. Summary: Stripe, Duffel, Google, and Resend
all default to sandbox/fail-closed, which is the strongest and most
consistent safety property found across both audits — no policy decision is
required to keep Limited Beta safe on this front, only a legal
determination of each provider's processor/DPA status (Question 15), which
does not block launching with current fail-closed defaults. The Wikimedia
image CDN dependency and the manual support-diagnostic tool are lower-
confidence items: the former is a known, low-risk third-party exposure
needing a disclosure decision (Question 13); the latter is an audit coverage
gap needing its delivery path confirmed before any retention/recipient
decision can even be framed (Question 17).

---

## Consent UI Inputs

The following are the exact inputs Product Design needs before a Consent UI
can be designed. No UI is designed here.

**Technically known (available to Design today, no policy needed):**
- The frontend already exposes `setAnalyticsConsent({analytics, marketing})` and an `init({consent})` seam; a UI can call these today.
- Revoking analytics already clears the event queue, cancels pending timers, resets both measurement identifiers, and removes attribution state.
- Analytics remains functionally inert without an explicit `true` grant; no silent restore-and-enable path exists.
- Marketing has a boolean setter but no adapter — flipping it currently does nothing observable.

**Policy decision required (not yet knowable from code):**
- Which purposes exist for Limited Beta at all (Question 1–3).
- Which of those purposes, if any, are optional vs. required for product function (none are currently required for core booking function).
- Default state for each purpose (current code default is `false`/off for both known purposes; whether that remains the launch default is a policy choice, not just a technical fact).
- Whether marketing is active at all for Limited Beta (Question 3).
- Whether analytics remains disabled at launch or is enabled with consent (Question 1).
- Whether attribution/campaign tracking is permitted, and its scope/TTL (Question 2).
- Whether preference persistence across sessions/devices is required — **not yet built**: no record/version/timestamp/expiry/source or cross-tab consent sync exists today; this is frontend engineering work gated on the policy answer, not a pre-existing capability.
- Whether users need later preference editing (a "privacy settings" surface) — not yet built; contingent on the same purposes decision.
- Withdrawal/revocation semantics, especially whether revocation must reach already-recorded backend data (Question 16) — this changes whether "revoke" needs a backend call or can remain frontend-only.
- Approved wording for purpose descriptions, consent prompts, and any privacy notice — not yet drafted; a Legal/Privacy + Product deliverable.
- Any links or notices required by policy decision (e.g., to a privacy policy page) — contingent on Legal/Privacy's decisions above.

---

## Pre-Beta Privacy Gate

### A. Decisions that MUST be made before real Beta users

- DEL-1 / Question 4 — ratify or redirect the account-deletion cascade scope.
- DEL-2 / Question 5 — communications recipient-email handling after deletion.
- EXP-1 / Question 11 — data-export scope for Limited Beta (or explicit confirmation that the current partial export, honestly labeled, is sufficient).
- EXT-3 / Question 17 — confirm the actual delivery path of the manual support-diagnostic content; this is a fact-finding requirement, not a policy question, but it must be resolved before Question about its recipient/retention can even be asked meaningfully.
- ANL-1/ANL-2/ANL-3 / Questions 1–3 — **only if** any optional analytics, attribution, or marketing purpose is intended to be live (even partially) during Limited Beta. If Limited Beta launches with analytics/attribution/marketing left in their current disabled state, these move to §D below.

### B. Engineering that MUST be completed after those decisions but before Beta

- If EXP-1 expands export scope: wire the existing user-scoped payment/financial-document/communication accessors into the export endpoint.
- If DEL-1/DEL-2 change current deletion behavior: implement the newly-approved scrub/anonymization steps.
- If ANL-1/ANL-2/ANL-3 approve any purpose for Beta: build the Consent UI (read/subscribe/persist/revoke, cross-tab sync, wording integration) — none of this exists today.
- If ANL-5/Question 16 requires revocation to reach backend data: implement a delete-by-session/visitor-key path for `analytics_events` (does not exist today).

### C. Items that can safely remain disabled/fail-closed during Beta

- Optional analytics, attribution, and marketing purposes (current default is already off; no change needed to stay safe).
- Any table-level retention automation (sessions, password-reset/OAuth pending rows, bookings, payments, checkout snapshots, financial documents, communications, analytics events) — expiry is already enforced logically where it matters for security (auth), and no unbounded-retention item here is a security exposure, only a policy-completeness gap.
- Live provider credentials for any integration not yet needed at Beta launch — all four (Stripe, Duffel, Google, Resend) already fail closed without explicit configuration.
- Single-session revocation, Google unlink, per-trip deletion — capability gaps, not exposures.

### D. Items that can be backlog after Beta

- RET-1 through RET-9 automation builds (contingent on §A decisions being made first; the automation itself is post-Beta scoped given no scheduler infrastructure exists).
- AUTH-2 (PKCE hashing hardening).
- DEL-4 (feature gaps: single-session revocation, Google unlink, per-trip deletion).
- EXT-2 (external-media disclosure) and EXT-4 (browser-level retention notice), unless Legal/Privacy elevates either.
- Any analytics/attribution/marketing purpose not approved for launch (§A) simply stays disabled and moves here.

Every item in §A and §B that touches Legal/Privacy is explicitly **their
approval to give, not this document's to grant.** Nothing above should be
read as this document deciding legal acceptability on Legal/Privacy's
behalf.

---

## Post-Beta Follow-Ups

- ANL-3 (marketing purpose), if not activated for Beta.
- AUTH-2 (Google OAuth PKCE hashing hardening).
- DEL-4 (single-session revocation, Google unlink, per-trip deletion).
- RET-9 confirmation that non-PII search-intelligence retention is not mistaken for a general data-retention policy.
- EXT-4 (browser-level history/autofill/download retention notice, if desired at all).
- Full retention-automation engineering build once durations are decided (§Pre-Beta Privacy Gate D).

---

## Recommended Execution Order

Derived from the dependency shape actually found across both audits, not
assumed in advance:

1. **Parallel policy decisions** (Legal/Privacy + Product): account-deletion cascade scope (Question 4), communications-after-deletion (Question 5), data-export scope (Question 11), retention durations (Questions 6–10, 12, 14), and — only if needed for launch — analytics/attribution/marketing purposes (Questions 1–3, 16). These are largely independent of each other and can proceed simultaneously; none blocks the others.
2. **Fact-finding**: confirm the manual support-diagnostic delivery path (Question 17) — this is evidence-gathering, not a policy call, and should happen early since it gates a downstream policy question (EXT-3).
3. **Product contract lock**: once §1 lands, finalize the truthful Account Deletion Contract and Data Export Contract wording (this document's versions are the technical input, not final copy) so Legal/UX can write consumer-facing language that does not overstate either capability.
4. **Consent/privacy UI design** — only if any analytics/attribution/marketing purpose was approved for Beta in §1; otherwise this step is skipped for launch and revisited post-Beta.
5. **Narrow engineering implementation**, scoped only to what §1's decisions actually require before Beta (see Pre-Beta Privacy Gate §B) — expected to be small (export wiring, and/or Consent UI if purposes were approved) since the technical foundation is already sound and fail-closed.
6. **Operations verification** of deployed cookie/security-header/live-provider configuration (AUTH-3, AUTH-4) — independent of the policy track above and can run in parallel with steps 1–3.
7. **Staging verification** of whatever was built in step 5.
8. **Release Gate sign-off** — Legal/Privacy confirms the policy questions in §Pre-Beta Privacy Gate §A are closed; Product confirms consumer-facing copy matches the ratified contracts; Engineering confirms step 5's implementation passed staging verification.

This order differs from a strict "decide everything, then build everything"
sequence in one respect: **operations verification (step 6) and fact-finding
(step 2) do not need to wait for the policy decisions**, since neither
depends on them — running them in parallel shortens the critical path
without changing what gates Beta launch.

---

## Explicit Non-Conclusions

This document does not, and should not be read to:

- Decide any legal basis, retention duration, or consent requirement.
- Approve any consent wording, privacy notice, or UI copy.
- Confirm deployed configuration (cookies, headers, live provider credentials) — that remains an open Operations verification task (AUTH-3, AUTH-4).
- Determine DPA, controller, or processor status for Stripe, Duffel, Google, or Resend.
- Resolve VAT or other legal invoice-retention requirements for financial documents.
- Grant Limited Beta legal or compliance approval of any kind.

## §12 — Preserving the Technical vs. Policy Distinction

The backend audit concluded: **"No blocking security or privacy defects for
Limited Beta."** This document preserves that as a **technical security**
conclusion only. It is explicitly not equivalent to, and must not be
paraphrased as:

- "Legally ready for Limited Beta"
- "Privacy compliance complete"
- "Consent requirements satisfied"
- "Retention policy in place"

The correct combined statement is: **the technical foundation contains no
known defect that would make Limited Beta unsafe to launch with its current
fail-closed defaults, while a substantial set of policy decisions (this
document's Pre-Beta Policy Questions) remain open and are not, and cannot
be, resolved by engineering work alone.**
