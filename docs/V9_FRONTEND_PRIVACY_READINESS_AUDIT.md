# V9 Frontend Privacy Readiness Audit

## 1. Executive Status

Starting checkpoint: `c23cd22395d14075e51991453d55be3a6de51aa5` (`V9 connect consumer password recovery`). Current source was audited; old interrupted edits were not replayed. Analytics `3606f55`, Google `cca5fe2` and password recovery `c23cd22` remain the functional baseline. Only concrete frontend defects listed below were fixed. This audit made no backend changes, provider mutations or push. Concurrent backend work is excluded from its commit.

**Analytics Infrastructure: READY** for the existing fail-closed foundation. **Production Measurement: DISABLED. Consent UI: NOT BUILT. Consent Policy: NOT APPROVED** by this audit; no approved policy was supplied. No remaining demonstrated Critical/High/Medium policy-independent frontend defect after fixes and review. This is not legal or deployment approval.

Evidence: complete source/storage searches, actual instrumentation call-site review, transpiled-source runtime harnesses with mocked browser/API dependencies, build and lint. Not live multi-tab, deployed-header, real OAuth/email/provider end-to-end verification. Read-only backend configuration observations do not establish deployed settings.

## 2. Browser Storage Inventory

Technical purpose labels do not establish a legal basis. Server truth must survive deletion/tampering of client storage.

| Exact key / location | Purpose / data | Gate and sensitive content | Authority / clear / account behavior |
|---|---|---|---|
| `detoura-theme` localStorage | Light/dark preference | No analytics gate; interface preference; no PII/ID | No authority. Toggle overwrites; browser clear. Retained at logout/switch. |
| `detoura-saved` localStorage | Whole recommendation plus saved_at, saved_price, travelers; routes/dates/airports/stays/operators | No analytics gate; planning. Personal travel preferences; trip/selection IDs, no actual traveler identity/password/ticket/payment records in current type | Estimate/selection handle, not booked truth. Unsave/browser clear; retained at logout/switch. |
| `detoura-journey-draft-v1` localStorage | Version, selectedAt, whole recommendation, search context travelers/origin/dateFrom/dateTo | No analytics gate; resume planning. Travel preferences and trip/selection IDs | Not authoritative booking/payment. Explicit reset/invalid data/version removal/browser clear; retained at logout/switch. |
| `detoura.attribution.v1` sessionStorage | Five bounded UTM labels, internal/external referrer category, public route template, timestamp | Analytics permission required. Labels may carry identifying semantics despite filtering; no raw URL/full referrer/account ID | No authority. Deny/revoke/default-denied startup removes; invalid/expired read removes. No logout-specific action. |
| `detoura.fk.s` sessionStorage | Random 24-hex measurement session ID | Permitted first-party mapped event only; pseudonymous ID, not auth credential | No authority. Deny/revoke/startup clears; otherwise tab lifetime. No logout-specific action. |
| `detoura.fk.v` localStorage | Random 24-hex visitor ID | Permitted first-party mapped event only; pseudonymous ID | No authority. Deny/revoke/startup clears; no logout-specific action. |
| `detoura.ops.session` legacy sessionStorage | Previously persisted Ops bearer credential | Privileged credential: technical defect fixed | Now removed by normal startup and Ops module/setToken; no new writes. Token stays in page memory, clears on logout/401/unload. |
| `detoura_csrf` cookie (default name) | JS reads for X-CSRF-Token on mutations | Essential request integrity, not analytics gated; security token | Server cookie lifecycle. Not copied to web storage/instrumentation. |
| `detoura_session` cookie (default name) | HttpOnly server session credential sent by browser | Essential authentication, not analytics gated; account-linked credential | Server validates authority; JS cannot read. Server expiry/logout/reset invalidation. |

Sources under `frontend/src`: `components/shell/Header.tsx`, `state/useSaved.ts`, `state/journeyDraft.ts`, `lib/analytics.ts`, `ops/opsApi.ts`, `main.tsx`, `api/client.ts`. No IndexedDB, Cache API store, service worker persistence, other persistent application store or JS cookie writer found.

URL/history: known Google return markers `google_auth`, `google_link_required`, `link_id`, `reason` are parsed into bounded state; recognized return fields are removed using `history.replaceState`. Other query/hash remains. This is not a universal URL scrubber. UTM values can remain in browser history even when optional analytics is denied. OAuth code/state goes to the backend callback URL, not consumer web storage. Browser history/autofill/cache/clipboard/download retention is outside these app keys and UNKNOWN here.

