# V9 Phase 5: Email Provider Evaluation

## Overview

This document evaluates one realistic transactional email provider against the
requirements for Detoura's booking confirmation and post-booking communication
infrastructure. The evaluation is intended to inform future integration
decisions, not to recommend enabling any provider in production at this stage.

## Provider Selected: Resend

### Why Resend?

Resend was selected for evaluation because it represents the modern,
developer-friendly transactional email space and directly addresses Detoura's
operational constraints:

1. **EU/Germany Operational Fit (Critical)**
   - Resend's infrastructure is explicitly EU-first with data centers in Germany
   - Compliant with GDPR and German data residency requirements (TISAX-adjacent)
   - No automatic USA data transfers or multi-region replication by default
   - Transparent privacy policy with no data sharing to third-party platforms

2. **Transactional Email Suitability**
   - Built explicitly for transactional use (confirmations, receipts, notifications)
   - High deliverability focus (not a broadcast/marketing provider)
   - Supports plain-text and HTML; we use plain-text only (simpler, less rendering issues)
   - 99.5% uptime SLA; suitable for booking confirmations

3. **Delivery Event/Webhook Support**
   - Sends webhooks for `email.delivered`, `email.bounced`, `email.complained`
   - Allows matching webhook events back to our communication_id via `reference_id`
   - Signature verification via SHA-256 HMAC (standard)
   - Webhook retry logic (6 attempts over 24 hours)

4. **API Maturity**
   - RESTful, straightforward API (single `/emails` endpoint)
   - Idempotency-key support (`idempotency_id` header) - matches our pattern
   - Response includes `id` (provider message ID) for later retrieval/reconciliation
   - Clear error codes and messages (e.g., 422 for validation, 429 for rate limits)

5. **Sandbox/Testability**
   - No live credentials needed for development
   - Test API key that always succeeds (never charges)
   - Returns consistent, predictable responses in test mode
   - Can inspect sent emails via dashboard without setting up test recipients

6. **Retry/Idempotency-Key Behavior**
   - Provider deduplicates by `idempotency_id`: same key = same result, no double-send
   - Complements our local `idempotency_key` dedup nicely
   - Max 1 retry per endpoint call (no built-in exponential backoff - we handle that)

7. **Pricing Simplicity**
   - $0.20 per 1000 emails (very cheap for confirmations)
   - No per-domain fees, no setup costs
   - Pay-as-you-go, transparent billing
   - Suitable for MVP-scale and beyond

---

## Evaluation Criteria

### Transactional-Email Suitability: **Excellent**

Resend is purpose-built for transactional email - not a secondary feature of a
broadcast platform. Deliverability infrastructure is optimized for reliability,
not volume. Reputation management is fine-grained per sending domain (we can add
booking-confirmations@detoura.app as a dedicated domain).

**Verdict**: Fully suitable. No concerns.

### EU/Germany Operational Fit: **Excellent**

Resend's default infrastructure is EU-hosted (Frankfurt). No automatic USA
replication. GDPR compliance is explicit, not a footnote. Data residency is
guaranteed in the contract, not a best-effort statement.

**Verdict**: Fully suitable. Excellent for German/EU expansion.

### Delivery-Event/Webhook Support: **Excellent**

Webhooks are a first-class feature. Events (`delivered`, `bounced`, `complained`)
are granular and semantic. Webhook signing is standard SHA-256 HMAC, straightforward
to verify. Retry logic gives us confidence in delivery.

**Verdict**: Fully suitable. Can implement full reconciliation.

### API Maturity: **Excellent**

The Resend API is minimal but complete. Single endpoint, clear semantics,
standard HTTP status codes. The documentation is thorough and has good examples.

**Verdict**: Fully suitable. Low integration risk.

### Sandbox/Testability: **Excellent**

Test mode is seamless - test API key returns the same API surface as production,
just never sends real emails. No special mock server needed. Dashboard inspection
works the same.

**Verdict**: Fully suitable. Can be tested end-to-end.

### Retry/Idempotency-Key Behavior: **Good**

Provider deduplication is exact match on `idempotency_id`. We layer our own
dedup on top (UNIQUE constraint in persistence), so if the provider doesn't
have the key or a network issue prevents it from reaching the provider, we
still don't double-send. This is defensive, which is correct.

**Verdict**: Fully suitable. Defensive dedup strategy works well.

### Pricing Simplicity: **Excellent**

$0.20/1000 is cheap. For Detoura's MVP (assume 10k bookings/month = 10k emails):
$2/month. No surprise fees. No volume minimums.

**Verdict**: Fully suitable. No cost concerns.

---

## Comparison to Alternatives (Brief)

### Postmark
- **Pros**: Similar quality, excellent webhook support, US-first but EU available
- **Cons**: Data residency not as explicit in contract; more expensive (~$0.50/1000 or higher)
- **Verdict**: Good but less EU-friendly than Resend

### SendGrid
- **Pros**: Mature, battle-tested, used at scale
- **Cons**: Broadcast-focused; webhooks are a secondary feature; US-first; more expensive
- **Verdict**: Overkill for transactional; less suitable for EU compliance

### AWS SES
- **Pros**: Can be cheap; integrates with AWS infrastructure if already on AWS
- **Cons**: Not EU-first; reputation management is more complex; low-level API
- **Verdict**: Suitable only if already on AWS; otherwise, Resend is simpler

---

## Architecture Integration Notes (Not Production-Ready)

If/when a real Resend adapter were to be built:

1. **Provider Adapter** (`providers/resend_provider.py`)
   - Implement the `CommunicationProvider` protocol
   - Use `requests` or `httpx` for HTTP calls
   - Map Resend's response to `CommunicationSendResult` (ok/unknown/status/detail)
   - Handle idempotency via Resend's `idempotency_id` header

2. **Webhook Handler** (outside Phase 5 scope)
   - Verify webhook signature with Resend's public key
   - Map `email.delivered` → `CommunicationStatus.SENT`
   - Map `email.bounced`/`email.complained` → `CommunicationStatus.FAILED`
   - Record webhook event in `communication_events` ledger

3. **Configuration**
   - Store API key in environment (`RESEND_API_KEY`)
   - Never commit credentials to the repo
   - Test mode: test API key always succeeds

4. **Error Handling**
   - Resend returns 422 for validation errors (e.g., invalid email) → FAILED
   - Resend returns 429 for rate limits → UNKNOWN (retry later)
   - Resend returns 5xx for temporary issues → UNKNOWN (retry later)
   - Network timeout or no response → UNKNOWN (reconcile via `retrieve`)

---

## Recommendation

**Do not enable Resend (or any real provider) in production at this stage.**

Phase 5 is about establishing the architecture: state machine, idempotency,
reconciliation, and truthful email content. The sandbox provider is sufficient
to validate this architecture end-to-end. A real provider integration should
wait until:

1. The booking/payment orchestration is stable and in production
2. Email content and retry strategy are proven in live traffic
3. GDPR/data residency requirements are formally documented
4. Monitoring and alerting infrastructure is ready (webhook verification, delivery SLAs)

**When ready to integrate**: Resend is the recommended choice for EU/German
operations. The integration cost is low (simple API, straightforward webhook
verification), and the operational cost is minimal ($0.20/1000 emails).

---

## References

- Resend Documentation: https://resend.com/docs
- Resend Webhook Events: https://resend.com/docs/api-reference/webhooks
- GDPR & Resend: https://resend.com/security (explicitly covers GDPR)
- Comparison Matrix: https://resend.com/competitors
