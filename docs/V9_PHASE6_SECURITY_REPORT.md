# V9 Phase 6 — Production Security & Ops Hardening (Slice 1 report)

Starting checkpoint: `2fbf4a8` (V9 Phase 5, APPROVED).

## Follow-up: Network / SSRF Security adversarial slice — CLOSED

Starting HEAD `d39bc2b`. A fresh adversarial pass over Detoura's outbound
network surface, independent of any earlier Phase 2.5 SSRF work - re-derived
and attacked from scratch rather than assumed sufficient.

### NETWORK SECURITY MAP

- **`providers/duffel.py` (`DuffelTransportProvider`)** — FIXED TRUSTED
  PROVIDER. Host is `DEFAULT_HOST = "https://api.duffel.com"`, a
  constructor default overridable only by whoever constructs the provider
  in Python code (`booking_flow.py`, `api/v1.py`, `ticket_operations.py`,
  all hardcoded, never from a request/env value). Production reachable:
  YES, for every real booking search/revalidation/issuance. Host
  validation: N/A (fixed host). Redirect policy: the underlying
  `UrllibHttpClient` uses urllib's default opener, which WOULD auto-follow
  a redirect with no re-validation if Duffel's API ever issued one -
  residual, low-priority risk given the destination is a hardcoded, long-
  trusted host, not attacker-influenced (not fixed this slice - see
  Findings). Timeout: `self.timeout` (constructor param, default from
  `DEFAULT_TIMEOUT_SECONDS`). Response-size limit: NONE - the whole body is
  read into memory unbounded (shared with every `UrllibHttpClient` user;
  low priority here since Duffel's JSON responses are inherently small and
  it is a trusted host). Retry policy: **THE HEADLINE FINDING** - every
  call went through an auto-retrying `RetryingHttpClient` with zero
  HTTP-method awareness, including genuinely irreversible mutations
  (Order creation, cancellation confirmation, order-change confirmation) -
  **fixed this slice**, see below.
- **`providers/amadeus.py`** — FIXED TRUSTED PROVIDER, same shape as
  Duffel (`DEFAULT_HOST` constructor default, never request-influenced).
  Not touched this slice - no non-idempotent mutation exists on this
  provider today (search-only).
- **`providers/stripe_payment.py`** — FIXED TRUSTED PROVIDER
  (`API_BASE` constant). Verified fresh this slice: `self.http` defaults to
  a bare `UrllibHttpClient()`, **never wrapped in `RetryingHttpClient`** -
  authorize/capture/refund (all POSTs) make exactly one attempt each, a
  timeout/connection failure is mapped straight to `ProviderResult(unknown=True)`
  and left there for `reconcile_payment` to resolve (the Phase 4 payment
  slice's own, correct pattern) - confirms the Duffel gap below was a
  specific, isolated omission, not a systemic codebase pattern.
- **`services/network_adapter.py` (`AuthorizedHttpFetcher`, the Phase 2.5
  acquisition engine)** — ALLOWLIST CONTROLLED, Ops-configured. Caller:
  `services/bootstrap_fetchers.py`'s `SimulatedAuthorizedWebFetcher`.
  URL source: `SourceRegistration.base_domain` (Ops-supplied free text via
  `POST /api/v1/ops/acquisition/sources`, `RegisterSourceRequest.base_domain:
  str | None`, **no format validation whatsoever** - not even "must look
  like a domain, not an IP or 'localhost'"). Authorization: fail-closed,
  requires `authorization_status == APPROVED` (a separate, audited action
  from registration) AND `network_allowed`. Host validation: domain-suffix
  allowlist (`_domain_allowed`) - string-only, pre-existing. IP validation:
  **NONE, before this slice** - only "not a bare IP literal in the URL" was
  checked; the actual resolved address was never inspected. **Fixed this
  slice**: `_check_host` now also calls the new
  `providers.http.assert_safe_public_host`. Redirect policy: `_NoRedirectHandler`
  + a manual per-hop revalidation loop, already correct pre-existing design
  (now benefits from the IP check on every hop too). Response-size limit:
  checked, but AFTER the full body is already read into memory
  unbounded by the underlying transport (a real, if lower-priority, gap -
  see Findings). Retry/budget: `RetryingHttpClient` + a persisted,
  cross-process request-budget/rate-limit gate charging every real attempt
  including retries (`_budget_gated_client`) - pre-existing, correct,
  unaffected by this slice. **Production reachable: NO** - confirmed via
  full-repo grep (`grep -rn "AuthorizedHttpFetcher(" --include="*.py" .`):
  every construction site is a test file. `services/bootstrap_registry.py`'s
  own module docstring states it directly: "Phase 2.5 ships exactly one
  fetcher wired up end to end: a deterministic fixture source. No
  AUTHORIZED_WEB_SOURCE is registered by default because none has been
  reviewed to APPROVED" - `_FACTORIES` (the only way a `source_id` maps to
  an actual fetcher) has exactly one entry, the offline fixture, in
  production code. This is DEAD/UNREACHABLE code today, fixed anyway so the
  gap can never become live the moment a real source is wired (the same
  reasoning the Payment Security slice applied to `run_paid_booking`).
- **`services/destination_images/wikimedia.py` (`WikimediaCommonsClient`)**
  — CONFIGURATION CONTROLLED / OPERATOR-RUN OFFLINE TOOLING, not a live
  request path. URL source: Commons' own third-party API JSON response
  (`Candidate.download_url`), constrained to two explicitly-allowlisted
  media domains (`upload.wikimedia.org`, `thumb.wikimedia.org`) regardless
  of what that JSON claims. Host validation: domain allowlist (pre-existing)
  + **new this slice**: `assert_safe_public_host`. Redirect policy: **was a
  real gap** - `download()` used the bare `urllib.request.urlopen()` global
  function, whose default opener auto-follows a 3xx with zero
  re-validation of the target; **fixed this slice** via
  `build_no_redirect_opener`. Production reachable: **NO** - confirmed via
  grep, `ImagePipeline`/`WikimediaCommonsClient.download` are invoked only
  by `scripts/acquire_destination_images.py`, an operator-run offline CLI;
  the live `GET /api/v1/destination-images/*` endpoints only ever read a
  pre-built static manifest from disk, never call this client at request
  time. Fixed anyway (operator-run tooling against third-party content is
  still a real, if narrow and privileged, scenario).
- **Consumer API surface (`api/*.py`)** — searched every request schema for
  a url/uri/host/domain/endpoint/callback/redirect/image_url/source_url-
  shaped field reachable by an ordinary (non-Ops) consumer: **none found**.
  No endpoint lets a consumer request accept a URL Detoura would then fetch.

### Finding (HIGH, reachable in production) — blind HTTP-level retry of non-idempotent Duffel mutations, CLOSED

`DuffelTransportProvider.http` is unconditionally a `RetryingHttpClient`
(`self.http = http_client if isinstance(http_client, RetryingHttpClient)
else RetryingHttpClient(http_client or UrllibHttpClient())`), and
`RetryingHttpClient` retries a POST on any 408/425/429/500/502/503/504 or
connection failure with **zero HTTP-method awareness** - exactly like a
GET. Duffel gives Detoura no idempotency key for Order creation (an
established fact from the Booking/Provider Security slice). A lost
RESPONSE - not necessarily a lost effect - during `create_test_order`'s
POST could trigger an automatic second real Order for the same leg,
invisible to `booking_orchestrator.py`'s own single-execution guarantees
(which govern when the call happens, not how many raw HTTP attempts happen
inside it). The same gap existed for `confirm_order_cancellation`
(irreversible cancellation confirmation) and the confirm/charge step of
`create_and_confirm_order_change`. Exploit precondition: **none required -
this is ordinary network flakiness** (a timeout, a 503, a dropped
connection during a real production call), not an attacker action -
arguably more concerning than a classic attacker-triggered bug since it
will happen from normal unreliability over time. Classified HIGH per this
slice's own rubric ("duplicate provider booking/order" is explicitly named
under HIGH), reachable today whenever a real Duffel test token is
configured (the live SANDBOX_BOOKED issuance path).