| Relevant memory | Content / lifetime / authority |
|---|---|
| App/search/comparison/draft/saved rechecks | Full search criteria, recommendations/provider issues may survive SPA navigation. Reset/replacement/reload clears; planning remains across logout (policy-dependent). Selection handles revalidated server-side. |
| BookingExperience | Traveler names/contact/DOB/passport, promo input, booking/payment/pass responses and idempotency handles; memory only. Unmount or known account/trip change remounts. Server semantic states establish outcomes. |
| MyTrips | Account list/detail/confirmation/document metadata; memory only. Hidden/cleared outside authenticated status, identity-keyed and unmounted on navigation. Aborted completions ignored. |
| Login | Email/password/confirmation/reset code in React/request memory. Secrets clear on mode switch, successful submission, logout, account change or unmount. Failed submissions retain editable input; no forensic zeroization/autofill claim. |
| Account/Google | Backend profile/status; Google link ticket/notices in memory. Link ticket clears on consume/logout/known identity change/unload; backend owns expiry. Callback hint does not authenticate. |
| Analytics | Purpose booleans, queue/timer, local-only dedupe state. Revocation clears optional pending state. |
| Ops | Bearer and returned operational/customer views in memory; sign-out unmounts views. No durable API cache found. Refresh now requires sign-in. |
| Downloads | Temporary Blob/object URL revoked after download initiation; downloaded file remains under browser/user/OS control. |

## 3. Client Retention Matrix

No approved policy was supplied. Existing constants describe behavior, not newly approved durations.

| Category | Current classification / duration | Policy |
|---|---|---|
| Theme | INDEFINITE; EXPLICIT CLEAR/overwrite | NO POLICY DEFINED |
| Saved recommendations | INDEFINITE; EXPLICIT CLEAR per item; no TTL/count cap | NO POLICY DEFINED |
| Journey draft | INDEFINITE; EXPLICIT CLEAR/invalid-data removal; timestamp is not expiry | NO POLICY DEFINED |
| Attribution | SESSION plus existing 30-day TTL checked on read; PERMISSION REVOCATION CLEAR | NO POLICY DEFINED |
| Funnel session ID | SESSION; PERMISSION REVOCATION CLEAR | NO POLICY DEFINED |
| Visitor ID | INDEFINITE while permitted; PERMISSION REVOCATION CLEAR | NO POLICY DEFINED |
| Ops credential | Page-memory SESSION; LOGOUT CLEAR/401/unload; legacy key startup purge | Backend expiry authoritative |
| Auth/CSRF cookies | Server TTL; configured default 14 days; LOGOUT CLEAR/reset invalidation | Deployed values/policy need verification |
| Account/traveler/reset memory | Component/page SESSION and account-boundary clearing | Not durable; no forensic-erasure claim |
| History/autofill/cache/downloads/clipboard | UNKNOWN browser/device/server retention | NO POLICY DEFINED in frontend |

Whole recommendation snapshots are persisted rather than a minimal field allowlist; future schema additions require privacy review. No duration or logout policy was invented for saved/draft preferences.

## 4. Analytics Purpose State

`lib/analytics.ts` defaults to `{analytics:false, marketing:false}`. ESSENTIAL is an adapter purpose, not a persisted choice; its only adapter is development-only sanitized console diagnostics. MARKETING has a boolean seam but no adapter. These names are not legal determinations.

Only literal true grants permission. Revoke empties queue, cancels timer, resets identifiers/dedupe, removes attribution and both measurement keys. Track checks permission and sanitizes runtime input; flush rechecks permission before beacon/fetch for timer, pagehide and visibility delivery. Threshold: 12 events; timeout: 2.5 seconds; serialized batch cap: 50. Already transmitted requests cannot be recalled. Storage denial/adapter failure does not interrupt product behavior.

## 5. Production Analytics State

`main.tsx` initializes without granting consent; no consumer grant caller found. `VITE_ANALYTICS_FIRST_PARTY=true` only installs the adapter. Delivery to configured API base plus `/events` (normally `/api/v1/events`) requires both flag and explicit permission. Production optional measurement remains DISABLED; there is no preference restore silently enabling it. Future setter integration requires separate review. No vendor tracker/CMP/UI added. Existing verification exercises denied production and analytics/storage failure isolation.

