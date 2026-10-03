# V9 — Production Release Checklist

A real production release of Detoura MUST NOT proceed while any item below is
unresolved. This checklist does not replace judgment or sign-off from
whoever owns the legal/Ops decision on each gated item — it exists so that
decision has a fixed, complete list to check against, and so "we forgot to
check X" cannot happen silently. See
`docs/V9_STAGING_PRODUCTION_OPS_READINESS_REPORT.md` for the evidence and
reasoning behind each line, and `docs/V9_STAGING_RUNBOOK.md` for the staging
process this presumes already happened first.

**Do not mark any item PASS without actually performing the check.** Marking
something PASS from memory or from a prior release defeats the point of a
release-gate document.

## Legal / privacy (LEGAL GATE — hard blockers)

- [ ] `scripts/verify_production_release.sh` exits 0 (runs
      `npm run verify:legal` against `frontend/src/privacy/legalConfig.ts`)
- [ ] `LEGAL_ENTITY_NAME` set to the real, reviewed legal entity name
- [ ] `REGISTERED_ADDRESS` set to the real, reviewed registered address
- [ ] `PRIVACY_CONTACT_EMAIL` set to a real, monitored privacy contact address
- [ ] `PRIVACY_NOTICE_EFFECTIVE_DATE` set to the actual effective date (ISO
      `YYYY-MM-DD`)
- [ ] `COMPETENT_SUPERVISORY_AUTHORITY_NAME` and `..._URL` set to the real
      supervisory authority, HTTPS URL
- [ ] `COMPANION_TRAVELLER_LEGAL_NOTICE_APPROVED` set to `true` **only** after
      legal sign-off on the companion-traveller notice/basis
      (`V9_PHASE5_FINANCIAL_DOCUMENT_LEGAL_NOTES.md`,
      `V9_PRIVACY_CONSENT_RETENTION_DECISION_MATRIX.md`) — never set this to
      make the gate pass without that sign-off actually happening
- [ ] `/privacy` renders the real, approved content end-to-end (not the
      draft/unavailable state) on a build produced with the above values

## Deployment / infrastructure

- [ ] `DETOURA_ENV=production` set on the production service (gates Secure
      cookies **and** `X-Robots-Tag` indexability — verify both, not just one)
- [ ] `AUTH_TRUSTED_PROXY_HOPS` set correctly for the actual production
      network topology (`1` for Render's single edge hop; recompute for any
      other host/CDN layering — an unverified guess here is a spoofing risk,
      not a safe default)
- [ ] HTTPS verified: production URL is served over HTTPS end-to-end, not
      just "the host supports it"
- [ ] Secure cookies verified: `curl -isS <production-login-endpoint>` shows
      `Set-Cookie: detoura_session=...; Secure; HttpOnly; SameSite=lax` (not
      just checked in staging — staging deliberately does not set `Secure`)
- [ ] Security headers reviewed: CSP/HSTS/`X-Content-Type-Options`/
      `Permissions-Policy`/`Referrer-Policy` remain an **explicit, disclosed
      OPS GATE** (none exist in this codebase as of this checklist — see the
      readiness report §6) — confirm whoever owns this release has
      consciously accepted that gap or closed it in a dedicated follow-up
      slice, not silently shipped past it
- [ ] Production DB persistence verified: the volume backing
      `DETOURA_DB_PATH` is confirmed to survive a container
      restart/redeploy on the actual host (not assumed from the Dockerfile
      alone)
- [ ] `WEB_CONCURRENCY` / `--workers` confirmed to be exactly `1`, **or**
      `DETOURA_SESSION_STORE=redis` + `DETOURA_REDIS_URL` configured and
      verified — running more than one worker without the shared store
      silently corrupts personalization state (see the readiness report
      §11's benchmark reference)
- [ ] Ops console topology confirmed single-process (Ops sessions have no
      shared-store option — see readiness report §11) if Ops is enabled in
      production at all
- [ ] Backup verified: a real `.backup` (or volume snapshot) taken against
      the production DB and confirmed non-empty/queryable
- [ ] Restore tested: the most recent backup restored into a throwaway
      instance and confirmed to boot and serve `/readyz` as ready
- [ ] Log retention configured at the host/platform level to 14 days maximum
      for ordinary operational logs (this is a platform-console setting —
      confirm it was actually set, not just documented)
- [ ] `DETOURA_METRICS_ENABLED` left unset, **or** confirmed
      network-restricted (not reachable from the open internet) if enabled
- [ ] `DETOURA_OPS_TOKEN` is a production-only secret, distinct from any
      staging token, generated fresh (not reused from a lower environment)

## Provider configuration

- [ ] `STRIPE_SECRET_KEY` reviewed for production readiness — confirm
      whether this release intends to enable live Stripe charging at all; if
      not, leave `PAYMENT_LIVE_CHARGING_ENABLED` unset/`false` and record
      that decision explicitly rather than leaving it ambiguous
- [ ] `DUFFEL_ACCESS_TOKEN` reviewed — note that **live Duffel order
      issuance does not exist in this codebase as of this checklist**
      regardless of configuration (the adapter hard-refuses non-`duffel_test_`
      tokens); this is a BLOCKED item for a future slice, not something this
      release can complete
- [ ] `GOOGLE_CLIENT_ID`/`_SECRET`/`_REDIRECT_URI` — confirmed registered for
      the actual production redirect URI, and real Google Sign-In E2E
      exercised at least once against production configuration
- [ ] `RESEND_API_KEY`/`RESEND_FROM_EMAIL` — confirmed, and real Resend
      delivery exercised at least once to a real, consenting test recipient
      before enabling `COMMUNICATION_LIVE_SENDING_ENABLED` broadly

## Product policy (must remain OFF unless explicitly and separately decided)

- [ ] Analytics: `VITE_ANALYTICS_FIRST_PARTY` left unset/`false` **or** a
      real, approved consent UI has shipped first — do not enable analytics
      collection with no consent mechanism in front of it
- [ ] Attribution: OFF
- [ ] Marketing tracking: OFF (no delivery adapter exists to enable, but
      confirm no third-party script was added out-of-band)

## Testing

- [ ] Full backend regression (`python -m pytest`) green, with any known
      test-order-pollution finding from
      `docs/V9_STAGING_PRODUCTION_OPS_READINESS_REPORT.md` §16 either
      resolved or explicitly, knowingly accepted (never silently ignored)
- [ ] Frontend lint + typecheck + build (`npm run lint && npm run build`)
      green
- [ ] CI's `image` job green on the exact commit being released
- [ ] Staging smoke tests (`docs/V9_STAGING_RUNBOOK.md` §14) completed on a
      build from the same commit

## Sign-off

- [ ] Legal/Ops owner has reviewed and checked every LEGAL GATE item above
      personally — this checklist records the check, it does not perform it
- [ ] Engineering owner has reviewed and checked every Deployment/
      Infrastructure and Testing item above
- [ ] Release commit hash recorded: ******\_\_\_\_******
- [ ] Release date recorded: ******\_\_\_\_******
