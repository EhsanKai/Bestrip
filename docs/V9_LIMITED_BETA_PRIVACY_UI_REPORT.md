# V9 Limited Beta Privacy UI Report

## Executive Summary

**IMPLEMENTED and VERIFIED within the frontend scope.** Public bilingual privacy UI, truthful account deletion/export controls, a fail-closed legal configuration/release check, and a non-sliding 30-day journey draft lifetime are complete. Independent review found a pre-existing late financial-document download across account departure; this frontend defect is fixed and regression-tested. No remaining demonstrated Critical, High or Medium frontend defect in this slice.

**Production Legal Readiness: LEGAL GATE.** Required legal metadata remains empty and companion-traveller approval remains false. The production page withholds the unfinished notice, while development displays an explicit draft. This report does not grant legal or deployment approval.

Backend changes: NONE. Provider objects created: NONE. Cookie banner and consent center: NOT BUILT. Optional analytics, attribution, marketing tracking and marketing email: DISABLED. Push: NO.

## Starting Baseline

Original and recovered starting HEAD: `44eee78460e39fcc0ffa63a860c5c6ad0ce4574d` (`44eee78`, “V9 record privacy policy implementation checkpoint commit hash”). Seven required privacy/retention/DSAR/provider artifacts were read before implementation. The later continuation request arrived after implementation, tests, local browser checks and independent review; recovery confirmed HEAD was still `44eee78`, no staged changes or task commit existed, and only reporting/checkpointing remained. Correct completed work was preserved.

Pre-existing dirty paths: untracked `AGENTS.md` and `CLAUDE.md`; both untouched and excluded. No backend tests or backend code were modified. The previously reported backend full-suite order-pollution failure was not investigated or changed.

Architecture: `main.tsx` chooses consumer versus Ops roots by pathname; `App.tsx` holds consumer screen state; `Landing.tsx` owns the public footer; `Login.tsx` contains authentication and the signed-in account view. There was no consumer deletion/export control before this slice. No global localization system was found. Deprecated Checkout was not revived.

## Privacy Route

**IMPLEMENTED / VERIFIED:** `/privacy` is a public root selected before the account-owning App mounts. `/privacy/` also resolves with `/privacy` as canonical. Direct navigation and refresh were exercised in the local browser. No sign-in or account API response is required. Existing Vite SPA behavior and Netlify/Vercel rewrites are retained. The page uses existing typography, tokens and BrandLogo; no design system or marketing page was introduced.

## English/German Content

**IMPLEMENTED / VERIFIED:** `frontend/src/privacy/noticeContent.ts` contains 20 paired, structured sections in English and German. The independent reviewer checked semantic parity and found no invented legal basis or obligations in one language. The native, labelled English/Deutsch selector does not use flags or analytics storage. `?lang=de` preserves selection through refresh and allows direct linking; canonical stays `/privacy`. The document and main content language update together.

Content covers account/auth, optional Google sign-in, planning, traveller information, payments, suppliers, service email, financial documents, security/logs, disabled measurement, recipients, transfers, differentiated retention, deletion, partial export, rights, authority and changes. Existing shared-browser saved recommendations are distinguished from the newly capped journey draft. No blanket financial-retention duration or final companion-traveller legal basis is asserted.

## Legal Metadata Configuration

**IMPLEMENTED; values remain a LEGAL GATE:** `frontend/src/privacy/legalConfig.ts` is the single public legal-metadata source for:

- `LEGAL_ENTITY_NAME`
- `REGISTERED_ADDRESS`
- `PRIVACY_CONTACT_EMAIL`
- `PRIVACY_NOTICE_EFFECTIVE_DATE`
- `COMPETENT_SUPERVISORY_AUTHORITY_NAME`
- `COMPETENT_SUPERVISORY_AUTHORITY_URL`
- `COMPANION_TRAVELLER_LEGAL_NOTICE_APPROVED` (currently **false**)

No company, address, privacy email, authority or effective date was invented. Validation rejects missing/draft values and invalid contact/date/HTTPS URL formats. Syntactic validity is not evidence of legal authenticity; Legal must supply and approve the actual values and wording.

## Production Fail-Closed Behavior

**IMPLEMENTED / VERIFIED:** `node frontend/scripts/verify-legal-readiness.mjs` is dependency-free and exits **1** for the current unconfigured state, explicitly reporting the companion-traveller gate. `npm run verify:legal` exposes it; `npm run build:release` runs it before the normal build. Netlify and Vercel frontend build commands now use `build:release`.