## 6. Attribution

Permission gates capture/persistence/read/delivery. Query parsing caps at 4096 characters, stored reads at 2048; five labels cap at 80 characters with restrictive syntax. Read validates age, rejecting future/expired/invalid records. Referrer becomes internal/external; landing routes become public templates. No raw URL/referrer storage in this module. A valid campaign label can still identify someone; filtering is not anonymization. Existing TTL, campaign governance and cross-account scope need policy approval. Logout is not purpose revocation.

## 7. Future Consent UI Technical Contract

| Operation | Classification / seam |
|---|---|
| Read current purposes reactively | FRONTEND IMPLEMENTATION NEEDED: no public getter/subscription/React binding |
| Grant analytics | TECHNICALLY AVAILABLE NOW: `setAnalyticsConsent({analytics:true})`, or initial `init({consent})` |
| Revoke analytics | TECHNICALLY AVAILABLE NOW: `setAnalyticsConsent({analytics:false})` |
| Clear attribution/pending events | TECHNICALLY AVAILABLE NOW through revoke; independent public operations need implementation |
| Marketing | TECHNICALLY AVAILABLE NOW as boolean setter only; use/adapter/taxonomy need implementation and approval |
| Persist/restore preferences | FRONTEND IMPLEMENTATION NEEDED: no record/version/timestamp/expiry/source or cross-tab consent sync |
| Legal basis, purpose classification, wording, retention/withdrawal semantics | POLICY / LEGAL DECISION REQUIRED |
| Accessible UI and consent evidence/integration tests | FRONTEND IMPLEMENTATION NEEDED after approved contract |

No consent UI, CMP, wording, persistence or marketing policy implemented. Essential product behavior must remain independent of measurement.

## 8. Auth / Account Privacy

Signup/login credentials go to intended account API; `/me` establishes frontend account status. No consumer bearer in web storage. CSRF is used for request integrity only. Source sweep found no password/reset/auth request in analytics, error capture, console or durable application storage. Transient form/request memory is unavoidable.

Fixed stale `/me` overwriting later login/logout via generation/abort checks. BroadcastChannel messages contain only `session-changed`; focus/pageshow revalidate as fallback. Revalidation during auth mutation is deferred and then executed. Failed login settles state. A transport failure retains the identity boundary but status becomes error, not authenticated; MyTrips stays gated.

No continuous expiry polling added. Unsignaled server expiry/revocation is learned on revalidation/API use. Instant erasure from every idle/offline tab is not guaranteed. Server validation/cache headers remain dependencies.

## 9. Google OAuth Browser Safety

Explicit user interaction starts backend OAuth navigation; no Google browser SDK/token storage. Backend owns code/state/nonce/token validation. Consumer parser accepts bounded known outcomes and removes recognized return fields. Link ticket stays in memory, is explicitly submitted after password login, and backend checks expiry (configured default 10 minutes). Same-email collision does not silently link.

Authenticated branch now displays safe linking failure. Ticket/status hints clear on known account change/logout. Initial recognized return remains usable. Live provider config, callback/log redaction and backend state/nonce enforcement remain outside this frontend verification. Arbitrary query/hash is not universally scrubbed.

## 10. Password Recovery Browser Safety

Flow remains a one-time CODE entered with new password, not a clickable reset URL. Code/password stays in component/request memory, not storage/URL/analytics/instrumentation/logs. Successful reset immediately clears code/password/confirmation; mode changes clear secrets. Backend owns single use/expiry, enumeration resistance, hashing, session invalidation and email delivery. Existing script and added component-handler checks pass; no real email sent.

## 11. Error Instrumentation

Actual `captureException` sites were inspected. Search passes explicit operation-only safe context, not full request. `lib/errorTracking.ts` reports classified bounded development diagnostics, not raw exception/provider text. No production tracker adapter initializes.

Separate manual support diagnostics are broader: ErrorState/SlowSearchNotice can build user-visible copy/email diagnostics with full URL and search request/provider context. This is user-mediated support output, not automatic error telemetry, and must not be described as universally sanitized. Recipient/minimization/retention needs policy review. Auth/password/reset inputs are not wired to those search-support sites.

## 12. Analytics Payload Review

