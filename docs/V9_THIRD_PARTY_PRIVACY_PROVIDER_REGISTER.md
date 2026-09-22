# V9 Limited Beta — Third-Party Privacy Provider Register

Backend/privacy register for every third-party integration this codebase
calls, built from actual code and configuration (`src/detoura/*_config.py`,
`src/detoura/providers/*.py`), not from provider terms this codebase cannot
read. **No controller/processor/DPA legal conclusion is stated here** — that
determination belongs to Legal/Privacy and is explicitly out of scope, per
both source audits and the policy lock.

| Provider | Role in the product | Data categories sent | Purpose | Credential / config mechanism | Default posture (verified in code) | Provider-side retention visibility | International-transfer dependency | Production verification status |
|---|---|---|---|---|---|---|---|---|
| **Stripe** | Payment processing (PaymentIntents, refunds, webhooks) | Amount, currency, an opaque client-side Stripe.js payment-method reference. **Never** a raw card number, CVC, or bank credential — this backend never receives them. No card brand/last4 metadata is even stored locally. | Charge/refund the customer for a booking | `STRIPE_SECRET_KEY` / `STRIPE_WEBHOOK_SECRET` via env var; `PAYMENT_LIVE_CHARGING_ENABLED` is a separate, explicit kill-switch (`payment_config.py`) | **Sandbox/test by default.** `live_charging_enabled` defaults `False`; even when `True`, an ambiguous/non-live-shaped `STRIPE_SECRET_KEY` fails closed rather than silently falling back | Not established by this codebase — Stripe's own retention of vaulted payment methods and its API objects is Stripe's policy, not Detoura's | Yes (Stripe is a US-based global processor) — DPA/SCC status is a Legal/Privacy determination, not assessed here | UNVERIFIED IN PRODUCTION — code defaults are documented, live deployment credentials/webhook config not confirmed by either audit |
| **Duffel** | Flight search, offers, orders, ticketing | Route/date/traveler-count at search time; at order time, passenger `given_name`/`family_name`/`born_on`/email/phone/gender/title (see Implementation Report §7). Passport fields are collected from the traveler but **currently never transmitted** — the adapter drops `identity_documents` for the sandbox flows this build exercises (a functional gap for a future live rollout requiring passport data, not a leak) | Flight search/booking fulfillment | `DUFFEL_ACCESS_TOKEN` via env var | **Fails closed to test mode** — a token not prefixed `duffel_test_` is rejected by the adapter itself (`providers/duffel.py::TEST_TOKEN_PREFIX`) | Not established by this codebase — Duffel's retention of order/passenger data on its own systems is Duffel's policy | Likely yes (Duffel aggregates international airline inventory) — DPA/SCC status is a Legal/Privacy determination | UNVERIFIED IN PRODUCTION for live order flows — sandbox-only exercised by this audit's test coverage |
| **Google** | Sign-in identity verification (OAuth/OIDC) only | `sub`, `email`, `email_verified` claims. Scope requested is `openid email` only (`services/google_oauth.py`) — **no profile/name scope is requested.** Access/refresh/ID tokens are explicitly discarded after use; only the derived `sub`/`email` claims are ever persisted (in `auth_identities`) | Consumer sign-in / account linking | `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` via env var | **Fails closed** — disabled (503) with no configuration; no token of any kind is ever persisted | Not established by this codebase — Google's own retention of its OAuth logs/consent records is Google's policy | Yes (Google is a global identity provider) — DPA/SCC status is a Legal/Privacy determination | UNVERIFIED IN PRODUCTION — live Google Cloud project configuration, callback URL, and consent-screen text not confirmed by either audit |
| **Resend** | Transactional email (booking confirmations, password-reset codes) | Recipient email address, email subject/body content at send time. **Body/subject content is not persisted** in Detoura's own database — only `recipient_address` (in `customer_communications`) survives past the send call | Deliver booking-confirmation and password-reset emails | `RESEND_API_KEY` via env var, sent only as a Bearer header to a fixed host (`communication_config.py`) | **Sandbox adapter by default.** Requires both an explicit provider selection *and* `COMMUNICATION_LIVE_SENDING_ENABLED=true` to send live; missing/malformed `RESEND_API_KEY`/`RESEND_FROM_EMAIL` fails closed rather than silently degrading | Not established by this codebase — Resend's own delivery-log retention is Resend's policy | Yes (Resend is a transactional email provider with its own infrastructure footprint) — DPA/SCC status is a Legal/Privacy determination | UNVERIFIED IN PRODUCTION — live sending has not been exercised end-to-end by either audit |
| **Wikimedia** (image CDN) | Destination/gallery imagery, loaded directly by the browser | Browser IP/user-agent/metadata and the requested city-image path reach Wikimedia directly (frontend-initiated, not proxied through this backend) | Recommendation/gallery visuals | None (no credential; public CDN) | `no-referrer` policy set on these requests; no analytics gate | Not established — outside Detoura's control entirely (frontend audit EXT-2) | Yes, inherent to any public CDN request | Disclosure/hosting-model decision remains open (Pre-Beta Question 13); not a backend integration and not modified by this slice |

## What this register does not state

- Controller vs. processor classification for any provider.
- Whether a Data Processing Agreement (DPA) is in place or required before
  Limited Beta — Pre-Beta Policy Question 15, owned by Legal/Privacy.
- Any provider's own internal retention duration — none of these providers'
  systems are inspectable from this codebase; only what Detoura sends and
  what Detoura itself persists is in scope here.
- Whether cross-border transfer safeguards (SCCs, adequacy decisions) are in
  place for any provider — a legal determination outside engineering's
  authority.

## Consistency finding (unchanged by this slice)

All four production-integration providers (Stripe, Duffel, Google, Resend)
share the same architectural pattern: **sandbox/test behavior by default,
with an explicit, separate configuration flag required to enable
production-grade credentials or live sending/charging.** This was the
backend audit's strongest consistency finding and remains true after this
privacy-policy implementation slice — no change in this slice touches any
of these four kill-switches or default postures.