A normal `npm run build` remains available for engineering validation. Even in that production bundle, an unready notice renders only a neutral unavailable message, no draft metadata/content, and `noindex,nofollow`. The actual production preview was checked in the browser: zero notice sections and the German unavailable message. Development alone shows the complete draft with an explicit development warning.

**OPS GATE:** root Dockerfile and custom/manual deployments remain unchanged and still can run the ordinary build. Their release process must explicitly invoke `verify:legal`/`build:release`. This slice does not claim every deployment path is automatically blocked. Verification scripts use Node 22.18+ native TypeScript support; local validation used Node 22.23.2. A bare engineering build is not production legal approval.

## Footer / Public Navigation

**IMPLEMENTED / VERIFIED:** public landing footer has a direct Privacy link in labelled legal navigation. No approved Imprint/Legal Notice or Terms document was found or fabricated. Their content and navigation remain a **LEGAL GATE**. Browser accessibility inspection confirmed the footer link.

## Authentication Privacy Access

**IMPLEMENTED / VERIFIED:** Privacy Notice is reachable in login, signup, recovery and signed-in account views. Existing Google button, password forms, recovery modes and backend-owned authentication behavior are preserved. No mandatory privacy checkbox or Google consent checkbox was added. Local browser checked Google-button presence and navigation into password recovery; mocked runtime scripts verify callback parsing, methods, payloads, secret cleanup and session restoration.

**PARTIAL:** real Google OAuth and real email/reset delivery were not exercised. The local app had no working account backend; it correctly displayed a session-check failure rather than pretending to authenticate.

## Account Deletion UX

**IMPLEMENTED / VERIFIED:** the signed-in account screen now connects the existing deletion endpoint using the approved title “Delete your account?”, approved account/sign-in and retained-record explanation, “Delete account”, “Cancel”, and a Privacy Notice link.

The backend protocol is unchanged: POST `/auth/account/delete`, cookie credentials and existing CSRF header, optional `current_password` in JSON. Accounts with a password must supply it; Google-only accounts can leave it blank. The server makes that determination. No extra consent or typed confirmation phrase was introduced. A successful response clears the account through the existing generation/broadcast boundary. Failed deletion does not falsely clear the account. Password input is memory-only and clears on cancel/failure/unmount.

Native dialog semantics provide an accessible name/description and modal keyboard behavior. Local browser verified opening, focus inside the dialog, Escape and Cancel, and focus returning to the trigger. No real account was deleted during verification.

## Account Export UX

**IMPLEMENTED / VERIFIED:** “Export account data” downloads the existing authenticated partial JSON response. Supporting copy states the limited scope and directs broader requests to the Privacy Notice. A valid configured privacy email is reused rather than invented. Export requests use `cache: no-store`; temporary Blob URLs are revoked; no export is persisted in web storage. Abort/unmount guards suppress late account-export completion.

Automated export remains **PARTIAL** (account metadata, linked identities, booking IDs), as documented by the backend. No backend scope expansion and no complete-GDPR-export claim. Downloads already saved by a user remain browser/OS-managed.

## Journey Draft 30-Day TTL

**IMPLEMENTED / VERIFIED:** existing key `detoura-journey-draft-v1` and version 1 are preserved. New drafts record fixed numeric `createdAt` and `expiresAt`, with a maximum interval of 2,592,000,000 milliseconds. `selectedAt` must agree with creation. Restore rejects missing, malformed, non-integer, future or overlong metadata and rejects at the exact expiry boundary. Saving an expired draft also discards it.

Load/restore never writes or renews timestamps. Rendering does not persist a generation. Only existing explicit selection/reoptimization semantics create a new generation. Search still does not clear a valid Your Journey selection. A bounded timer and focus check expire state in long-lived tabs; timer chunks respect the browser's 32-bit timeout ceiling. Expiry cleanup does not delete another tab's newer valid stored draft.

Browsers cannot execute cleanup while closed or suspended. Expired records are never restored when the app next runs; physical removal occurs on that access or active timer. This is an application TTL, not forensic deletion or protection against a device owner manipulating their clock/storage.

## Legacy Draft Migration

**IMPLEMENTED / VERIFIED:** old indefinite version-1 drafts without both retention fields are discarded. An old `selectedAt` alone does not earn a fresh lifetime. Corrupt JSON and denied storage fail safely. Tests include exact expiry, repeated restores with zero writes, future dates, missing fields, version mismatch, excessive lifetime and explicit new generation creation.

## Browser Storage Audit

**VERIFIED for the changed scope:** no new sensitive browser storage. Journey storage still contains planning recommendation/search context plus retention metadata, not traveller identity, booking/payment status or financial documents. Existing recommendation snapshots are not a universal field allowlist; future schema expansion still requires privacy review.