Inspected actual track calls in App, search, BookingExperience and MyTrips plus runtime sanitizer/transport mapping. Event-schema payloads use bounded categories/counts/currency/tier/state. No name/email/phone/DOB/passport, free text, promo value, business/provider/OAuth/auth ID, reset/password, raw URL/error/request/response or exact price is emitted by these event-schema call sites. Attribution is separately qualified in section 6. Dedupe keys may contain business IDs but are local-only. Transport deliberately includes pseudonymous measurement session_key/visitor_key: analytics is not identifier-free.

Request exceptions remain non-terminal request/confirmation errors. Backend FAILED/CANCELLED payment states remain distinct from UNKNOWN/RECONCILIATION_REQUIRED; booking failed remains distinct from recovery_required/confirmed. Authorization is not booking; booking is not capture. Current legacy mapping does not deliver every typed event; this audit does not claim complete production funnel measurement. Closed analytics files unchanged.

## 13. Logout / Account Switch

| State | Classification / behavior |
|---|---|
| Profile/account-owned booking/traveler/MyTrips | MUST CLEAR on known identity change; ALREADY CLEARS through generation checks, identity keys and abort guards. Expiry-detection caveat in section 8. |
| Login secrets/errors and Google ticket/hints | MUST CLEAR; ALREADY CLEARS on successful operations/mode changes/unmount and known identity boundary as applicable. Failed logout remains visible and does not claim success. |
| Theme | SAFE CROSS-ACCOUNT technical preference; policy owns persistence |
| Saved/draft/search/comparison | POLICY-DEPENDENT browser-wide travel preferences, retained; not account-owned booking authority |
| Analytics attribution/IDs | POLICY-DEPENDENT if enabled; logout does not revoke. Denied startup/revoke clears. No consumer grant today. |
| History/autofill/downloads | POLICY-DEPENDENT/browser-controlled; not app-purged at logout |

## 14. Third-Party Frontend Inventory

| Integration | Initialization / transmitted data / gate / status |
|---|---|
| Wikimedia image CDN | Recommendation/gallery image near viewport; IP/browser metadata and city-image path. no-referrer policy, no analytics gate. Active content, not evidence of tracker. |
| Wikimedia/Creative Commons credit links | User click, noreferrer; navigation metadata reaches destination; no analytics gate |
| Google OAuth | Explicit click through first-party backend; OAuth redirect protocol data. No browser SDK/analytics gate/frontend token persistence. Live config unverified. |
| Stripe/Duffel | Active booking calls Detoura API; no active direct browser SDK/order integration found. Backend provider dependency. Unused legacy Checkout prototype is not App booking authority. |
| Analytics/error vendors | None initialized; development diagnostics and gated first-party seam only |
| Maps | Local SVG, no third-party map request |
| Fonts/hero | Manrope bundled CSS and first-party /media/hero; no runtime Google Fonts request. Provenance URLs alone do not trigger network. |
| React/Motion/Vite | UI/build dependencies, no discovered tracker initialization |

No hidden external script/iframe tracker found; SEO script is JSON-LD. Source/dependency inventory is not deployed network capture or supply-chain certification.

## 15. Technical Defects

All category A, owner FRONTEND. Severity refers to starting checkpoint; all fixed.

| ID | Severity | Defect / fix |
|---|---|---|
| A1 | High | Ops sessionStorage bearer moved to page memory; legacy purge and 401 clear. Reload requires sign-in; same-origin script can still access memory. |
| A2 | Medium | Stale restoration could overwrite newer auth; generation/abort guards, deferred revalidation and failed-login settlement. |
| A3 | Medium | Stale cross-tab/account views; nonsecret notifications/focus/pageshow refresh and account-keyed BookingExperience/MyTrips/Login. No offline instant-expiry claim. |
| A4 | Medium | Successful password/reset inputs retained; clear immediately. No forensic zeroization claim. |
| A5 | Medium | Authenticated branch hid link/logout failure; render existing safe messages. |
| A6 | Medium | Aborted MyTrips list response could restore stale data; post-response abort guard. Detail already guarded. |
| A7 | Medium | Google ticket/hints survived known account transition; clear on identity change/logout and ignore stale link completion. |

No backend, analytics architecture, retention duration, consent UI/vendor or legal-copy changes.

## 16. Policy-Dependent Gaps

All category B; not closed by technical success.

