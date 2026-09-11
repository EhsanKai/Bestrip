# V9 Phase 2.5 — Authorized Market-Prior Acquisition

Branch `claude/travel-planner-mvp-nvb267`. Starting point: V9 Phase 2 approved
at `db961fd`. This phase adds the infrastructure that can safely populate the
Phase 2 Bootstrap Market Prior from an authorized external source — it does
not rebuild Phase 2, and the absolute boundary from Phase 2 still holds:

> **BOOTSTRAP PRIOR != LIVE PRICE != BOOKABLE OFFER.**

## Why this exists

Phase 2 built the *architecture* for a Bootstrap Market Prior but shipped with
`REAL BOOTSTRAP SOURCE: NOT CONFIGURED` — the sparse-prior domain model,
import pipeline, decay/confidence, opportunity scoring and ~200-city catalog
all worked, but nothing actually acquired data from anywhere but a fixture or
an operator-approved file. Phase 2.5 builds the missing half: a bounded,
authorized, resumable acquisition engine that can sit in front of that same
import pipeline — without weakening any of its guarantees, and without
scraping anything Detoura is not explicitly authorized to touch.

## Architecture

```
Authorized External Source
      |
MarketPriorSource-shaped Acquisition Adapter   (SourceFetcher protocol)
      |
Bootstrap Acquisition Job                       (market_prior_jobs)
      |
bounded tasks                                   (market_prior_tasks)
      |
fetch  ->  parse  ->  normalize  ->  validate
      |
the EXISTING, UNMODIFIED Phase 2 import boundary
(services/market_prior_import.normalize + persistence/market_priors.upsert_priors)
      |
BootstrapMarketPrior                            (unchanged from Phase 2)
      |
the EXISTING opportunity scoring / candidate funnel  (unchanged from Phase 2)
      |
EXPLOIT + EXPLORE shortlist  ->  bounded Duffel acquisition  ->  live offer / Price Memory
```

Everything below the "existing, unmodified" line is Phase 2 code, untouched.
Phase 2.5 only adds the layer that decides *what to acquire and how to fetch
it safely*, then hands the result to the same boundary Phase 2 already had.

### New modules

| Module | Role |
|---|---|
| `models/market_prior_acquisition.py` | `SourceType`, `AuthorizationStatus`, `SourceHealth`, `TaskStatus`, `JobStatus`, `StopReason`, `RateLimitPolicy`, `SourceRegistration`, `TaskCell`. |
| `persistence/market_prior_acquisition.py` | CRUD for `market_prior_sources` / `market_prior_jobs` / `market_prior_tasks`; atomic task claiming with stale-lease recovery; dedup against existing fresh `market_priors` coverage. |
| `services/bootstrap_planner.py` | The sparse planner: `plan_cells()` (pure), `dry_run()` (zero network, zero writes). |
| `services/network_adapter.py` | `AuthorizedHttpFetcher` — the only code path allowed to make an HTTP request for an `AUTHORIZED_WEB_SOURCE`/`API_SOURCE`. Builds on the existing V4 `RetryingHttpClient`/`RateLimiter` (`providers/http.py`), adds domain allowlisting, an SSRF guard, manual redirect validation, a response-size limit, and CAPTCHA/challenge detection. |
| `services/bootstrap_parser.py` | The parser contract: `TaskContext`, `SchemaChanged`, `validate_record_matches_task()`. |
| `services/bootstrap_fetchers.py` | Two `SourceFetcher` implementations: `FixtureSourceFetcher` (offline, deterministic) and `SimulatedAuthorizedWebFetcher` (goes through `AuthorizedHttpFetcher`, tested only against injected/stub HTTP clients — never a real domain). |
| `services/bootstrap_executor.py` | `run_job_slice()` — claim/fetch/parse/validate/import/mark-result, in a bounded loop, with every §16-§20/§27-§29 invariant enforced inline. |
| `services/bootstrap_registry.py` | The fixed, code-reviewed `source_id -> SourceFetcher factory` mapping. An Ops caller can never supply an arbitrary URL or fetcher. |
| `acquisition_config.py` | `AcquisitionConfig` (hard request budget, dedup freshness window, task lease seconds, response-size limit) + `ORIGIN_TIERS`. |
| `api/ops_market_prior_acquisition.py` | The Ops job-control surface (§35). |