Theme and saved recommendations retain their previous policy-dependent behavior; they are not account-owned booking truth and are not indiscriminately cleared on logout. The inactive attribution/measurement stores remain fail-closed. Existing legacy Ops-token purge remains. Passwords, reset codes, Google tokens, CSRF material, DOB/passport data, provider secrets and account exports were not introduced into localStorage/sessionStorage.

Account generation checks, identity-keyed booking/My Trips/Login remounts, and session-change notifications remain. One previously unguarded document download completion was fixed: all in-flight PDF controllers abort on account status change/unmount, and late responses/errors are ignored even if transport resolves after abort. No old-account Blob is emitted in the adversarial test.

## Booking / Traveller Fields

**VERIFIED by actual-flow source inspection and existing mocked booking-handler tests:** BookingExperience remains unchanged. Current fields are names, DOB, email, phone and optional nationality/passport-document details for the test flow. No health, disability, religious diet or other special-category field was found or added. No companion consent checkbox exists. No supplier-required field was removed.

**LEGAL GATE:** companion-traveller Article 6 basis and Article 14 handling remain unsigned. **PARTIAL:** no live supplier booking/payment was performed. The provider register's existing passport-forwarding/live-rollout caveat remains external to this frontend slice.

## Analytics / Attribution / Marketing

**VERIFIED:** analytics/marketing defaults and lack of production consent grants remain unchanged. No attribution, marketing measurement, tracking or email activation was added. Privacy has no measurement initialization or account hook; existing application startup remains denied. Existing analytics script executes denied-default, revocation, queue/storage and actual booking outcome handlers. No vendor tracker was introduced.

## Cookie / Consent UI

**VERIFIED:** cookie banner, CMP, consent modal/center, Accept All/Reject All and analytics/marketing toggles are NOT BUILT. Existing essential session/request-integrity cookies remain server-owned. No fake preference is offered for disabled processing.

## External Media

**VERIFIED source inventory; PARTIAL deployed network verification:** hero video/posters, landing inspiration, account background and fonts already use local assets; Privacy adds no remote images or fonts. Recommendation/gallery destination photos still use `thumb.wikimedia.org` with `no-referrer`; these decorative requests expose IP/browser metadata to the host independently of analytics. Wikimedia/Creative Commons credit links navigate externally on click. Supplier imagery was not changed. No broad asset migration was undertaken.

Both notice languages disclose Wikimedia requests. Hosting/disclosure approval remains a **LEGAL GATE**. Local page rendering was observed, but no complete production network trace or supplier-flow traffic capture is claimed.

## Accessibility

**VERIFIED locally:** Privacy has one main landmark, one H1 and 20 H2 sections, descriptive legal links, a labelled native language select, document language updates and visible focus using existing tokens. English-to-German switching was exercised by keyboard (Space, Down, Return). Native deletion dialog opening, Escape/Cancel and trigger focus restoration were checked using an isolated temporary synthetic UI harness, with no real account mutation. The harness was removed before staging.

**PARTIAL:** no formal screen-reader certification or exhaustive contrast audit across every existing theme/screen.

## Responsive Verification

**VERIFIED locally:** notice inspected at desktop 1440×900 and mobile 390×844; readable line length, wrapped German text and no observed horizontal overflow. Deletion dialog at 390×844 fit with readable controls and copy. Temporary viewport override was reset. Exhaustive device coverage remains **PARTIAL**.

## SEO

**IMPLEMENTED / VERIFIED:** meaningful translated title and description, canonical `/privacy` through the existing site-URL helper, noindex while unready. No private/transient route was added to the sitemap. Existing sitemap allowlist remains home-only: the currently unapproved privacy page is deliberately omitted. Existing SPA metadata is client-applied after load; no prerendering redesign was introduced. `VITE_PUBLIC_SITE_URL` remains an Ops configuration dependency for absolute deployment URLs; local fallback canonical was `/privacy`.

## Build / Lint

**VERIFIED:** `npm run build --prefix frontend` succeeds (TypeScript plus Vite). `npm run lint --prefix frontend` succeeds with seven existing warnings: entry-point lazy Ops component, SearchProgress set-state effect, Icon mixed exports, three opsFormat mixed exports, and unused Ops catch parameter. No new unresolved lint warning. No dependencies or lockfile changes. Touched TS/TSX files are below 500 lines; My Trips presentation helpers were extracted unchanged, independently compared to baseline.

## Verification Scripts

All four existing verification scripts ran successfully before implementation. After final code changes, these commands passed:

| Command from repository root | Result / evidence |
|---|---|
| `node frontend/scripts/verify-analytics.mjs` | VERIFIED: denied defaults, consent revocation, sanitizer/storage failure isolation, actual checkout handlers with mocked outcomes |
| `node frontend/scripts/verify-google-signin.mjs` | VERIFIED: URL parsing, linking payload/CSRF, no token persistence or frontend OAuth exchange |
| `node frontend/scripts/verify-password-recovery.mjs` | VERIFIED: reset request/confirm protocol, generic copy, duplicate guard, no durable secrets |
| `node frontend/scripts/verify-privacy.mjs` | VERIFIED: session races, account switches, cleanup, deletion success/failure races, active versus departed PDF completion |
| `node frontend/scripts/verify-privacy-ui.mjs` | VERIFIED: dependency-free source/config/TTL tests, real client methods under fake transport, partial export and document failure behavior |
| `npm run verify:legal --prefix frontend` | LEGAL GATE: expected exit 1, all six metadata fields and false companion approval reported |

The new script needs only Node built-ins/native type stripping; Node emits an experimental API warning for `stripTypeScriptTypes`. Existing scripts continue using the repository's installed TypeScript compiler. Tests do not create provider objects or send real email. Assertions based on source structure are not represented as browser end-to-end evidence.

## Browser Verification

**VERIFIED for local UI; PARTIAL for integrated/live flows.** Actual Codex in-app browser used against Vite development and production-preview servers. Checked direct `/privacy`, German selection/refresh, title/canonical/robots, one main/H1, 20 draft sections, production unavailable state with zero sections, mobile/desktop layout, keyboard select, footer, Login/Google-button presence, recovery navigation, anonymous My Trips, and isolated deletion dialog keyboard/focus behavior.

No production deployment, real Google round trip, real reset email, real authenticated account deletion/export, real PDF retrieval, or live booking/payment was performed. Those flows have source/mocked runtime regression coverage as distinguished above.

## Independent Review

**VERIFIED:** separate named read-only reviewer examined tracked and added files after implementation and normal verification, including production placeholders, invented metadata, deletion/export overclaims, EN/DE parity, companion consent/legal basis, special categories, CMP/tracking, TTL renewal/legacy behavior, sensitive storage, authentication, account boundaries and backend scope.

One demonstrated Medium was found: an existing PDF request could trigger a download after the owning account's UI unmounted. Fixed with abort plus completion guards. Reviewer independently ran the expanded privacy script and confirmed the fix; extracted presentation function bodies matched baseline. Final review verdict: **PASS for frontend engineering**, no remaining demonstrated Critical/High/Medium defects. Docker/manual release-gate limitation was explicitly retained.

## Remaining Legal Gates

- All six verified controller/contact/date/authority metadata values.
- Companion-traveller Article 6 / Article 14 wording and approval (flag remains false).
- Final legal approval of both notice languages, including applicable provider roles, agreements and transfer safeguards.
- Approved Terms and Imprint/Legal Notice content.
- Category-specific financial/transaction retention and other unresolved retention/export scope decisions in the authoritative registers, including saved recommendations and standing communications retention.
- External-media and manual support-diagnostic recipient/retention policy decisions.

## Remaining Ops Gates

- Enforce and verify ordinary identifiable operational logs ≤14 days, with documented exceptions.
- Verify deployed cookie/security headers and production SPA route handling.
- Real Google/Resend configuration and delivery/callback verification; supplier/payment rollout validation where applicable.
- Supply the actual public site URL and enforce legal readiness in Docker/custom/manual release workflows.
- Complete integrated staging account/export/document/booking verification; no frontend code can certify the deployment or providers.

## Final Classification

**Limited Beta Frontend Privacy Engineering Status: IMPLEMENTED / VERIFIED within the documented scope.** Production legal readiness remains **LEGAL GATE**, deployment/provider readiness **OPS GATE**, and live integrated verification **PARTIAL**.

Technical defects addressed: 2 — the previously indefinite journey-draft lifetime and the independently demonstrated stale document-download boundary. Both fixed. Remaining reviewed severities: Critical 0, High 0, Medium 0. Missing notice/account controls were implemented as requested features, not counted as additional security defects.

Evidence binds to frontend source receipt SHA-256 `5fc8e7035903aa12a6fc50d2ad6a7c029117e3b89e5314cc14521db0cc730d87`: sorted changed/new frontend paths, each hashed as UTF-8 path + NUL + file bytes + NUL. Report excluded to avoid self-reference. The checkpoint is the commit containing this report and those frontend files; its final hash is returned in the task response.

Exact-path staging scope: 22 frontend files and this report. Existing untracked `AGENTS.md` and `CLAUDE.md` excluded. Backend, root Dockerfile and lockfiles unchanged. No push.