| ID | Gap | Classification | Owner |
|---|---|---|---|
| B1 | Purpose/legal basis/wording/evidence and preference version/expiry contract absent; UI read/subscription/persistence work remains | CONDITIONAL BLOCKER before measurement activation | PRODUCT, LEGAL/PRIVACY, FRONTEND |
| B2 | Saved/draft/theme retention, snapshot minimization and shared-device visibility: NO POLICY DEFINED | CONDITIONAL BLOCKER for launch privacy decision | PRODUCT, LEGAL/PRIVACY, FRONTEND |
| B3 | Attribution TTL, indefinite visitor ID/cross-account scope and campaign governance | CONDITIONAL BLOCKER before measurement activation | PRODUCT, LEGAL/PRIVACY |
| B4 | External-media disclosures/hosting/gating policy unresolved | CONDITIONAL BLOCKER for launch privacy decision | PRODUCT, LEGAL/PRIVACY, OPERATIONS |
| B5 | Manual support full URL/request/provider data: approved recipient/redaction/retention absent | CONDITIONAL BLOCKER before relying on support workflow | PRODUCT, LEGAL/PRIVACY, OPERATIONS |
| B6 | Browser history/autofill/download retention user-facing policy | NON-BLOCKER for code audit; decision input | PRODUCT, LEGAL/PRIVACY |

## 17. Backend / Operations Dependencies

All category C; no backend files edited.

| ID | Dependency | Classification | Owner |
|---|---|---|---|
| C1 | Deployed Secure/HttpOnly/SameSite attributes, cookie-name alignment, expiry/revocation, no-store/security headers/cache | CONDITIONAL BLOCKER for deployed assurance | BACKEND, OPERATIONS |
| C2 | OAuth callback/log redaction, ticket/token expiry/storage, state/nonce validation, redirects/provider config | CONDITIONAL BLOCKER for live Google assurance | BACKEND, OPERATIONS, EXTERNAL PROVIDER |
| C3 | Reset-code expiry/single-use, enumeration protection, invalidation, real email delivery/retention | CONDITIONAL BLOCKER for live recovery assurance | BACKEND, OPERATIONS, EXTERNAL PROVIDER |
| C4 | Deletion/export, server retention, financial-document requirements, support/event-log access | CONDITIONAL BLOCKER for privacy launch decision | BACKEND, OPERATIONS, LEGAL/PRIVACY |
| C5 | Live multi-tab/expiry/shared-device and deployed network checks beyond mocked harnesses | CONDITIONAL BLOCKER for those end-to-end assurance claims | FRONTEND, BACKEND, OPERATIONS |

## 18. Limited Beta Frontend Blockers

No remaining demonstrated category-A Critical/High/Medium frontend code defect after review. Optional analytics must stay disabled; B1/B3 block any proposed measurement activation. Other conditional gates retain their listed owners; frontend cannot declare legal/deployment acceptability. Consent UI NOT BUILT / Consent Policy NOT APPROVED remain explicit gaps, not reasons to weaken permissions.

Receipt: starting HEAD c23cd22 plus this eight-path audit change (App, main, opsApi, Login, MyTrips, useAccount, verify-privacy script and report), committed together. Build passes; lint passes with seven existing warnings, zero errors. Existing verify-analytics, verify-google-signin, verify-password-recovery and new verify-privacy scripts pass. No package test script exists; these focused executable checks are the test receipt.

Separate read-only auth/privacy and telemetry reviewers inspected secrets, token persistence, instrumentation, purpose/revoke, account boundaries, third parties and report claims. Review exposed additional auth edge cases (failed login during restoration, revalidation during mutation and late Google-link completion after logout); fixed and runtime-tested. Final review reported no remaining Critical/High/Medium defect. Verification uses mocks/source assertions, not live providers or full browser end-to-end coverage.

## 19. Recommended Inputs for Privacy / Legal Review

Provide storage/retention tables, event schema/mapping, measurement-ID design, campaign governance, shared-device planning behavior, media destinations and a manual support diagnostic example. Obtain approved purpose/wording/retention/withdrawal/evidence and external-integration decisions. Obtain backend/operations receipts for cookies/headers, logs, account lifecycle, email/OAuth, deletion/export and server retention. Then scope the future UI against section 7 and independently validate before activating measurement. These are technical inputs, not legal advice or legal-acceptability conclusions.