## Source authorization model (§4)

`SourceRegistration.authorization_status` is one of `APPROVED` /
`REVIEW_REQUIRED` (default) / `PROHIBITED` / `DISABLED`. The single fail-closed
gate every network-capable adapter consults is
`SourceRegistration.network_allowed`:

```python
@property
def network_allowed(self) -> bool:
    if self.authorization_status is not AuthorizationStatus.APPROVED:
        return False
    if self.is_network_capable:            # AUTHORIZED_WEB_SOURCE / API_SOURCE
        if not self.base_domain:
            return False
        if self.robots_allowed is False:
            return False
    return True
```

Registering a source through the Ops API (`POST /sources`) always leaves
authorization at `REVIEW_REQUIRED`; only a separate, explicit, audited
`POST /sources/{id}/authorization` call can move it to `APPROVED`. The
software records that decision (`authorization_basis`, `reviewed_by`,
`reviewed_at`) — it does not conclude the decision is legally correct (§4).

A `robots_allowed=False` (an operational signal from a machine-readable
policy check — §9) blocks network access even for an otherwise-`APPROVED`
source, but is never itself treated as a complete legal authorization when
`True` or unset.

## The network adapter (§6, §29)

`AuthorizedHttpFetcher` wraps the existing `RetryingHttpClient`/`RateLimiter`
(unchanged, reused) with:

* **domain allowlist** — the URL host must equal or be a subdomain of the
  registration's `base_domain`; anything else raises `AccessBlocked` with
  `StopReason.DOMAIN_NOT_ALLOWED` before a request is even sent;
* **SSRF guard** — non-`https` schemes and IP-literal hosts are rejected the
  same way;
* **manual redirect validation** — `NonRedirectingHttpClient` disables
  `urllib`'s automatic redirect-following (which would otherwise re-request a
  possibly different host with no chance to validate it), so
  `AuthorizedHttpFetcher.fetch()` re-runs the full domain/SSRF check on every
  redirect hop and refuses to follow one off the allowed domain;
* **response-size limit** (`max_response_bytes`, default 2 MB);
* **CAPTCHA/challenge detection** — a body/status heuristic that raises
  `StopReason.CAPTCHA_DETECTED` rather than silently accepting a challenge
  page as data;
* **rate limiting** — `RateLimitPolicy.effective_min_interval_seconds` (the
  stricter of `requests_per_minute` and `min_delay_seconds`) feeds the
  existing `RateLimiter`; a 429 that survives the retry budget becomes
  `StopReason.RATE_LIMITED`, never a silent drop.

`max_concurrency` is enforced structurally: Phase 2.5 ships a **sequential**
executor (one task in flight at a time). This is a deliberate choice — the
design brief for this adapter is correctness, politeness, recoverability and
traceability, explicitly *not* maximum throughput — and it sidesteps having
to make `RateLimiter` thread-safe for a throughput gain nothing in Phase 2.5
needs yet. A future bounded worker pool can raise `max_concurrency` behind the
same `SourceFetcher` protocol without changing the executor's contract.

## Hard budget under concurrency, retries, and across processes (§16, §17, §59)

Two rounds of adversarial review found and closed real gaps here; the design
below is the final, fixed state.

### Round 1 — concurrent job slices could exceed the budget

The original executor read `job["requests_used"]`, compared it to
`request_budget`, and only bumped the counter *after* a task finished —
three separate, independently-locked database operations. Two concurrent
`run_job_slice` calls against the same job (a double-click on Ops "start", a
naive client retry, two operators) could each pass a now-stale check and
both proceed, driving `requests_used` past the configured ceiling (reproduced
concretely: budget 20 -> 26 actual requests under 8 concurrent slices).

Fixed by making the check-and-increment a single atomic statement —
`persistence.market_prior_acquisition.reserve_request()`:

```sql
UPDATE market_prior_jobs SET requests_used = requests_used + 1
WHERE job_id = ? AND requests_used < request_budget
```

A reservation that turns out not to be used (nothing left to claim, or the
source became blocked before any fetch) is given back by `release_request()`.

### Round 2 — the reservation had to move to the actual HTTP attempt, and rate limiting had to become persisted, not in-process

A further review raised two sharper points, both correct:

1. A single `reserve_request()` around one *logical* `fetch_one()` call is
   not sufficient when the HTTP layer retries internally —
   `RetryingHttpClient` can turn one task into up to four real outbound
   attempts, and each one is its own real request against the source. The
   fix moved budget reservation for `AUTHORIZED_WEB_SOURCE`/`API_SOURCE`
   fetchers down into the transport itself:
   `network_adapter._budget_gated_client` wraps the injected `HttpClient` so
   `reserve_request()` runs before *every* raw attempt — including every
   retry — and raises `BudgetExhausted` the instant the budget is gone,
   before a real request is sent. `BudgetExhausted` is deliberately **not**
   a `ProviderHttpError`, so `RetryingHttpClient` never retries around it —
   it propagates immediately, guaranteeing the *n*-th attempt where the
   budget runs out is the last one ever sent. Proven by
   `tests/test_v9_phase25_network_safety.py::test_retry_attempts_all_succeed_within_budget_three`
   and `::test_third_retry_is_never_sent_once_the_budget_is_two` — exactly
   the "budget 3 -> 3 attempts; budget 2 -> 2 attempts, third never sent"
   scenario. A task whose fetch is cut short this way has its claim given
   back to `PENDING` (`release_task_claim`) rather than losing or
   double-counting it. `FILE_IMPORT`/`MANUAL_DATASET` sources have no HTTP
   retry layer at all, so they keep the simpler per-task reservation in the
   executor — reserving in both places for those would double-charge.
2. The per-`source_id` lock added in round 1 was a `threading.Lock` — correct
   within one process, but two independent OS processes (two uvicorn
   workers, two separate job-control invocations) each hold their own lock
   object and would never see each other. Rate limiting had to become
   **persisted, shared state**, exactly like the request budget. Fixed with
   `reserve_rate_limit_slot()`: a `next_allowed_at` column on
   `market_prior_sources`, advanced atomically (read the current value,
   compute the next slot, write it back — all inside one `Database.write()`
   transaction) every time any caller, anywhere, wants to make a request for
   that source. The lock is gone entirely; correctness now comes from SQLite
   itself serialising writers to the same row, which works whether the two
   callers are two threads, two jobs, or two OS processes sharing the same
   database file.

   Making this work cross-process surfaced one more issue: `Database.write()`
   used a plain (deferred) `BEGIN`, which only takes SQLite's write lock at
   the first write statement — under two processes both doing
   read-then-write in the same transaction, this produced a reader-to-writer
   lock-upgrade race that `PRAGMA busy_timeout` does not reliably retry
   around (`sqlite3.OperationalError: database is locked`, reproduced by the
   cross-process test below). Fixed by switching to `BEGIN IMMEDIATE`, which
   takes the write lock up front — a second writer (same process or a
   different one) then just waits for the first to commit, exactly the
   semantics `busy_timeout` is built to retry. This is a `Database`-wide fix
   (every writer in the whole application benefits from it), not scoped to
   Phase 2.5, and the full repository suite was re-run to confirm nothing
   depended on the old deferred-transaction behaviour.

Proof, in increasing order of how hard it is to fake:

* `tests/test_v9_phase25_acquisition.py::test_concurrent_slices_never_exceed_the_hard_request_budget`
  (8 threads) and `::test_high_contention_budget_race_still_never_exceeds_the_budget`
  (25 threads, 3 slices each, a tighter budget) — the budget is hit exactly,
  never exceeded, and every succeeded task produced exactly one prior row.
* `tests/test_v9_phase25_network_safety.py::test_two_jobs_on_the_same_source_share_one_persisted_rate_limit`
  — two different jobs on the same source draw from one shared,
  monotonically-advancing slot sequence, not two independent ones.