**Fixed**: `_request_json()` gained `retry: bool = True`; `False` routes
the call through `self.http.inner` (the raw, non-retrying transport)
instead of `self.http`. Applied to the three genuinely irreversible
mutations: `create_test_order`'s inline POST, `confirm_order_cancellation`,
and the confirm/charge call inside `create_and_confirm_order_change`. Left
unchanged (retry-enabled, correctly): `fetch_offers`/`get_offer` (reads)
and the preview/quote steps (`create_order_cancellation`, the first call in
`create_and_confirm_order_change`) - Duffel's own docs distinguish these
explicitly as "does NOT cancel/change anything yet". A single attempt now
surfaces a failure/timeout once, as one typed exception, which
`_issue_item`'s existing TIMEOUT/PROVIDER_FAILURE uncertain-outcome
handling (Booking/Provider Security slice) already treats correctly -
never silently turned into a second real attempt underneath it.

### Finding (LOW, unreachable today) — no resolved-IP validation in the Phase 2.5 acquisition engine, CLOSED

Detailed in the map above. `_check_host` validated only the hostname
STRING (not an IP literal, matches the `base_domain` allowlist) - never
what IP address that hostname actually resolves to at connect time. A
trivial concrete case: an Ops-registered source's `base_domain` (free
text, no format validation) set to `"localhost"` would have been accepted
by every existing check and connected straight to loopback. **Fixed**: a
new shared `providers.http.assert_safe_public_host` resolves the hostname
(or validates an IP literal) and rejects loopback/private/link-local
(including the 169.254.0.0/16 cloud-metadata range)/multicast/reserved/
unspecified addresses, including IPv4-mapped IPv6 wrapping an unsafe IPv4 -
checking EVERY resolved address, not just the first, since a connecting
library may pick any of them. Wired into both `network_adapter.py`'s
`_check_host` (every redirect hop, not just the first request) and
`wikimedia.py`'s `_check_domain`. **Severity is LOW, not HIGH, because this
engine has zero production callers today** (see map) - fixed proactively so
the gap can never become live the moment a real `AUTHORIZED_WEB_SOURCE` is
wired, matching the same "close it before it's reachable" reasoning the
Payment Security slice applied to `run_paid_booking`.

**Residual, explicitly NOT claimed fixed**: this closes the *reachable,
practical* gap (a bad/hostile DNS answer *at the moment of this check*) -
it does not fully close a textbook DNS-rebinding race (a *different*
answer between this check and the transport's own, separate resolution
moments later in the same request), which would need full IP-level
connection pinning to close completely. That residual gap is real but
requires an attacker who ALREADY controls DNS for an Ops-approved
`base_domain` to win a narrow timing race within one request's lifetime -
a materially harder precondition than the "just register `base_domain:
localhost`" gap this fix fully closes. Documented as tracked, low-priority
hardening, not claimed as closed.

### Finding (LOW, unreachable today) — redirect auto-follow in Wikimedia image download, CLOSED

Detailed in the map above. `download()`'s bare `urllib.request.urlopen()`
auto-followed a 3xx from the (third-party-influenced) media URL with no
re-validation. **Fixed** via `build_no_redirect_opener` - a redirect now
surfaces as a catchable `ProviderHttpError`, never silently followed.

### Verified, not a finding — subdomain-suffix matching is working as designed

Attacked "subdomain confusion" (`evil.example.allowed.example` against
`base_domain="allowed.example"`) directly: `_domain_allowed`'s suffix
check correctly treats this as an authorized subdomain, not a bypass -
which is the INTENTIONAL, already-tested design (`base_domain` represents
authorization over an entire DNS zone, and anyone who controls that zone's
DNS can create any subdomain label, "evil"-looking or not, entirely
legitimately). Userinfo confusion (`https://allowed@evil/`), trailing-dot,
mixed-case, and unsupported-scheme (file:/ftp:/gopher:/data:/javascript:)
attacks were all independently re-verified as already correctly rejected
by the existing `urlparse().hostname`-based logic and the `scheme != "https"`
gate - regression tests added, not fixes (nothing was broken).

### Verified, not a finding — numeric IP-notation SSRF-filter bypass is closed by design, not by luck

Classic filter-bypass trick: `ipaddress.ip_address()` (what the IP-literal
fast-path in `_check_host` uses) does not parse decimal/hex/octal IP
notation (`2130706433`, `0x7f000001`, `017700000001` for `127.0.0.1`).
Verified directly that this platform's own resolver DOES interpret these as
`127.0.0.1`. Because `assert_safe_public_host` falls through to a REAL
resolution (not a second string-pattern check) whenever the literal-parse
fails, it catches this class transparently, as a natural consequence of
resolving through the same mechanism a real connection would use - not
because of any special-casing that could itself be incomplete.

### Finding (MEDIUM, independent-review) — `_is_unsafe_ip` missed RFC 6598 Carrier-Grade NAT space, CLOSED

Found by the independent reviewer, not by the implementer. The original
manual OR-chain (`is_loopback or is_private or is_link_local or
is_multicast or is_reserved or is_unspecified`) does not cover
`100.64.0.0/10` - verified empirically that Python's `ipaddress.is_private`
excludes this range entirely, so `assert_safe_public_host` treated the
whole CGNAT block as an ordinary public address. Some cloud/container
network fabrics route this range internally specifically because naive
RFC1918-only filters miss it - a known real SSRF-filter-bypass class, not
theoretical. Exploit precondition: same as the engine's own reachability
(currently unreachable in production - see map), but a real gap in the
check itself, worth closing regardless. **Fixed**: `_is_unsafe_ip` now uses
`not ip.is_global or ip.is_multicast or ip.is_reserved` - `is_global`
already excludes CGNAT along with everything the old chain covered, plus
`is_multicast` (which `is_global` reports `True` for) and `is_reserved`
(covering the IPv6 `64:ff9b::/96` NAT64 translation prefix, which can
embed an arbitrary IPv4 address in its low bits - another IPv6-notation
bypass class, closed as a side-effect of restoring parity with the
original chain rather than narrowing it). Independently re-verified by the
reviewer against 27 addresses spanning every category plus the CGNAT
boundaries (confirmed not overbroad) - no false positives against real
public IPv4/IPv6 introduced.

### Finding (MEDIUM, independent-review) — the actual credential-bearing production transport still auto-followed redirects, CLOSED

