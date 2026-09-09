# Commercial Pricing — Compliance Notes

**Status: TEST IMPLEMENTATION — NOT LEGAL APPROVAL.**

This document records what the V8.5 commercial pricing engine does and, more
importantly, what still has to be reviewed by qualified legal counsel before any
of it runs in Live Mode with real money. Passing tests is not legal compliance.
Nothing here is legal advice.

Everything in this build runs in Duffel **Test Mode** and takes **no payment**.
Every customer-facing surface says "sandbox / test mode".

## What the engine does today

- **Separates supplier fare from Detoura revenue.** The airline fare is shown
  exactly as the provider quoted it. Detoura's own component (a service fee and
  a percentage markup) is a distinct, disclosed line. The supplier price is
  never mutated to bury Detoura's margin.
- **`PriceBreakdown`** exposes: `supplier_transport`, `supplier_baggage`,
  `supplier_fees`, `detoura_service_fee`, `detoura_markup`, `discount`, `tax`,
  and the derived `customer_total`. The review screen renders this before the
  single confirmation.
- **Two service tiers.** `BASIC` (self-service) is the default and is never a
  pre-selected paid upgrade. `ALL_IN_ONE` is explicitly opt-in. It is a Detoura
  *service product* — orchestration, revalidation, one traveller-data flow,
  issuance, monitoring, recovery support. It is **not** described anywhere as an
  EU "package holiday" / "package travel" arrangement.
- **`DynamicMarkupPolicy`** — deterministic, bounded, auditable. It keys only on:
  service tier, ticket count, order value, currency, configured market. It has
  **no field** for any personal or protected characteristic and a test asserts
  this. No opaque personalised price discrimination.
- **Promotions are Detoura-funded.** A promo reduces Detoura's own component,
  never the supplier fare, and never drives any total below zero or Detoura's
  revenue below zero.
- **Immutable economics ledger.** Each completed booking stores the breakdown
  and the pricing-policy version used. Historical rows are never recomputed when
  the live markup rules change.
- **Tax** — a Detoura-side tax rate hook exists (`detoura_tax_rate`) and
  defaults to `0.0`. No market is configured for Detoura-side VAT in this build.

## Must be legally reviewed before Live Mode

At minimum, and not exhaustively:

1. **EU airfare price transparency** (Regulation (EC) No 1008/2008, Art. 23):
   the final price payable, including all unavoidable and foreseeable taxes,
   charges, surcharges and fees, must be shown at all times; optional price
   supplements must be opt-in and clearly communicated. Confirm the review
   screen and any earlier price display meet this once real fares flow.
2. **Consumer-law pre-contractual disclosures** (Consumer Rights Directive
   2011/83/EU and national transpositions): identity of the trader, total
   price, payment/performance arrangements, cancellation/withdrawal information.
3. **VAT / tax treatment** of Detoura's service fee and markup, per country of
   establishment and per customer location; margin-scheme vs. standard VAT;
   invoicing requirements.
4. **Payment surcharge rules** (Directive (EU) 2015/2366 / national law): limits
   or prohibitions on card/payment-method surcharges.
5. **Package Travel Directive (EU) 2015/2302** classification: does combining
   flights (and later stays) with Detoura's orchestration create a "package" or
   a "linked travel arrangement"? If so, organiser/retailer obligations apply.
6. **Organiser vs. retailer / intermediary obligations**, including information
   duties, liability for performance, and the point at which Detoura becomes the
   organiser.
7. **Insolvency protection** requirements for packages / LTAs (security for
   refunds and repatriation).
8. **Cancellation and refund disclosures** — the distinct states the engine
   already models (`CANCELLED`, `REFUND_PENDING`, `REFUNDED`,
   `PARTIALLY_REFUNDED`, `NON_REFUNDABLE`, `CANCELLATION_FAILED`) must be
   reflected in customer-facing terms.
9. **Country-of-establishment requirements** — licensing/registration for
   travel intermediaries and organisers in each market served.
10. **Advertising / drip-pricing rules** — no headline price that grows with
    unavoidable fees at checkout.

## Engineering guarantees that support (but do not substitute for) the above

- The server owns every commercial amount. No price, fee, markup or discount in
  a request body is honoured — there is no field for one.
- Markup and discount are bounded and every evaluation is explainable.
- The audit trail records every promo and policy change with actor and
  before/after state.
- `test_mode` is surfaced on the commercial summary and shown to the customer.

_Last updated: V8.5 Phase A._