* `tests/test_v9_phase25_network_safety.py::test_rate_limit_is_enforced_across_two_independent_processes`
  — two real `multiprocessing.Process` workers, sharing nothing but an
  on-disk SQLite file, still produce a combined reservation sequence that
  respects the configured minimum interval end to end.

Regression: `tests/test_v9_phase25_acquisition.py::test_concurrent_slices_never_exceed_the_hard_request_budget`
and `::test_concurrent_slices_completing_a_job_never_double_count_priors`
(8- and 6-thread races respectively), stable across repeated runs.

### Round 3 — a stale worker could stomp a task a different worker had legitimately reclaimed

A non-blocking hardening from the same re-review: `mark_task_result` and
`release_task_claim` originally scoped their `UPDATE` to
`task_id` (+ `status='RUNNING'` for the release), with no check on *which*
worker was calling. If a worker's lease expired mid-fetch (§19) and a second
worker legitimately reclaimed the same task via `claim_next_task`'s
stale-lease branch, the *first* worker's eventual, late
`mark_task_result`/`release_task_claim` call would still match
`task_id=? AND status='RUNNING'` — now the second worker's claim — and could
incorrectly revert or overwrite it. Not reachable under the default 120s
lease against this phase's worst-case retry timing (~43s), but a real gap
under an operator-shortened lease or a pathologically slow fetch.

Fixed by giving every `run_job_slice` invocation its own unique
`lease_owner` (a fresh random token per call — never the previous fixed
literal `"executor"`, which every concurrent invocation shared) and scoping
both functions' updates to `AND lease_owner=?` in addition to `task_id`
(and, for `release_task_claim`, `status='RUNNING'`). A stale worker's call
now matches zero rows — a silent, safe no-op — instead of touching a task
someone else legitimately owns. Both functions return whether the update
actually applied, so a caller can tell the difference if it needs to.
Regression: `tests/test_v9_phase25_acquisition.py::test_stale_worker_cannot_stomp_a_task_reclaimed_by_another_worker`
and `::test_run_job_slice_generates_a_unique_lease_owner_per_invocation`.

## Jobs and tasks (§10, §11, §18, §19, §27)

`BootstrapJob` (`market_prior_jobs`) tracks scope (origins/destinations/
horizons as JSON), a hard `request_budget`, and `requests_used`. Per-status
task counts (`succeeded`/`no_data`/`failed`/`blocked`/`cancelled`/`pending`/
`running`) are *derived live* from `market_prior_tasks` by
`job_counters()` rather than duplicated as a second, driftable set of
counters on the job row.

`BootstrapAcquisitionTask` (`market_prior_tasks`) is uniquely keyed on
`(job_id, origin, destination, horizon_bucket)` — planning the same job twice
(or after a crash) is a no-op the second time (`INSERT OR IGNORE`).

**Claiming is atomic** (`claim_next_task`): a `SELECT` of candidates followed
by a conditional `UPDATE ... WHERE task_id=? AND status=?`, exactly the
select-then-conditional-update-with-rowcount-guard pattern the existing Ops
ticket-claim code uses. A lost race just means the caller tries the next
candidate — never a double-claim.

**Stale-lease recovery (§19)**: a claimed task gets `lease_expires_at`. The
same `claim_next_task` query also matches a `RUNNING` task whose lease has
expired — a worker that died mid-task is picked back up by the next slice
invocation with no special "was this abandoned?" code path, and without ever
reclaiming a task some other still-live worker holds.

**Idempotency (§27)**: `run_job_slice` writes the parsed prior to
`market_priors` (an UPSERT-by-market-key) *before* marking its task terminal.
A crash between those two lines just means a stale-lease resume re-does an
idempotent upsert — never a lost result, never a doubled sample count. A task
already marked terminal is never reclaimed at all, so the ordinary path never
imports the same observation twice either. Proven in
`tests/test_v9_phase25_acquisition.py::test_resume_after_crash_never_double_counts_priors`
and `::test_hard_request_budget_stops_a_slice_and_job_resumes`.