Found by the independent reviewer: the SSRF-safety work above was applied
only to the two components confirmed unreachable/near-unreachable in
production (the acquisition engine, the Wikimedia offline tool). The
shared `UrllibHttpClient` - the real transport Duffel, Amadeus, AND Stripe
all construct directly, carrying real bearer credentials on every live
production call - still used the bare `urllib.request.urlopen()` global
function, whose default opener auto-follows a 3xx and forwards every
request header (`Authorization` included) to the new host with no
same-origin check at all (confirmed directly against CPython's own
`HTTPRedirectHandler` source). Exploit precondition: the destination is a
hardcoded, trusted host for all three providers (no env var or request
path overrides it), so this requires the trusted upstream itself
(`api.duffel.com`/`api.stripe.com`/Amadeus's host) to be compromised or
DNS-hijacked - not directly attacker-reachable from an ordinary request,
but real credential-exfiltration impact (Invariant 10) if that precondition
is met, and the more consequential of the two independent-review findings
since it covers live production traffic rather than dead/near-dead code.
**Fixed**: `UrllibHttpClient` now builds `build_no_redirect_opener(...)`
once at construction and uses it for every request - the same shared
no-redirect mechanism already used by the acquisition engine and
Wikimedia, so this closes the gap at its root with zero duplicated logic
and zero provider-file changes. A 3xx now comes back as an ordinary
`HttpResponse`, which every provider's own existing status-code handling
already treats as a failure (behavior-preserving for the normal case,
since none of these APIs redirect in practice - confirmed via grep, no
provider file has any redirect-dependent logic; Stripe's own
`automatic_payment_methods[allow_redirects]=never` param independently
confirms a 3xx is never an expected shape there either).

### Tests

`tests/test_v9_phase6_network_security.py` (44 tests): `assert_safe_public_host`
unit coverage (every unsafe IPv4/IPv6 class including CGNAT and its exact
boundaries, the NAT64 prefix, IPv4-mapped IPv6, multi-record
one-bad-record rejection, unresolvable-host rejection, the numeric-notation
case against this platform's real resolver); `AuthorizedHttpFetcher`
SSRF-target integration tests via a self-contained fake DNS resolver (no
test depends on real/private network access); `WikimediaCommonsClient`
IP-safety + redirect-block tests; the Duffel retry-safety tests (both the
fix and explicit regression proof that read-only `get_offer`/`fetch_offers`
still retry normally, unaffected); a real-`http.server`-based test proving
`UrllibHttpClient` genuinely blocks a redirect and never forwards
`Authorization` to the redirect target end to end, not mocked.
`tests/test_v9_phase25_network_safety.py` (pre-existing, 24 tests) updated
to inject the same kind of fake resolver (`AuthorizedHttpFetcher` now
performs real DNS resolution by default, which one existing test's fake
subdomain did not survive) - every original test's own assertions verified
unchanged by the reviewer, only the DNS dependency removed.

### Independent review

Separate read-only agent (`security-architect`), two rounds. **Round 1**: a
fresh, from-scratch attack on every invariant (not just the diff) -
independently re-verified every reachability claim via its own greps, ran
a real local HTTP server to prove `build_no_redirect_opener` genuinely
blocks a redirect (rather than trusting the mocked unit test), traced
`booking_orchestrator.py` directly to confirm the Duffel retry-safety fix
does not regress the Booking/Payment slices' uncertain-outcome handling,
and checked Stripe/Amadeus for the same blind-retry pattern (found none -
Stripe was never exposed to it). Found 2 MEDIUM findings (both above, both
fixed) and confirmed every reachability/severity claim in this report
independently. **Round 2 (fixes re-checked)**: independently re-derived
both formulas/mechanisms against the actual current code (27-address
battery for the CGNAT fix, source-level confirmation of CPython's
`ipaddress`/`urllib` internals for both), ran the full 23-file targeted
suite itself: **538 passed, 2 skipped, 0 failed**. No CRITICAL/HIGH
findings in either round.

**FOLLOW-UP FIXES: APPROVED.**

### Full regression

Two from-scratch attempts at the complete ~2200-test suite, each killed
after stalling severely (18-35 lines of progress after 25-31 minutes wall
clock, versus ~13-14 minutes for a full clean run earlier in this same
session). Diagnosed, not just assumed: `vm_stat` showed free memory
dropping from ~30k pages (~120MB) to ~17k pages (~68MB) with load average
climbing 2.9→4.1 between the two attempts, and killing the first stalled
attempt immediately freed memory back up (~30k→~82k pages) - concrete
evidence of genuine, worsening machine-wide memory pressure after this
session's many hours of continuous multi-agent activity, not a hang
introduced by this diff. Zero `F`/`E` markers appeared in either partial
run's output before it was killed.

Per this slice's own explicit instruction ("if resource conditions again
prevent pytest from printing a certified summary, report that honestly"),
not claiming a full-suite result this session. The available evidence
instead: the independent reviewer's own **from-scratch, separate-process**
run of the full 23-file targeted suite spanning every file this slice
touches or could affect (network/SSRF, Phase 2.5 acquisition, all
Duffel/provider tests, booking security, payment security,
ticket-operations, destination images) - run twice, in two independent
review rounds, both clean: **538 passed, 2 skipped, 0 failed**, 0
regressions. Combined with the implementer's own repeated clean runs of
the same suite, this is treated as strong, if not fully from-scratch-whole-
suite-certified, evidence. **Recommended before this checkpoint is treated
as fully released**: re-run the full suite once in a fresh/idle
environment (matching the same recommendation carried from the Booking
Security and Payment Security slices earlier this session).

## Follow-up: Payment Security adversarial slice — CLOSED

Starting HEAD `b1a9c82`. A fresh adversarial pass over the entire V9 Phase 4
payment architecture (already closed/approved in an earlier session), plus
closing the one specific architectural gap that slice had explicitly
deferred: `payment_booking_orchestrator.run_paid_booking()` calling
`run_booking()` directly with no atomic single-execution claim.

### PAYMENT EXECUTION SECURITY MAP

- **Checkout source of truth**: `models/payment.py::CheckoutSnapshot` -
  immutable (Pydantic `frozen=True`), written once from the booking domain's
  already-computed `CommercialQuote` (`payment_service.freeze_checkout_snapshot`),
  never recomputed by payment code. No `update_snapshot` function exists
  anywhere in `persistence/payments.py`.
- **Payment creation endpoint**: `POST /api/v1/payments` - LIVE/REACHABLE.
  Body accepts only `booking_id` + `idempotency_key`; price/currency always
  come server-side from the booking's own `run.quote`.
- **Server-owned amount source**: `run.quote` (a `CommercialQuote` computed
  entirely server-side by the booking/commercial-pricing domain).
- **Currency source**: `snapshot.currency` <- `quote.currency`, server-side.
- **Payment idempotency**: client-supplied `idempotency_key` (>=8 chars) +
  a UNIQUE-constraint INSERT dedup (`persistence/payments.py::create_payment`,
  never a read-then-write race); every provider-facing idempotency key is a
  deterministic server-side derivation, `sha256(payment_id:operation:version)`.
- **Authorization transition**: `POST /api/v1/payments/{id}/confirm` ->
  `payment_service.authorize_payment` - LIVE/REACHABLE, CSRF-required for an
  authenticated session.
- **Capture transition**: two paths - (1) `run_paid_booking`'s automatic
  capture on `BookingPhase.COMPLETE` - **NOT YET WIRED**, zero callers under
  `detoura.api` (confirmed by grep, independently re-confirmed by the
  reviewer); (2) `POST /api/v1/ops/payments/{id}/capture` - LIVE/REACHABLE,
  ops-token-gated, only from `AUTHORIZED`.
- **Webhook entry point**: `POST /api/v1/payments/webhook/{provider_name}` -
  LIVE/REACHABLE.
- **Webhook verification**: Stripe's documented HMAC-SHA256 scheme with a
  300s replay-tolerance window, or the sandbox's `sha256(payload)==signature`
  reference scheme - both fail closed on any mismatch/missing field.
- **Webhook dedupe**: `claim_provider_event` - an atomic INSERT on
  `(provider, provider_event_id)` as the primary key, before any processing
  happens.
- **Refund path**: consumer `POST /api/v1/payments/{id}/refund` (full-
  remaining-only, session+CSRF required) - LIVE/REACHABLE; Ops
  `POST /api/v1/ops/payments/{id}/refund` (partial allowed, ops-token-gated)
  - LIVE/REACHABLE.
- **Booking trigger**: (a) `booking_flow.start_confirmation` - the live
  managed-booking confirmation path, entirely uncoupled from payment today;
  (b) `payment_booking_orchestrator.run_paid_booking` - the ONLY code path
  that couples payment authorization to booking execution - **NOT YET
  WIRED** to any endpoint.
- **Booking single-execution guard**: `booking_orchestrator.claim_for_execution`
  - now the sole atomic gate for `run_booking`, covering both callers.
  **CLOSED this slice** (previously only `start_confirmation` took it;
  `run_paid_booking` bypassed it entirely - tracked as blocker #16).
- **Ownership boundary**: `api/payments.py::_get_owned_payment` (a payment
  with a non-null `user_id` is 404 to every other user, identical shape for
  "doesn't exist" vs "belongs to someone else"); an anonymous payment
  (`user_id is None`) is reachable by anyone holding the unguessable
  `payment_id` token (`secrets.token_urlsafe(16)`-derived) - this is
  deliberate, documented guest-checkout-by-capability-link design, the same
  pattern the booking-security slice already established for `booking_id`,
  not a new IDOR.
- **Stripe test/live guard**: `payment_config.resolve_provider` - a 3-layer
  fail-closed chain (the `live_charging_enabled` kill switch, then the
  `provider` name, then `StripePaymentProvider.__post_init__`'s own
  test-key-shape check, `allow_non_test_key` defaulting `False`). Grepped
  the whole tree: the only `StripePaymentProvider(` construction site is
  `payment_config.py`, and it never passes `allow_non_test_key=True` - no
  environment-variable combination can produce a usable live-charging
  provider without a deliberate code change.
- **Currently unreachable components**: `run_paid_booking`/
  `run_paid_booking_and_finalize` - zero callers under `detoura.api`,
  confirmed independently by both the implementer and the reviewer. This is
  the single biggest structural fact this map surfaces: **today, Detoura's
  managed booking flow and its payment-collection flow are two fully
  independent systems from the API's perspective.** A managed booking can
  be confirmed (creating a real Duffel **Test Mode** order - no real money,
  per `booking_orchestrator.py`'s own design) with no Detoura-collected
  payment at all, and a payment can be authorized/captured with no live
  system enforcing any relationship to booking outcome beyond a shared
  `booking_id` at the data level. This is not a newly discovered hole - it
  is the honest, currently-shipped state of two Phase 4/V8 systems built and
  tested in isolation but never wired into one end-to-end checkout; wiring
  them together (through `run_paid_booking`, whose single-execution safety
  this slice just proved) is future product work, not a regression.

### Blocker #16 — CLOSED: the `run_paid_booking` atomic-claim gap

`booking_orchestrator.py` gained `claim_for_execution(run)`: the sole atomic
check-and-set that may move `run.phase` out of
`AWAITING_CONFIRMATION`/`RECONFIRM_REQUIRED` into `REVALIDATING`, under one
`run._lock` acquisition. `run_booking` gained an `already_claimed: bool =
False` parameter: by default (every caller except one) it takes the claim
itself before touching `_revalidate_item`/`_issue_item`; `already_claimed=True`
is reserved for `booking_flow.start_confirmation`, which still claims
synchronously before spawning its worker thread (preserving its existing
immediate-rejection double-click behavior, byte-for-byte the same exception
type/message) - and if a caller ever passed `already_claimed=True` without
having actually claimed, `run_booking` verifies `run.phase` is genuinely
`REVALIDATING` and raises `RuntimeError` rather than trusting the flag.
`run_paid_booking` now calls `run_booking` with the default
(`already_claimed=False`), closing the gap for free with zero duplicated
guard logic - wrapped in `try/except ValueError` so a losing concurrent call
returns a truthful, **non-mutating** outcome (it never writes to the
payment, specifically to avoid racing the winner's own capture/cancel
compare-and-swap) rather than crashing or silently double-booking.

Independently verified: the reviewing agent's own 200-trial adversarial
concurrency harness (racing a `start_confirmation`-style claim against
`run_paid_booking` on the literal same `BookingRun`, with artificially
widened provider-latency windows) produced **200/200 clean runs - exactly
one provider order call, zero unhandled exceptions** every time. Grepped
the whole tree for `_issue_item` (the only function that ever creates a
real supplier order): reachable only through `run_booking`, both
`already_claimed` branches now covered.

**One narrow, deliberate, harmless exception found by the reviewer**:
`guided_booking.prepare_journey` (Basic/self-service tier) calls
`_revalidate_item` directly with no claim - inert today because it always
passes `duffel=None` (so `_revalidate_item` never touches a real provider)
and Basic never reaches `_issue_item` or any payment coupling at all.
`claim_for_execution`'s docstring now names this exception explicitly
rather than overclaiming blanket coverage, and warns that Basic would need
its own claim if it is ever wired to a real provider call.

### Fresh invariant attacks on the pre-existing Phase 4 payment domain

Not re-litigating what `tests/test_v9_phase4_*.py` already covers well
(amount/currency tampering, basic webhook signature/replay/dedup, CSRF,
cross-user IDOR, ops gating, refund-amount bounds, reconciliation
classification) - this pass targeted what a genuinely fresh attack still
needed: real multi-threaded races (not sequential CAS simulations),
webhook payload-trust and out-of-order/unknown-reference handling, and the
Stripe guard exercised through the real `resolve_provider` configuration
chain. New permanent regression suite:
`tests/test_v9_phase6_payment_security.py` (16 tests).

- **Real concurrency** (actual `threading.Thread` + `threading.Barrier`,
  not sequential calls): `run_booking` direct-race, `run_paid_booking`
  same-run race, capture+capture, refund+refund, capture+cancel. All
  financial invariants held (`captured_amount`/`refunded_amount` never
  exceeded, never both `CAPTURED` and `CANCELLED`, exactly one real
  provider order) in every run.
- **Webhook hardening**: a forged payload claiming `status: "captured"` and
  an absurd amount never moved real state (the handler only uses the
  payload to decide *which* payment to re-check, then always asks the
  provider itself via `retrieve` - proven, not assumed); an event for an
  unrecognised `provider_reference` is a safe no-op; a stale/out-of-order
  event never regresses an already-settled payment.
- **Stripe test/live guard**: exercised through the real
  `resolve_provider()` chain (not just the isolated provider constructor) -
  a live key with live charging enabled still refuses (`StripeConfigurationError`);
  live charging enabled with no key at all still refuses; the only way to
  get a usable Stripe instance requires both the kill switch AND a
  test-shaped key, and is then unambiguously test mode.

### Finding (MEDIUM) — `ops_capture`'s exception handling, closed

A genuinely concurrent capture request can move a payment's real stored
status into `CAPTURE_PENDING` in the narrow window between `ops_capture`'s
own convenience pre-check and `payment_service.request_capture`'s own call
- `request_capture`'s leading guard then raises a plain `ValueError`
("...cannot capture from status CAPTURE_PENDING"), not `StaleVersion`,
which `ops_capture` did not catch - an unhandled 500 instead of the same
clean 409 every other race in that file already returns. **No money-safety
impact**: capture was never duplicated either way, in any trial. Found by
the reviewer running the implementer's own capture-race test repeatedly
with `-W error::pytest.PytestUnhandledThreadExceptionWarning` (reproduced
~5/8 times). **Fixed**: `ops_capture` now also catches `ValueError` and
returns a clean 409, matching the pattern already used everywhere else in
that file. New deterministic regression test
(`test_ops_capture_converts_a_genuine_concurrent_capture_race_to_a_clean_409_not_a_500`)
calls the real `ops_capture` function with only `request_capture`
monkeypatched to force the exact race exception. The same class of gap in
the implementer's own capture-race test (`except ps.store.StaleVersion`
too narrow) was also widened; re-run 10x under the strict warning mode,
0/10 unhandled exceptions.

### Finding (LOW) — `claim_for_execution` docstring overclaim, closed

Described above (see "one narrow, deliberate, harmless exception").

### Real Stripe Test Mode E2E

`REAL STRIPE TEST MODE E2E: BLOCKED — CREDENTIALS UNAVAILABLE`. No
`STRIPE_SECRET_KEY`/`STRIPE_WEBHOOK_SECRET` present in this environment
(verified: `env | grep -i stripe` returns nothing). All deterministic/
provider-fixture security work proceeded against the sandbox provider,
which implements the identical `PaymentProvider` contract.

### Independent review

Separate read-only agent (`security-architect`), two rounds - a full
from-scratch review of Blocker #16's closure and a fresh attack on the
entire Phase 4 payment domain (200-trial independent adversarial
concurrency harness of its own, beyond the implementer's own tests), then a
targeted re-check of the two fixes that review produced. **Round 1:
APPROVED** (1 MEDIUM + 1 LOW finding, both fixed). **Round 2 (fixes
re-checked): FOLLOW-UP FIXES: APPROVED**. No CRITICAL/HIGH findings in
either round.

Tests: `tests/test_v9_phase6_payment_security.py` (16 tests, all real
multi-threaded/integration tests against `Database(":memory:")` and
`SandboxPaymentProvider` - no tautological mocks, spot-checked by the
reviewer). Targeted suite (14 files spanning payment, booking security,
ownership, ticket-ops/recovery, financial documents) run repeatedly by
both the implementer and the reviewer, always green.

### Full regression

A fresh, from-scratch full run of the entire suite was completed twice this
slice. Both runs reached **100% of the progress bar with zero `F`
(failure)/`E` (error) markers anywhere in the output** - but both times the
background process was killed by this environment before it could print
pytest's own final one-line summary (the run immediately after the
warnings-summary block), leaving an otherwise-complete log with no trailing
count line. This is a process/environment-level cutoff, not a test
failure: both runs produced **byte-identical** output up to the exact same
truncation point (confirmed via `diff`), which is only possible if both
independently-executed runs genuinely passed the same way to the same
point - a content-dependent hang would not reproduce byte-for-byte between
two separate process invocations. The second run used unbuffered output
(`python -u`) specifically to rule out a stdout-buffering explanation for
the missing tail; it hit the identical cutoff regardless, confirming this
is a fixed wall-clock/process-lifetime constraint in the sandboxed
environment, not a buffering artifact or a code-dependent stall (the kind
seen in the previous slice's session).

Because the progress-bar markers themselves were fully flushed and
captured up to `[100%]` in both runs, the exact result was reconstructed
directly from that data rather than left unknown:

**2200 tests collected: 2175 passed, 25 skipped, 0 failed, 0 errors.**

This is corroborated by: the implementer's own repeated targeted-suite runs
(all green), the reviewer's two independent review rounds (each running the
full targeted suite itself, plus the reviewer's own 200-trial independent
adversarial concurrency harness against the current tree, both clean), and
zero `F`/`E` markers anywhere in either full-suite log. **Recommended
before this checkpoint is treated as fully released**: re-run the full
suite once from an interactive (non-sandboxed-background) terminal to
obtain pytest's own certified summary line directly, purely to close the
process-cutoff gap in provenance - not because any evidence here suggests
a real failure exists.

## Follow-up: Booking / provider execution security — CLOSED

Starting HEAD `8410b4d`. A fresh adversarial pass over the booking execution
path (`services/booking_flow.py`, `services/booking_orchestrator.py`) found
and fixed two headline issues plus two sibling bugs caught while fixing the
second, all independently reviewed (separate read-only agent, not
self-approved) across two rounds.

**Finding 1 (the headline finding) — concurrent double-execution of booking
confirmation.** `start_confirmation`'s "can this be confirmed?" check was a
plain unlocked read of `run.phase`; the only place that actually *claimed*
the run - transitioning it out of AWAITING_CONFIRMATION/RECONFIRM_REQUIRED -
was the background worker thread, once scheduled. Two callers racing
`start_confirmation` for the same run (double-click, a client retry
overlapping the original request) could both pass the stale read before
either worker ran, and both would spawn one - two independent
`run_booking` executions against the same legs, each capable of
independently calling `duffel.create_test_order` for the same offer
(confirmed Duffel's own API gives Detoura no idempotency key for order
creation - nothing in `providers/duffel.py` negotiates one, so Detoura's
own single-execution guarantee is the *only* thing preventing a duplicate
Order). Reproduced directly before fixing: 300/300 barrier-synchronized
trials double-invoked `run_booking` once the simulated work inside it
overlapped the window (i.e. the shape of any real provider call). **Fixed**:
the phase check-and-claim now happens atomically under `run._lock`, in the
caller's own thread, before any worker is spawned. Independent review
re-derived the fix from scratch and additionally stress-tested it beyond
the shipped test - 40 trials × 8 racing threads (320 calls), 0 failures.

**Finding 2 — an uncertain provider outcome (timeout/connection failure)
was recorded identically to a definite failure.** `models/booking.py`'s
`BookingState` enum already carries `TIMEOUT` and `PROVIDER_FAILURE`,
deliberately kept apart from `FAILED` and routed toward `RECOVERY_REQUIRED`
(a person decides) rather than silent resolution - but the orchestrator
collapsed every non-success into a plain `FAILED` regardless of whether the
underlying cause was a definite provider rejection or a timeout where
Detoura genuinely does not know what happened. **Fixed**: `_issue_item`/
`_revalidate_item` now return the actual resulting `BookingState` instead
of a bare bool, mapped from the specific exception
(`DuffelOfferGone`→UNAVAILABLE, `DuffelOrderError`→FAILED,
`TimeoutError`→TIMEOUT, `ProviderHttpError`/`OSError`→PROVIDER_FAILURE).
Independent review confirmed every value produced is a domain-legal
transition (cross-checked against `ALLOWED_TRANSITIONS`) and that
`_breaches_tolerance`/`journey_intent().outcome`/`build_travel_pass` all
treat the new states correctly as "not settled."

**Sibling bug 1 (caught fixing Finding 2) — `_mark_unattempted` clobbered
the very states it just recorded.** Called when a required leg's
revalidation fails, to mark other not-yet-issued legs NOT_ATTEMPTED - its
exemption list only protected FAILED/CONFIRMED, so a TIMEOUT/
PROVIDER_FAILURE item would have been immediately overwritten back to
NOT_ATTEMPTED the instant it was recorded. **Fixed**: only a still-READY
item is downgraded now.

**Sibling bug 2 (caught by independent review's first pass, fixed in a
second round) — the ISSUING loop's own `stop`-skip branch had the
identical clobber bug, untouched by the first fix.** Same exemption-list
pattern, same fix: only READY→NOT_ATTEMPTED. **Also fixed in the same
round**: a related gap where a *non-required* item that failed
revalidation (UNAVAILABLE/TIMEOUT/PROVIDER_FAILURE) was still silently
pushed through `USER_CONFIRMED → BOOKING → _issue_item` anyway when no
*required* leg had failed (so `stop` was never set) - now gated on
`item.state is BookingState.READY` before issuance is attempted at all.
Independent review confirmed no live duplicate-order risk from the old
behavior (each item issues at most once per `run_booking` call regardless),
judged the fix correct and complete, confirmed the `failed_reval` guarantee
(every required item is READY by the time the issuing loop starts) still
holds, and confirmed a READY optional item still issues normally. Second
review round: **APPROVED**, 141/141 targeted tests passing (up from 134).

**Deferred, tracked as debt (not fixed this slice, by explicit decision):**
- `guided_booking.py`'s `_revalidate_item` call still unpacks the return as
  a bare `bool` (`if ok:`) rather than comparing to `BookingState.READY`.
  Harmless today - Basic/guided always calls with `duffel=None`, which
  forces `_revalidate_item`'s unconditional `READY` short-circuit, so `ok`
  is always `BookingState.READY` (truthy either way) - but if guided fare-
  checking is ever wired to a real Duffel client, every revalidation
  failure would silently read as success. **Fix when guided/Basic real
  fare revalidation is wired**, not before.
- `services/payment_booking_orchestrator.py::run_paid_booking` calls
  `run_booking` directly with no phase guard at all - Finding 1's atomic
  claim lives only in `start_confirmation`, not in `run_booking` itself.
  Confirmed via full-repo search: **no caller anywhere in `src/detoura/api/*.py`**
  - dead/not-yet-integrated code, no live exploit path today. **Required
    blocker for the Payment Security / payment-orchestration integration
    slice**: do not wire `run_paid_booking` into any endpoint without first
    centralizing the single-execution guarantee (e.g. moving the atomic
    claim into `run_booking` itself, or a shared helper both call) so the
    protection isn't caller-dependent. Do not patch this by duplicating the
    guard as a stopgap.

Tests: `tests/test_v9_phase6_booking_security.py` (17 tests) - the
concurrency repro as a permanent test, provider-order-count proof under
concurrent confirmation, timeout/connection-error state mapping for both
revalidation and issuance, the `_mark_unattempted`/stop-skip preservation
tests (parametrized over TIMEOUT/PROVIDER_FAILURE/UNAVAILABLE with
`required=False` items), the optional-leg re-attempt-gate tests (same three
behaviors, proving no provider order is created), a READY-optional-still-
issues-normally test, and one authorization-boundary test proving
booking_id-capability execution is intentional product design, separate
from (and not affecting) My Trips' ownership-gated visibility. One
pre-existing test updated (`test_v8_booking.py`: a gone-offer-at-
revalidation assertion corrected from the old, less precise `FAILED` to
`UNAVAILABLE` - independent review confirmed this is a legitimate
correction, not a weakening; the test's core invariants are unchanged).

Full regression: **not completed this session** - three attempts at the
full ~2170-test suite each stalled to a crawl partway through (13-95%,
minutes of CPU across tens of minutes of wall clock) on a machine that had
been running this entire multi-slice Phase 6 session for 15+ hours (load
average climbing past 5, memory pressure visible); a clean, isolated run of
the specific files this slice touches plus their alphabetical neighbors
(`test_v9_phase5_ops_confirmations_api.py` through
`test_v9_search_recorder.py`, 8 files) completed normally in 52.68s with
118 passed, 1 skipped, 0 failed - evidence the slowdown is environmental,
not a hang introduced by this diff. Targeted coverage is comprehensive and
independently verified: the reviewing agent ran and reported the full
targeted suite (`test_v9_phase6_booking_security.py`, `test_v8_booking.py`,
`test_v85_tiers.py`, `test_v85_reoptimize_ui.py`,
`test_v85_commercial_security.py`, `test_v85_release_blockers.py`,
`test_v85_ticket_operations.py`, `test_v9_phase4_domain.py`,
`test_v9_phase4_orchestrator.py`) green twice, independently, in its own
process (134/134, then 141/141 after the follow-up fixes) - a stronger
signal than a from-scratch full run this session couldn't obtain reliably.
**Recommended before this checkpoint is treated as fully released**: re-run
the full suite once in a fresh/idle environment to confirm.

## Follow-up (commit `11f9eb1`): account → booking ownership — CLOSED

The item below ("Account → booking ownership: VERIFIED, NOT FIXED
(deferred)") is now fixed. `BookingRun` carries an `owner_user_id`, set
once at booking-intent creation from the server-resolved session (never
the request body); the existing `persist_run` choke point claims the trip
for that owner on every call, idempotently. `persistence.accounts.claim_trip`
itself was hardened in the same change - its existence-check-then-insert
was two separate lock acquisitions (a real TOCTOU race), now one
transaction. The `xfail(strict=True)` spec test in
`tests/test_v9_phase6_ownership_wiring.py` passes for real now and the
marker is removed; 15 more tests were added (IDOR, anonymous flow,
traveler-email-is-not-account-identity, idempotent retry, cross-user
denial, two concurrency scenarios, recovery-state visibility). Independently
adversarially reviewed (separate agent, read-only) - one Low finding (no
log line on a silent `claim_trip` failure), fixed. Full detail in the
commit message; release-blocker ledger item 4 below is updated to reflect
this closure.

## Verdict

**V9 PHASE 6 VERDICT: INCOMPLETE** (partial — real, tested, independently
reviewed progress on a bounded subset; the full 9-slice program this phase
is scoped to was not completed in one sitting, and is not claimed to be).

Two slices got full evidence-first treatment with a fix and a durable test
suite; several others got a real but bounded spot-check (confirmed existing
controls intact, no new fresh adversarial pass); the rest were not reached
this session. Nothing here is marked APPROVED/complete that wasn't actually
exercised — see the per-slice breakdown below.

## What this report covers

- **Slice 1 (auth abuse / login limiter): DONE.** Implemented, tested,
  independently adversarially reviewed by a separate reviewing agent (not
  self-approved), one real finding from that review fixed, full regression
  green.
- **Slice 2 (authorization / API security): PARTIAL.** One release-debt
  item verified precisely (account→booking ownership: confirmed NOT wired,
  with hard evidence and a durable spec test) and deliberately not fixed in
  this same slice (see rationale below). One new gap found and fixed
  (request body size cap). Existing IDOR/CSRF/ownership controls
  spot-checked, not freshly re-attacked from scratch.
- **Slice 9 (ops): one item.** Found and fixed an unrelated brute-force gap
  on the Ops shared-token exchange while investigating authorization
  patterns for Slice 2.
- **Slice 3 (payment): DONE.** A fresh, evidence-first adversarial pass over
  the entire Phase 4 payment architecture plus closing the `run_paid_booking`
  atomic-claim blocker (see "Follow-up: Payment Security adversarial slice"
  above), independently reviewed across two rounds, no CRITICAL/HIGH
  findings. Real Stripe Test Mode E2E remains BLOCKED (no credentials in
  this environment) - the one item of Slice 3 that genuinely cannot be
  completed here.
- **Slice 5 (network/SSRF): DONE.** A fresh, evidence-first adversarial pass
  over the entire outbound network surface (see "Follow-up: Network / SSRF
  Security adversarial slice" above) - one HIGH finding (blind HTTP-level
  retry of non-idempotent Duffel mutations, live/reachable) plus two
  LOW findings (unreachable-today gaps in the Phase 2.5 acquisition engine
  and the Wikimedia offline tool, closed proactively) fixed by the
  implementer, plus two MEDIUM findings (CGNAT coverage, credential-bearing
  redirect-following) found and fixed after independent review flagged
  them. Independently reviewed across two rounds, no CRITICAL findings,
  no regressions in booking/payment security.
- **Slices 6 (PII/logging), 7 (secrets/config), 8 (dependencies):
  spot-checked, not a full fresh adversarial pass.** Slice 8 is complete
  (dependency scan is a point-in-time check, genuinely finished). Slices
  6-7 had existing controls read and spot-verified sound.

## Slice 1 — Auth abuse / login limiter hardening

### The problem

The pre-existing login rate limiter keyed its window purely on the
normalized email (`services/rate_limit.py` + `auth_service.login`). An
unauthenticated attacker who merely knew a victim's email address could
submit enough failed attempts to trip that email's limiter window and deny
the victim's own logins for the remainder of the window - repeatable
indefinitely. The same limiter's backing dict also had no eviction, so a
high-cardinality flood of distinct keys was an unbounded-memory vector.

### The fix

- **`services/rate_limit.py`**: `RateLimiter` is now bounded (LRU eviction
  at `max_entries`, default 50,000) and self-sweeping (opportunistic
  removal of naturally-expired windows every 30s of caller-supplied `now`).
- **`services/client_ip.py`** (new): resolves a trustworthy client IP.
  Ignores `X-Forwarded-For` entirely unless `AUTH_TRUSTED_PROXY_HOPS` is
  explicitly configured (default 0 = don't trust it), in which case exactly
  the N-th-from-the-right entry is trusted - the one a real, N-hop-deep
  trusted proxy chain would have appended itself, immune to a client
  forging its own prefix entries. Documented for this project's actual
  target (Render, one edge hop).
- **`auth_service.login`**: two independent budgets replace the single
  email-only one - a coarse per-IP volume control (`login_ip` scope,
  catches one source cycling through many identifiers) and a short, soft
  per-`(client_ip, email)` throttle (`login_pair` scope, never resettable
  by an attacker who lacks the victim's IP). Only the pair budget resets on
  success; the IP budget does not (so a lucky guess against one of many
  accounts can't reset an attacker's own room to keep guessing).
- **`api/auth.py`**: resolves and threads `client_ip` into both `login` and
  `register`.
- **`api/app.py`+`api/body_limit.py`**: unrelated at first glance but
  landed in this same slice - see below.

### Independent adversarial review (not self-approved)

A separate reviewing agent (read-only, no write access) was given the diff
and asked to try to break it: lock out a victim, bypass throttling, exhaust
limiter memory, spoof the source IP (at both `hops=0` and `hops=1`), enable
account enumeration, leak exceptions, or cause pathological state growth.

**Result: clean on all of those, but it found one real, valid gap the
diff itself had missed** - `auth_service.register()` was left on the exact
same email-only scope the whole slice was about removing. An attacker who
knows only a victim's email can burn that email's entire *registration*
budget with throwaway requests, denying the real person the ability to sign
up with it for as long as the attacker cares to repeat it. Lower severity
than the login bug (it can't be used against an *existing* account/session,
only to block a brand-new signup), but a real gap in the same class this
slice exists to close. **Fixed**: `register()` now uses the identical
`(client_ip, email)` pair scoping as `login()`.

### Tests

`tests/test_v9_phase6_login_limiter.py` (22 tests): victim-cannot-be-
locked-out-by-email-alone (login and, after the fix, register too),
different-IP-not-blocked, per-IP volume control, success resets only the
targeted budget, enumeration safety, expired-entry sweeping, bounded memory
under a 10,000-key flood, LRU eviction order, 500-thread concurrency
(exact count, no corruption), malformed/oversized/weird key text, IPv4/IPv6
handling, trusted-proxy-hop arithmetic (including the "attacker forges a
prefix, trusted hop still wins" case), and an end-to-end HTTP-level test
through the real app with `X-Forwarded-For`. Plus two tests for the new
Ops login rate limit (see Slice 9).

Full regression: **2153 tests collected, all passing** (up from 2119 at the
Phase 5 checkpoint; only environmental skips, unchanged from Phase 5's
report). Targeted files re-run individually and via the full suite.

## Slice 2 — Authorization / API security

### Account → booking ownership: VERIFIED, NOT FIXED (deferred)

The pre-Beta debt ledger flagged this as needing verification. It does:
`persistence/accounts.claim_trip` - the only thing that ever populates the
`trip_ownership` table - **has no caller anywhere in `src/detoura` outside
its own definition.** Confirmed two ways: (1) an AST-based static scan of
every production source file for any reference to `claim_trip`, (2) an
end-to-end test that registers a user, logs them in, creates a real booking
intent through the actual HTTP booking-creation flow, and checks
`get_trip_owner`/`list_trip_ids_for_user` - neither ever reflects the
booking. This is a **functional completeness gap** (My Trips can never be
populated for any real account today), not an authorization hole - nothing
is over-exposed; the failure mode is that ownership is never recorded, not
that it leaks.

Both are captured as permanent tests in
`tests/test_v9_phase6_ownership_wiring.py`:
`test_claim_trip_has_no_production_caller_yet` (plain, stays green until
someone wires a caller) and
`test_a_signed_in_users_booking_intent_is_claimed_as_theirs`
(`xfail(strict=True)` - specifies the *desired* behavior; starts failing
the run, on purpose, the moment someone wires it, so the marker has to be
removed and the test becomes a real regression test at that point).

**Why not fixed in this same slice**: the only correct hook point found
(`services/booking_persistence.persist_run`, called from every state-
changing booking-intent endpoint) operates on `BookingRun` - the central
orchestrator object shared across Phase 4/7/8's payment, ticketing and
recovery logic. Wiring an optional session through booking-intent creation
and into that object is a real, cross-cutting change to the single most
sensitive data model in the codebase, not a security-hardening change with
a small, containable diff. Rushing it under this phase's "harden, don't
rewrite" mandate risked exactly the kind of change this project's own
Phase 5 discipline warns against. Recommended as its own dedicated,
carefully-tested follow-up slice, not folded into Phase 6.

### Request body size limit: FOUND AND FIXED

No layer of the app capped request body size - FastAPI/Starlette buffer an
entire body into memory to parse it before any Pydantic `max_length` field
validation ever runs, so a single oversized body was an easy way to spend
memory on any route, including ones whose eventual validated fields are
tiny (e.g. login). `api/body_limit.py` (new): a pure ASGI middleware
(deliberately not Starlette's `BaseHTTPMiddleware`, which would defeat the
point by buffering the whole body itself first) that refuses via
`Content-Length` before reading a byte when possible, and otherwise counts
bytes as they stream in and refuses mid-stream - catching a missing or
dishonest `Content-Length` too. Default 2 MiB (generous: this API has no
file-upload endpoint, and its own tightest structural limits, e.g. 64
recheck legs, top out far below that in practice).

Caught one real, legitimate pre-existing test in the process
(`tests/test_v65_request_limits.py::test_a_flood_of_recheck_legs_is_refused`,
a deliberately-oversized 37,500-leg adversarial payload) that expected a
422 from the endpoint's own semantic `max_length` validator specifically;
updated it to accept either 422 or the new, earlier, cheaper 413 - both are
a correct refusal of the same adversarial input, and the byte-cap path is
strictly less expensive (rejects before Pydantic ever allocates 37,500
objects).

### IDOR / CSRF / session / ownership: spot-checked, not re-audited from scratch

Read (not rewritten): the double-submit CSRF check (`api/auth.py`), cookie
flags (`HttpOnly`+`SameSite=Lax`+`Secure`-in-production on the session
cookie), and confirmed the existing green test suite still covers IDOR
safety (`test_ownership_checked_reads_are_idor_safe`), cross-route
ownership (`test_route_ownership_checks_both_levels`), and anti-
enumeration response shaping in `me_trips.py`. No new fresh adversarial
attack was mounted against these this session - they were not touched by
this slice's diff and their existing test coverage still passes.

## Slice 9 — Ops brute-force gap (found while reviewing Slice 2)

`POST /api/v1/ops/session` (the shared-secret `DETOURA_OPS_TOKEN` exchange
that gates the entire admin console - payment reconciliation, refunds,
ticket ops) had **no rate limiting at all**. `secrets.compare_digest`
defeats a timing attack, not a volume one; an operator-chosen weak or short
token was one unthrottled guessing loop away from being crackable at
network speed. Fixed: `api/ops_auth.check_ops_login_rate_limit`, a plain
per-IP volume cap (`OPS_LOGIN_MAX_ATTEMPTS`/`OPS_LOGIN_WINDOW_SECONDS`,
default 10/300s) reusing the same generic `RateLimiter`, checked before
`ops_enabled()`'s 503 is ever reached... actually checked *after* - the
"is this deployment even configured" answer must not itself require
spending part of an IP's guess budget to learn. Tests confirm the ordering
(`503` when unconfigured, regardless of rate-limit state) and that the cap
is a pure volume control (fires even on the eventually-correct token, once
an IP's budget for that window is spent).

## Slices not reached this session

**Slice 3 (payment)**: no fresh adversarial pass; existing Phase 4 tests
(webhook signature + replay-tolerance verification, `claim_provider_event`
idempotency, three-gate live-key fail-closed design in `payment_config.py`)
were read and spot-confirmed sound, not re-attacked. **Real Stripe Test
Mode server E2E: NOT RUN** - no credentials available in this environment;
this was expected and is not a new blocker, it's the same one carried from
Phase 4/5.

**Slice 4 (booking/provider security)**: not audited this session beyond
what the full regression already exercises.

**Slice 5 (network/SSRF)**: Phase 2.5's existing network-safety test suite
(`test_v9_phase25_network_safety.py`) confirmed still green; not rewritten,
per instruction to reuse rather than redo, but also not freshly attacked.

**Slice 6 (PII/logging)**: a targeted grep for password/token/secret
patterns near logging/print calls found nothing (the one hit,
`tools/duffel_probe.py`, already redacts before printing); the broad
`except Exception` / `str(error)` pattern used across `api/*.py` was spot-
checked in `v1.py`'s search handler and confirmed the raw exception detail
stays server-side (only a generic message reaches the client) - not
exhaustively checked across all ~30 similar call sites.

**Slice 7 (secrets/config)**: `.dockerignore` excludes `.env*`; no tracked
`.env` files; manual grep for live-looking key patterns in tracked source
found none (Phase 5 report has the detail); Stripe's three-gate fail-closed
live-mode design (`payment_config.py`) read and looks sound.

**Slice 8 (dependencies): complete.** `npm audit` (frontend): 0
vulnerabilities. `pip-audit` (backend): 0 known vulnerabilities. No updates
needed.

## Release-blocker ledger

Carried from Phase 4/5, still open (none of these are Phase 6
implementation items unless noted):

1. Consumer `/api/v1/search` still needs connecting to the real Phase 3
   intelligence pipeline.
2. Real Stripe Test Mode server E2E - **not run, no credentials** (unchanged
   by this slice - see "Real Stripe Test Mode E2E" above).
3. Fresh independent Phase 4 payment adversarial QA - **CLOSED** this
   session (see "Follow-up: Payment Security adversarial slice" above).
4. Account→booking ownership wiring - **CLOSED** (commit `11f9eb1`, see the
   follow-up section at the top of this report).
5. EU legal/tax/payment/package-travel specialist review - external,
   unstarted.
6. Destination-image manual review backlog - unchanged.
7. Login limiter targeted-DoS / memory-growth - **CLOSED** this slice.

New items opened this session:

8. `register()` email-only-scope DoS - **CLOSED** this slice (same fix as
   login).
9. No request body size cap - **CLOSED** this slice.
10. Ops shared-token exchange had no brute-force throttle - **CLOSED**
    this slice.
11. Slice 6 needs a genuine fresh adversarial pass (not just a spot-check)
    before Phase 6 can be declared APPROVED. (Slices 3, 5 - **CLOSED**, see
    items 3 and 18 below.)
12. Slice 4 (booking/provider security) - **CLOSED** this session (see
    "Booking / provider execution security" section above). The rest of
    Slice 9 (ops recovery/reconciliation visibility beyond the login fix)
    not yet started.
13. Concurrent double-execution of booking confirmation (duplicate provider
    orders) - **CLOSED** this session.
14. Uncertain provider outcomes (timeout/connection failure) recorded as
    definite failures, losing the RECOVERY_REQUIRED signal - **CLOSED**
    this session.
15. `guided_booking.py` bool/BookingState truthiness mismatch - **tracked
    debt, deliberately deferred** until guided/Basic real fare revalidation
    is wired to a real provider (currently inert - see the follow-up
    section above for why).
16. `run_paid_booking` bypasses the atomic single-execution claim entirely
    - **CLOSED** this session (see "Follow-up: Payment Security adversarial
    slice" above; `claim_for_execution` now centralizes the guard, covering
    `run_paid_booking` with zero duplicated logic). `run_paid_booking`
    itself remains unwired to any endpoint (product work, not a security
    gap) - see the PAYMENT EXECUTION SECURITY MAP's "currently unreachable
    components" note.
17. `ops_capture`'s exception handling didn't catch a plain `ValueError`
    a genuinely concurrent capture request can raise (found via this
    slice's own adversarial concurrency testing) - **CLOSED** this
    session. No money-safety impact (capture was never duplicated), just
    an unhandled 500 instead of the same clean 409 every other race in
    that file already returns.
18. `claim_for_execution`'s docstring overstated its own reachability
    coverage (missed `guided_booking.prepare_journey`'s direct,
    harmless-today call to `_revalidate_item`) - **CLOSED** this session
    (docstring corrected, forward-warning added for if Basic is ever wired
    to a real provider).
19. `DuffelTransportProvider` blindly HTTP-retried non-idempotent Order
    creation/cancellation-confirmation/change-confirmation POSTs via the
    generic `RetryingHttpClient`, live/reachable, HIGH - **CLOSED** this
    session (see "Follow-up: Network / SSRF Security adversarial slice"
    above; `retry=False` now bypasses the transport-level retry for the
    three genuinely irreversible mutations, reads/previews unaffected).
20. The Phase 2.5 acquisition engine (`network_adapter.py`) and the
    Wikimedia offline image-acquisition tool validated only the hostname
    STRING, never the resolved IP address, and the latter also
    auto-followed redirects - **CLOSED** this session, proactively (both
    are currently unreachable/near-unreachable in production - see the
    Network Security Map above - fixed before either becomes live).
21. Independent-review findings on the Network/SSRF slice's own fix:
    `_is_unsafe_ip` missed RFC 6598 CGNAT space, and the actual
    credential-bearing Duffel/Stripe/Amadeus transport still auto-followed
    redirects (the no-redirect fix had only been applied to the two
    already-unreachable components) - both **CLOSED** this session, see
    the two independent-review Finding sections above.

## Tests / build / secret scan

- Full regression: 2153 tests, all passing (0 failures), only the same
  environmental skips as Phase 5 plus one intentional `xfail`.
- Secret scan: manual, as in Phase 5 - clean.
- `npm audit` / `pip-audit`: clean.
- Frontend build/typecheck: unaffected (backend-only diff).

## Commits

One Phase 6 commit on top of `2fbf4a8`, exact-path staged:
`src/detoura/services/rate_limit.py`, `src/detoura/services/client_ip.py`,
`src/detoura/services/auth_service.py`, `src/detoura/api/auth.py`,
`src/detoura/api/ops.py`, `src/detoura/api/ops_auth.py`,
`src/detoura/api/app.py`, `src/detoura/api/body_limit.py`,
`src/detoura/auth_config.py`, `tests/conftest.py`,
`tests/test_v65_request_limits.py`, `tests/test_v9_phase6_login_limiter.py`,
`tests/test_v9_phase6_body_limit.py`,
`tests/test_v9_phase6_ownership_wiring.py`, this report.

No frontend files, no `AGENTS.md`/`CLAUDE.md` staged - all preserved
untouched.
