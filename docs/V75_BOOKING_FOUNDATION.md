# V7.5 — Booking Foundation

**Nothing in this release books anything.** No provider call, no payment, no order, no ticket. V7.5 builds the domain shape so V8 can execute against something already reasoned about.

## The experience being built toward

A traveller picks a journey, enters their details once, confirms once — and Detoura books whatever underlying tickets that takes. Four flights on three airlines through two providers is an implementation detail they should never have to project-manage.

```
Search → choose journey → review flights → traveller details ONCE
      → revalidate ALL offers → final all-in price → confirm ONCE
      → [V8] orchestrate the underlying bookings → one confirmation
```

## The case that drives the design

Not the happy path. **The third ticket failing after two succeeded.**

```
Ticket 1  CGN → PRG   booked
Ticket 2  PRG → VIE   booked
Ticket 3  VIE → BUD   FAILED
Ticket 4  BUD → DUS   never attempted
```

This must never read as `CONFIRMED`. Telling somebody they have a holiday they cannot take is the worst thing this system could do.

**How that is enforced:**

- `JourneyBookingIntent.can_confirm` requires **every required item settled**.
- `with_state(CONFIRMED)` raises `InvalidTransition` otherwise, naming the unsettled items.
- There is deliberately **no `force` parameter** — an override would exist only to do the forbidden thing.
- `outcome` is **derived from the items**, never asserted alongside them, so a journey-level flag cannot drift from its own tickets.
- `PARTIAL_FAILURE` transitions **only** to `RECOVERY_REQUIRED`. Enumerated over the transition table: `CONFIRMED` has exactly one predecessor, `BOOKING`.
- `NOT_ATTEMPTED` is distinct from `FAILED` — nothing was sent, so nothing needs undoing.

**No automatic rollback is modelled.** Airlines do not universally support it, and inventing a rollback that silently fails would be worse than admitting a human must intervene.

## Models

| Type | Purpose |
|---|---|
| `JourneyBookingIntent` | The journey the traveller confirms once |
| `BookingItem` | One ticket, carrying a `ProviderOfferReference` — never a Duffel offer |
| `BookingState` | 15 states; transitions declared as data, not scattered `if`s |
| `RevalidationResult` | What re-checking found immediately before confirming |
| `PriceTolerance` | How much the price may move before re-consent is required |

## Expiry

A journey expires with its **shortest-lived** ticket — not the latest, not an average. It is only as bookable as its first offer to die.

## Price tolerance

Both an absolute and a percentage bound, whichever is larger: a percentage alone is too loose on an expensive trip and too tight on a cheap one.

> Approved €500, tolerance +€15 → revalidated €508 proceeds; €529 requires new confirmation.

**A price drop never needs re-consent** — nobody needs asking again to be charged less.

## Deliberately not built

Real orders · payment or card handling · a traveller PII vault · automatic refunds · rollback orchestration. `TravelerIdentityReference` is named as a seam but not implemented, because persistent sensitive PII storage is a decision that needs making deliberately rather than as a side effect of a foundation phase.

## V8 prerequisites

1. A `duffel_test_` token, and the probe run to lift **UNVERIFIED**.
2. Offer revalidation against a live sandbox.
3. An execution orchestrator — the state machine exists; nothing drives it.
4. A traveller-data boundary decision (transient payload vs stored profile).
5. A payment decision. Nothing here touches money.