## Fail-closed mid-run (§20)

Authorization is re-checked **before every task claim**, not just once at the
start of a slice — a source revoked while a job is mid-run stops the very
next task, not just the next job invocation
(`tests/test_v9_phase25_acquisition.py::test_authorization_revoked_mid_run_stops_before_the_next_task`).
`AccessBlocked` (from the network adapter) and `SchemaChanged` (from the
parser contract) both mark the offending task and immediately pause the job
with the exact `StopReason` recorded — the executor never retries around a
stop condition within the same slice.

## The sparse planner (§12, §13, §26)

`plan_cells(origins, destinations, horizons)` is a flat
`origin x destination x horizon-bucket` product — never a per-calendar-day
matrix. At `30 origins x 203 destinations x 6 horizons` this is ~36k cells,
planned in well under 100ms (`scripts/bootstrap_scale_dry_run.py`), with
execution separately bounded by the hard request budget. Horizon buckets here
are the same Phase 2 +14/+30/+45/+60/+90/+120-day intelligence buckets — they
are never turned into a live Duffel query date by anything in this module or
the executor.

## Origin tiers (§14)

`acquisition_config.ORIGIN_TIERS` (`TIER_1` = first 5, `TIER_2` = first 10,
`TIER_3` = all 30 of `search_intel_config.DEFAULT_BOOTSTRAP_ORIGINS`) is a
documented default grouping an operator can start a rollout from — the
planner itself takes whatever origin list it is given and has no business
logic baked in about tiers.

## Cost/economics (§16, §28, §34)

`AcquisitionConfig.max_requests_per_run` (default 50, `MARKET_PRIOR_MAX_REQUESTS_PER_RUN`)
is the hard ceiling one `run_job_slice` call may spend, checked before every
claim. `SourceRegistration.request_cost_minor` is `None` (UNKNOWN) unless a
source's pricing is actually known — `dry_run()`'s `estimated_cost_minor` is
`None` in that case, never `0`. This is tracked entirely separately from the
Duffel search-provider-call economics from Phase 1/2 (`services/provider_economics.py`)
— acquiring a Bootstrap Market Prior and searching Duffel for a live fare are
different economics with different cost models.

## Rollout plan (§36) — documented, not executed

| Stage | Scope | Gate before the next stage |
|---|---|---|
| A | 5 origins (`TIER_1`) x 25 destinations x 3 horizons | Manual review of `dry_run` output + a small real run's cost/quality once a source is `APPROVED` |
| B | 10 origins (`TIER_2`) x 100 destinations x sparse horizons | Same, at 10x the previous scale |
| C | launch-region origins x all 203 destinations | Cost review; confirm dedup is keeping re-acquisition low |
| D | broader European origin coverage (`TIER_3`+) | Ongoing refresh planning (below), not a one-off crawl |

No stage runs automatically; each requires an explicit Ops `POST /jobs` +
`POST /jobs/{id}/start` (or `/resume`) call, and every call is still bounded
by `AcquisitionConfig.max_requests_per_run`.

## Refresh planning (§15, §27 note)

Not implemented as an automatic background scheduler in Phase 2.5 (deliberately
— "no uncontrolled background crawling" carries over from Phase 2 §27). The
signals a future refresh planner should prioritize on are already visible
through the Ops API: `already_fresh_cells`'s dedup logic (never-observed vs.
stale), `job_counters` (under-sampled/failed markets), and the existing Phase
2 opportunity score / contribution history (high-opportunity, high-demand
markets). A scheduled refresh job is future work, not shipped here.

## Security (§29, §30)

* Only Ops configuration (`bootstrap_registry.py`, code-reviewed) defines what
  a source actually does — a consumer, and even an Ops API caller, can never
  submit "fetch this URL."
* SSRF: non-https and IP-literal hosts rejected; domain allowlist re-checked
  on every redirect hop.
* Response-size and redirect-count limits prevent resource exhaustion from a
  hostile or misbehaving response.
* No traveler PII (name/email/phone/DOB/passport/account id) is ever sent to
  or read from an acquisition source —
  `tests/test_v9_phase25_acquisition.py::test_no_pii_columns_in_acquisition_tables`.
* `market_prior_sources`/`jobs`/`tasks` are entirely separate from
  `bookings`/`booking_economics`/`audit_events`/`ticket_operations` — nothing
  here reads or writes those tables.

## Search independence (§28)

Nothing in `services/live_search.py`, `real_supply.py`,
`search_intel_recorder.py`, or the booking flow imports any Phase 2.5
acquisition module — proven by grep in
`tests/test_v9_phase25_acquisition.py::test_search_and_booking_paths_never_import_acquisition_modules`.
If every prior source is offline or unconfigured, a consumer search still
works exactly as Phase 2 already guaranteed: live Price Memory, the existing
prior (if any), and bounded EXPLORE for the rest.

## Observability (§31, §32)

`SourceHealth` is derived, not just "did the last HTTP call return 200":
`REVIEW_REQUIRED`/`DISABLED`/`BLOCKED` come straight from
`authorization_status`; otherwise recent jobs' `stopped_reason`s
(`CAPTCHA_DETECTED`/`ACCESS_DENIED`/`DOMAIN_NOT_ALLOWED` -> `BLOCKED`,
`RATE_LIMITED` -> `RATE_LIMITED`, `SOURCE_CHANGED` -> `SOURCE_CHANGED`) and a
>50% failed+blocked task rate across the last 5 jobs (`DEGRADED`) feed the
verdict, defaulting to `HEALTHY`. `GET /sources/{id}/health` and
`GET /sources` (which lists every source's health and
`has_fetcher_configured`) expose this to Ops.

## Real authorized external source

**REAL AUTHORIZED EXTERNAL SOURCE: NOT CONFIGURED.** Phase 2.5 ships exactly
one wired-up, `APPROVED` source: `fixture-europe-demo` — deterministic,
offline, no external network access at all. See
`docs/V9_MARKET_PRIOR_SOURCE_OPTIONS.md` for the category matrix a real
source would be evaluated against, and §38 of the original spec for why this
is an acceptable Phase 2.5 outcome (the complete framework, validated end to
end on fixture data, with no unauthorized network access anywhere).

## Known limitations

* `max_concurrency` is not yet backed by a real worker pool (sequential
  execution only) — see "The network adapter" above.
* No automatic background refresh scheduler (§15/§27) — refresh is an
  explicit Ops action against the same job/task machinery, not a cron job.
* `robots.txt` parsing itself (beyond the `robots_allowed` field a human/tool
  sets on the registration) is not implemented as an automated fetch-and-parse
  step in this phase; the field is read from and enforced correctly, but
  populating it today is a manual/Ops step, not yet automatic.
* Rate limiting and the request budget for network-capable sources are both
  persisted (`market_prior_sources.next_allowed_at`,
  `market_prior_jobs.requests_used`) and correct across threads, jobs, and
  OS processes sharing the database file — see "Hard budget under
  concurrency, retries, and across processes" above. `FILE_IMPORT`/
  `MANUAL_DATASET` sources still use the simpler per-task executor-level
  reservation, since they have no HTTP retry layer to gate.
* `max_concurrency` (bounded parallel *fetches*) is not yet backed by a real
  worker pool — the executor claims and processes tasks one at a time within
  a single `run_job_slice` call. Multiple *concurrent calls* to
  `run_job_slice` (multiple workers, multiple processes) are safe and
  budget/rate-limit-correct as described above; a single call does not
  itself parallelize its own task loop.
* No automatic background refresh scheduler (§15/§27) — refresh is an
  explicit Ops action against the same job/task machinery, not a cron job.
* `robots.txt` parsing itself (beyond the `robots_allowed` field a human/tool
  sets on the registration) is not implemented as an automated fetch-and-parse
  step in this phase; the field is read from and enforced correctly, but
  populating it today is a manual/Ops step, not yet automatic.
* Account Foundation (Part B) was deferred this phase — see the Phase 2.5
  final report for why and what remains for a future phase.
