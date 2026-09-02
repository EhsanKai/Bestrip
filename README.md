# Travel Intelligence Engine

A deterministic, multi-objective trip optimizer. It answers:

> *Given my money, my time and my preferences, what is the best trip you can build me?*

Not "find me a cheap flight". The optimizer prices and scores the **whole
trip** — the ride to the airport, the flights and trains, the hotel and how
good it is, the days you actually get to spend somewhere, and how well the
cities match the traveler — then searches the space of complete round trips for
the best one under the profile you ask for.

> ⚠️ **All data is synthetic.** Flights, trains, buses, hotel rates, ratings,
> inventory, airport transfers and destination metadata are fabricated to
> exercise the optimizer. Nothing here reflects real prices or availability.
> A real transport provider is implemented and tested (V4) but ships without a
> credential, so nothing in this repository makes a network call.

---

## Table of contents

- [What V4 changed](#what-v4-changed)
- [What V3 changed](#what-v3-changed)
- [The five traps](#the-five-traps)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [How the algorithm works](#how-the-algorithm-works)
  - [1. Origin discovery and ground transfer](#1-origin-discovery-and-ground-transfer)
  - [2. Beam search, and why it is not greedy](#2-beam-search-and-why-it-is-not-greedy)
  - [3. Accommodation](#3-accommodation)
  - [4. Constraints and admissible bounds](#4-constraints-and-admissible-bounds)
  - [5. Travel Value](#5-travel-value)
  - [6. The destination experience model](#6-the-destination-experience-model)
  - [7. Travel intensity](#7-travel-intensity)
  - [8. Usable destination time](#8-usable-destination-time)
  - [9. Pareto filtering](#9-pareto-filtering)
  - [10. Diversity filtering](#10-diversity-filtering)
  - [11. Baseline comparison](#11-baseline-comparison)
- [Candidate generation and complexity](#candidate-generation-and-complexity)
- [The adaptive beam](#the-adaptive-beam)
- [Inventory and availability](#inventory-and-availability)
- [Learning the profile weights](#learning-the-profile-weights)
- [Budget sensitivity](#budget-sensitivity)
- [Recommendation profiles](#recommendation-profiles)
- [Worked example](#worked-example)
- [Observability](#observability)
- [Configuration](#configuration)
- [The API](#the-api)
- [Synthetic data](#synthetic-data)
- [Plugging in real APIs](#plugging-in-real-apis)
- [Where an LLM fits](#where-an-llm-fits)
- [Running the tests](#running-the-tests)
- [Performance](#performance)
- [Known limitations](#known-limitations)
- [What a V5 would be for](#what-a-v5-would-be-for)

---

## What V4 changed

V3 closed with a list of six recommendations for V4 and a "ready for future
implementation, not implemented" table. V4 is that list, done. Each item below
names the V3 limitation it removes.

| V3 said | V4 does |
| --- | --- |
| *"the default beam demonstrably misses a better trip"* | **Adaptive beam**: widen while widening still pays. Finds the 0.6823 itinerary a fixed beam of 40 never reached. |
| *"rooms never sell out and flights never fill; there is no inventory model"* | **Inventory**: seat and room counts, two new rejection reasons, and refundability priced against scarcity instead of a flat bonus. |
| *"profile weights are a starting point, not a claim about real travelers"* | **Learned weights**: fit the nine weights from chosen-vs-shown feedback the planner already produces. |
| *"the HTTP clients and their response mapping"* are missing | **A real transport provider**, end to end: OAuth, retries, backoff, rate limiting, response mapping, currency normalization. Tested against recorded payloads. |
| *"a model call, and the prompt/guardrails around it"* are missing | **Both LLM seams**, Claude-backed, with the guardrails that make them safe to install. |
| *"the accommodation component cannot tell you whether a room was good value"* | **Value for money**: quality-per-euro against the cheapest room actually offered. A diagnostic, never a weighted component. |

Three things did **not** change, and that is deliberate:

- **The beam search is still the beam search.** Nothing was replaced.
- **The optimizer still contains no LLM**, and the AST test that enforces it
  now guards a package with a real model in it.
- **Every V3 number in this README still reproduces.** Adaptive beam and
  inventory simulation are both off by default, because turning either on
  changes which itinerary wins, and a benchmark you cannot reproduce is not a
  benchmark. Each is one config field.

**Two bugs V4 found in its own new code**, both fixed and both tested:

- **The prior did not out-vote thin evidence.** The weight fit averages its
  gradient over pairs, so two observations pushed exactly as hard as two
  hundred - and three clicks produced a *more* extreme profile than sixty. The
  prior is now scaled against the amount of evidence, which is what a
  fixed-strength prior against a summed likelihood would have done.
- **A missing exchange rate silently returned zero flights.** The real provider
  skipped offers it could not price, which is right for a malformed record and
  catastrophic for a misconfiguration: "nobody configured GBP" came out as
  "there are no flights". It now fails loudly.

---

## What V3 changed

V1 optimized a transport route. V2 optimized a trip's *cost*. V3 optimizes the
trip. The beam search, the constraint engine, the Pareto and diversity filters
and the non-greedy guarantee are all unchanged — what changed is how much the
optimizer understands about what it is choosing between.

| | V2 | V3 |
| --- | --- | --- |
| A destination | 5 attributes, a node in a graph | **12 attributes**, richness, recommended stay |
| Experience | stay length vs. recommended range | **quality × preference match × stay quality** |
| Preferences | 6 numeric weights | numeric weights **+ preferred / disliked experiences, previously visited, preferred city count, travel style** |
| A hotel | a price | price **+ rating + location + type + cancellation** |
| Room choice | always the cheapest | **branches across tiers**; paying more must earn its keep |
| Objective | 5 components | **9 components**, profile-weighted |
| City count | a saturating bonus | **CityCountFit**: derived from trip length and the catalog's own recommended stays |
| Travel effort | a transit-share term | **TravelIntensity**: transit share + movement rate + airport churn |
| Pareto frontier | 4 objectives | **8 objectives**, with grid (ε) dominance |
| Provider calls | uncounted | **cached and measured** — ~12,000 lookups collapse to ~0–100 upstream |
| Money | a float | **`Money` + `PriceBasis` + `PriceNormalizer`** at the provider boundary |
| "What if I had more?" | — | **budget sensitivity** analysis and endpoint |

**Two V3 findings worth calling out**, both fixed:

- **The return bound was inadmissible.** V2 rejected any state in a city with
  no *direct* flight home. A city reachable only onward (Rome via Madrid) was
  therefore pruned even though a route home existed — an overestimate of the
  true completion cost, and exactly what §22 forbids. An unknown bound now
  means *no pruning*.
- **Flexible dates could return a worse answer than fixed dates.** A flexible
  request searches a space `len(start_dates)` times larger; holding the beam at
  a constant width made it explore proportionally less of that space. The beam
  now scales with the number of start dates.

---

## The five traps

The project is built around five synthetic scenarios where the naive answer is
wrong. Each has a dedicated fixture and a test that fails if the optimizer takes
the bait.

**1. The cheap first leg** (V1, still passing)

```
             ┌→ London ─→ Brussels ─→ DUS      120   cheapest first leg, worst trip
Düsseldorf ──┤
             └→ Prague ─→ Vienna ───→ DUS       92   ← best complete itinerary
                  ↑
                  the *expensive* first leg
```

`tests/test_beam_search.py` implements a greedy cheapest-next-hop reference,
shows it picks the 120 route, and asserts the optimizer picks the 92 one.

**2. The cheap flight into the expensive city** (V2)

```
DUS → London  35/pp   +  London room 120/night   =  the dearer trip
DUS → Prague  55/pp   +  Prague room  40/night   =  the cheaper trip
```

Transport alone says London. The complete trip says Prague. Asserted in
`tests/test_v2_accommodation.py`.

**3. The cheap flight from the expensive airport** (V2)

```
Köln → DUS  30  +  DUS → London  35   =  65 one way
Köln → CGN   5  +  CGN → London  45   =  50 one way
```

The cheaper flight leaves from the airport that costs more to reach. Asserted
in `tests/test_v2_ground_transfer.py` — including that the itinerary with the
cheapest *flights* is not the itinerary with the cheapest *journey*.

**4. The cheap flight into the wrong city** (V3)

```
DUS → Zurich 30/pp  +  Zurich room 95/night  +  nature / adventure    → 183.50
DUS → Prague 55/pp  +  Prague room 40/night  +  history / architecture → 162.00
```

The traveler asked for history and architecture. The cheapest flight in the
network leads to the city that is dearer overall *and* a worse match: Zurich
costs €21.50 more all-in and scores 0.71 on experience against Prague's 0.81.
A greedy cheapest-first-leg search takes Zurich; the optimizer takes Prague.
Asserted in `tests/test_v3_adversarial.py`, together with a control run where
equal hotel rates send CHEAPEST straight back to Zurich — which proves the win
came from the model, not from a rigged network.

**5. Four cheap cities against two civilised ones** (V3)

```
Berlin + Prague                    2 cities,  3.4h in transit,  122.00 floor
Berlin + Prague + Rome + Madrid    4 cities, 18.2h in transit,  214.00 floor
```

Every hop is cheap, and greedy-cheapest-next-hop chains all four. Both
BEST_VALUE (0.7479) and ADVENTURE (0.7316) take the two-city trip at an
intensity of 0.032, with the four-city loop discovered, affordable and rejected.
Intensity is what stops "more cities" from becoming "four airports in four
days" — and note that ADVENTURE, the profile that most wants cities, still
refuses this one.

---

## Quick start

```bash
pip install -e ".[dev]"

pytest                                            # 654 tests
python examples/v4_capabilities.py                # the six V4 additions
python examples/v3_scenarios.py                   # A/B/C x three profiles
python examples/v3_scenarios.py --budget-sensitivity
python examples/v3_scenarios.py --baseline        # their idea vs. ours
python examples/profiles_demo.py                  # the V2 comparison
python examples/koln_scenario.py --debug          # one profile, full search trace

uvicorn travel_planner.api.app:app --reload       # http://127.0.0.1:8000/docs
```

As a library — no FastAPI required:

```python
from datetime import date
from travel_planner import TravelPlanner, TripRequest, TravelPreferences
from travel_planner.profiles import ProfileName

planner = TravelPlanner()          # synthetic transport, hotels and transfers
result = planner.plan(
    TripRequest(
        origin="Köln",
        budget=450, travelers=2, duration_days=5,
        date_from=date(2026, 9, 10), date_to=date(2026, 9, 15), date_flexible=True,
        transport_preferences=["flight", "train"],
        preferred_destinations=["Madrid"],
        avoid_destinations=["Paris"],
        preferences=TravelPreferences(history=0.8, culture=0.8, multiple_cities=0.9),

        # V3, all optional
        preferred_experiences=["culture", "history", "food"],
        disliked_experiences=["nightlife"],
        previously_visited=["Amsterdam"],
        preferred_city_count=2,
    ),
    profile=ProfileName.ADVENTURE,     # or BEST_VALUE (default), or CHEAPEST
)

for itinerary in result.recommendations:
    print(itinerary.rank, itinerary.route_label(), itinerary.total_cost)
    print("  ", itinerary.cost_breakdown)
    print("  ", itinerary.value_breakdown)          # nine components + weights
    print("  ", itinerary.destination_insights)     # why each city
    print("  ", [f.value for f in itinerary.explanation_factors])
```

Every provider is injectable:

```python
planner = TravelPlanner(
    transport_provider=my_transport,
    destination_provider=my_destinations,
    accommodation_provider=my_hotels,
    ground_transfer_provider=my_transfers,
    config=PlannerConfig(beam_width=40, max_cities=3),
)
```

---

## Architecture

```
travel_planner/
├── config.py                  PlannerConfig — every tunable number
├── profiles.py                ProfileName, TravelValueWeights, PROFILES
├── usable_time.py             the usable-day model
├── models/
│   ├── trip.py                TripRequest, TravelPreferences, TravelStyle,
│   │                          AccommodationPreference
│   ├── transport.py           TransportOption, TransportType
│   ├── accommodation.py       AccommodationOption, Tier, Type, rating_from_stars
│   ├── transfer.py            GroundTransferOption
│   ├── destination.py         Destination — 12 attributes + richness      (V3)
│   ├── money.py               Money, PriceBasis, PriceNormalizer          (V3)
│   ├── search.py              SearchState, CityStay
│   ├── itinerary.py           Itinerary, CostBreakdown, TravelValueBreakdown,
│   │                          StaySummary, DestinationInsightSummary,
│   │                          ExplanationFactor, PlanResult
│   └── debug.py               RejectionReason, IterationDebug, SearchDebug
├── data/
│   ├── destinations.py        the city catalog + origin-airport distances
│   ├── synthetic_transport.py the transport network
│   ├── synthetic_accommodation.py  rates, tiers, ratings, locations
│   └── ground_transfers.py    origin ↔ airport table
├── learning.py                fitting profile weights from choices        (V4)
├── providers/
│   ├── http.py                HttpClient, retries, backoff, rate limits  (V4)
│   ├── amadeus.py             a real transport provider, end to end      (V4)
│   ├── transport.py           TransportDataProvider  + Synthetic / Real
│   ├── accommodation.py       AccommodationDataProvider + Synthetic / No / Real
│   ├── ground_transfer.py     GroundTransferProvider + Synthetic / Free / Real
│   ├── destinations.py        DestinationProvider
│   └── cache.py               caching decorators + call metrics           (V3)
├── algorithms/
│   ├── beam_search.py         the state-space search
│   ├── experience.py          ExperienceEngine, DestinationInsight        (V3)
│   ├── accommodation_value.py AccommodationScorer                         (V3)
│   ├── intensity.py           IntensityScorer                             (V3)
│   ├── travel_value.py        the nine-component objective
│   ├── scoring.py             the V1 engine, still used for its primitives
│   ├── pareto.py              domination, now over eight objectives
│   └── diversity.py           Jaccard diversification
├── constraints/validator.py   ConstraintValidator + the estimator protocols
├── services/
│   ├── planner.py             TravelPlanner — the entry point
│   ├── baseline.py            the naive single-destination round trip
│   ├── origin_resolver.py     "Köln" → CGN, DUS, FRA, EIN
│   ├── return_estimator.py    admissible bound on getting home
│   ├── accommodation_estimator.py  admissible bound on sleeping
│   ├── budget_sensitivity.py  "what would more money buy?"                (V3)
│   └── explanation.py         structured explanation factors
├── llm/
│   ├── interfaces.py          PreferenceParser / ItineraryExplainer seams
│   ├── client.py              LlmClient, AnthropicClient, ScriptedClient  (V4)
│   ├── explainer.py           Claude explainer + numeric grounding guard  (V4)
│   └── parser.py              Claude parser + schema, retry, fallback     (V4)
└── api/                       FastAPI adapter (optional)
```

Dependency direction is strict and enforced by structure:

```
api → services → algorithms → constraints → provider interfaces → models
```

`usable_time.py`, `profiles.py` and `money.py` sit at the root because `config`
and `models` need them. Provider *implementations* only ever satisfy the
protocols; no algorithm imports one. A test parses the ASTs of `algorithms/`,
`constraints/`, `services/`, `providers/` and `data/` and fails if any of them
imports `travel_planner.llm`.

---

## How the algorithm works

State-space search + multi-objective optimization + constraint satisfaction +
route diversification. Not flight search.

```
TripRequest (+ profile)
     │
     ▼
OriginResolver ────► CGN, DUS, FRA, EIN        candidate departure airports
     │
     ▼
GroundTransferProvider ──► the ride to each one, priced and timed
     │
     ▼
Beam search  ◄──►  TransportDataProvider        what can I board?
     │       ◄──►  AccommodationDataProvider    what will the nights cost?
     │       ◄──►  ConstraintValidator          is this still legal?
     │       ◄──►  TravelValueScorer            how good is the finished trip?
     ▼
Completed round trips
     │
     ▼
Pareto filter        drop dominated itineraries (8 objectives, ε-dominance)
     │
     ▼
Duplicate collapse   the same route at a different hour is one trip
     │
     ▼
Diversity filter     drop near-duplicate city sets (Jaccard)
     │
     ▼
Top N + baseline comparison + explanation factors + destination insights
```

### 1. Origin discovery and ground transfer

The origin is a *region*, not an airport. `OriginResolver` returns departure
nodes within `max_origin_distance_km` (default 250 km):

| Airport | km from Köln | transfer | in range at 250 km |
| ------- | ------------ | -------- | ------------------ |
| CGN     | 15           | €5 / 20 min  | ✅ |
| DUS     | 40           | €15 / 55 min | ✅ |
| FRA     | 155          | €30 / 90 min | ✅ |
| EIN     | 170          | €22 / 130 min | ✅ |
| AMS     | 260          | €42 / 175 min | ❌ (raise the radius) |

Transfers are priced **per person** and charged both ways. The initial search
state is pre-charged for the ride out; the return leg charges the ride home
from whichever airport the trip lands at — which may be a different one.
`total_travel_minutes` stays intercity-only for continuity;
`total_transport_minutes` adds the transfers.

### 2. Beam search, and why it is not greedy

One iteration adds one leg. From any state there are two actions: **continue**
to an unvisited city, or **return** to an origin airport. Stay lengths are
searched, not assumed, and each is paired with the rooms that stay would need.

```
initial states (airport × start date, ground transfer pre-charged)
        ↓
generate next states (continue / return × stay length × timetable × room tier)
        ↓
validate constraints          ← rejections recorded with a typed reason
        ↓
estimate Travel Value         ← optimistic completion, see below
        ↓
rank, keep top `beam_width`   ← pruned states recorded with their estimate
        ↓
repeat, up to max_cities + 1 legs
```

**Why it is not greedy.** Partial states are ranked by an *optimistic
completion*: the state's cost and time, **plus the cheapest way home, plus the
ride from that airport, plus the nights it must still pay for**. A state
sitting in London with an €85 return and a €78 room is judged on the trip it
implies, not on the €35 leg that got it there.

**Beam width scales with the start dates.** A flexible-date request searches a
space `len(start_dates)` times larger than a fixed-date one. V2 held the beam
constant and could therefore return a *worse* answer for a more flexible
request — indefensible from the user's point of view, and now fixed
(`scale_beam_with_start_dates`, capped by `max_effective_beam_width`).

**Beam spread.** At most `beam_slots_per_route` slots go to any one city
sequence, so the beam does not fill with the same trip from four airports.

**Determinism.** Sorted iteration everywhere; ties break on cost, then on the
state's leg-id signature. Asserted per profile, three runs deep.

### 3. Accommodation

Prices are **per room per night**; a room sleeps `capacity` people and the
search books `ceil(travelers / capacity)` of them. Three tiers exist in every
city, and each buys real quality:

| tier | price | rating | location | type | sleeps | cancellation |
| --- | --- | --- | --- | --- | --- | --- |
| budget | ×0.65 | 3.3★ | 0.55 | hostel | 2 | no |
| standard | ×1.00 | 4.1★ | 0.72 | hotel | 2 | yes |
| comfort | ×1.55 | 4.7★ | 0.90 | boutique | 3 | yes |

Price rises faster than quality on purpose: the top tier costs 2.4× the budget
one and is not 2.4× better, so "pay more" is a decision the optimizer has to
justify rather than a free upgrade.

V2 always booked the cheapest sufficient room, so that decision did not exist.
V3 branches across `accommodation_options_per_stay` tiers (default 2).

**AccommodationScore is quality only — price is deliberately excluded.** Cost
already has its own component; scoring price twice would bias the trade rather
than model it. The result is that a €120 4.8★ room does not automatically beat
a €60 4.4★ one: it wins only when the quality gain outweighs what the extra €60
costs on the budget axis. `tests/test_v3_accommodation_value.py` walks the
break-even and asserts monotonicity across it.

**Was the upgrade worth it?** (V4) That is the one question quality-only
scoring cannot answer, and V3 listed it as a known gap. `value_for_money`
compares the premium paid against the quality it bought, relative to the
cheapest room the provider *actually offered* for the same stay — because that
is the alternative the traveler really had:

```
Prague     standard  paid 148.50  cheapest offered  96.51  premium 51.99  → worth it
Berlin     budget    paid 153.72  cheapest offered 153.72  premium  0.00  → nothing traded
```

`0.5` is neutral: either no premium was paid, or it bought exactly the going
rate. The reference rate is **measured, not guessed** — it is the rate at which
the whole tier ladder trades quality for money for a balanced traveler, which
makes budget→comfort land on neutral by construction and then says something
useful about the halves: budget→standard scores **0.89**, standard→comfort
**0.27**. The first step up is good value and the second is not, and a single
"room quality" number cannot tell those apart.

It is a **diagnostic, never a weighted component** — a test asserts
`value_for_money` is not in `COMPONENTS`. Putting it in the objective would
score price for a third time.

### 4. Constraints and admissible bounds

All feasibility rules live in `ConstraintValidator`. Each failure returns a
typed reason: `BUDGET_EXCEEDED`, `DURATION_EXCEEDED`, `DURATION_UNDERUSED`,
`UNAFFORDABLE_ACCOMMODATION`, `NO_ACCOMMODATION_AVAILABLE`,
`AVOIDED_DESTINATION`, `MISSING_MANDATORY_DESTINATION`,
`DUPLICATE_DESTINATION`, `MIN_CITY_STAY_VIOLATED`, `MAX_CITIES_EXCEEDED`,
`TRANSPORT_TYPE_NOT_ALLOWED`, `DATE_WINDOW_VIOLATED`, `NOT_RETURNED_TO_ORIGIN`,
`UNREACHABLE_RETURN_BUDGET`, `UNREACHABLE_RETURN_TIME`, `INVALID_CONNECTION`.

**Every pruning bound must be admissible** — it must never overestimate the
cheapest or fastest way to finish, or the search will silently delete good
trips. Three bounds are used, and each has a test asserting no bookable option
undercuts it:

| bound | source | admissible because |
| --- | --- | --- |
| cheapest way home | `CachedReturnEstimator` | minimum over every airport and every date in the window |
| cheapest remaining nights | `CachedAccommodationEstimator` | cheapest tier at the un-surcharged base rate |
| cheapest ride home | the transfer table | minimum over the candidate airports |

`tests/test_v3_adversarial.py::test_pruning_never_removes_a_reachable_itinerary`
runs the search twice, with bounds on and off, and asserts the pruned run finds
a superset of the unpruned one.

**Preferred vs. mandatory.** `preferred_destinations` raises the score;
`must_visit` is hard and steers the search itself. **Avoided destinations are
rejected, never merely penalized.**

**Dates.** The window `date_from … date_to` and the trip length `duration_days`
are separate: a 5-day trip in a 10-day window is 5 days, not 10. With
`date_flexible = true` every start date that fits is searched; with `false`,
only `date_from`, and the planner never silently shifts it. Duration is
measured as the trip's **span** — first departure to last arrival.

### 5. Travel Value

Nine components, each in `[0, 1]`, weighted by the active profile. The first
five keep their V2 names for backward compatibility.

| component | spec name | what it measures |
| --- | --- | --- |
| `cost` | BudgetEfficiency | money left over, blended with sensible use of the budget |
| `experience` | DestinationExperience | quality × preference match × stay quality |
| `preferences` | PreferenceMatch | taste match + wish-list coverage |
| `time` | TimeUtilization | usable destination time vs. time in transit |
| `diversity` | Diversity | countries, transport modes, number of places |
| `city_count` | CityCountFit | distance from the right number of cities |
| `accommodation` | AccommodationQuality | rating, location, type fit, cancellation |
| `convenience` | Convenience | leg count, mode comfort, hop length |
| `intensity` | TravelIntensity | how much of the trip is spent moving |

**CostScore** blends efficiency with a *saturating* utilization term:

```
utilization  = total_cost / budget
efficiency   = clamp(1 - utilization)
sensible_use = clamp(utilization / budget_utilization_target)   # target 0.6
CostScore    = (1 - w) · efficiency + w · sensible_use          # w = profile's
```

With `w = 0` (CHEAPEST) this is pure efficiency and strictly monotone: cheaper
always wins. With `w = 0.35` (BEST_VALUE) spending up to ~60% of the budget
costs almost nothing, so a better trip is free to be dearer.

**CityCountFit** derives the right number of cities rather than hard-coding a
table:

```
per_city  = mean recommended stay + city_change_overhead_days
ideal     = round(duration_days / per_city)  ± appetite  + profile bias
ceiling   = duration_days // (shortest recommended stay + overhead)
```

The ceiling is what stops the appetite shift and the profile bias compounding
into "four cities in five days". An explicit `preferred_city_count` overrides
all of it — if the traveler insists, intensity and experience will price it.

### 6. The destination experience model

Twelve normalized attributes per city — history, nature, nightlife, culture,
food, architecture, shopping, museums, beaches, family friendliness, romance,
adventure — plus a recommended stay range and a derived `richness`.

```
ExperienceScore = destination_quality × preference_match × stay_quality
```

A weighted **geometric** mean, because the product form is the point: a
wonderful city you have no time in is not a wonderful experience, and neither
is a perfectly-timed stay somewhere the traveler dislikes. Each factor is
floored so one weak dimension drags hard without annihilating the rest.

**Preferences arrive through several channels**, resolved into one weight map:

- numeric `preferences` (the V1 weights, plus seven optional V3 ones),
- `preferred_experiences` — raises those attributes to full weight,
- `disliked_experiences` — *subtracts*, so a party city ranks below a quiet one
  for someone avoiding nightlife rather than merely level with it,
- `previously_visited` — a known city keeps 75% of its value, so novelty
  competes fairly.

Every city produces a `DestinationInsight` naming its strengths, weaknesses,
any disliked attributes present, and how the stay length compares to the
recommendation. That is what a "why this destination?" panel renders.

### 7. Travel intensity

Two trips can cost the same, last the same five days, and feel completely
different. Intensity is three signals kept separate so they stay explainable:

- **transit share** — transport time over trip length (zero above 15%),
- **movement rate** — legs per day,
- **airport churn** — flights and transfers, each a check-in that duration
  alone never captures.

It is *not* an anti-multi-city penalty: ADVENTURE weights diversity and city
count high enough to pay for a reasonable amount of movement. What intensity
stops is travel that buys nothing. `travel_style` scales how much the traveler
minds — a RELAXED traveler minds more than a PACKED one, and the raw intensity
is unchanged either way.

### 8. Usable destination time

A day of a trip is not a day of sightseeing. Each calendar day contributes the
overlap between the traveler's presence and a configurable usable-day window
(default 08:00–21:00): arriving at 23:30 buys nothing, a 06:00 flight home
costs the whole last morning, and a full day in between is 780 minutes.

### 9. Pareto filtering

An itinerary is **dominated** when another is no worse on every objective and
strictly better on at least one; dominated itineraries are inferior under *any*
weighting, so they are removed before ranking. V3 widened the frontier from four
objectives to eight:

| objective | direction | grid resolution |
| --- | --- | --- |
| total cost | ↓ | €5 |
| total transport time | ↓ | 30 min |
| city count | ↑ | 1 |
| preference score | ↑ | 0.02 |
| usable destination time | ↑ | 60 min |
| experience score | ↑ | 0.02 |
| accommodation score | ↑ | 0.03 |
| convenience | ↑ | 0.03 |

Eight objectives has a well known failure mode: almost nothing dominates
anything, the "frontier" swallows the candidate set, and the filter stops
filtering. That is what the third column is for. V3 compares on a **quantized
grid** (ε-dominance): each objective is rounded to a resolution that reflects
what a traveler can actually perceive before dominance is tested. Differences
below that resolution are ties, so a trip that is €1.40 cheaper and four minutes
faster no longer earns a place on the frontier.

Measured on scenario B — 786 complete itineraries:

```
exact dominance     786 → 248
grid dominance      786 → 124
```

Half the frontier was rounding noise. The grid lives in `OBJECTIVE_RESOLUTION`
(`algorithms/pareto.py`) next to `OBJECTIVE_DIRECTIONS`; setting a resolution to
`0` opts that objective back into exact comparison.

Travel intensity is deliberately **not** a Pareto objective. It is already
implied by transport time and city count, and adding a ninth correlated
objective would inflate the frontier without adding information.

### 10. Diversity filtering

Two passes, because "the same trip twice" has two different meanings.

**Exact route duplicates** are collapsed first (`FilterStage.DUPLICATE_ROUTE`).
The same airports, cities and stay lengths on a different departure time is one
trip presented twice, and it survives Pareto easily — a €3 price difference is
a real difference to a dominance test and no difference at all to a traveler.

**Near-duplicates** are then removed by Jaccard similarity of the city set: an
itinerary is accepted only if its similarity against every already-accepted one
stays at or below the threshold (0.5, or 0.6 under ADVENTURE). If that is too
strict to fill `max_results`, the remaining slots are back-filled in rank order
rather than returning a short list — fewer results is a worse answer than
similar ones.

### 11. Baseline comparison

The cheapest simple round trip to the user's first preferred destination —
what a conventional search would return — **including its hotel and airport
transfers**, and long enough to satisfy `min_duration_utilization`. Without
that last rule the baseline degenerates into a one-night flying visit that
undercuts every real trip and makes `money_saved` meaningless.

No preferred destination means **no baseline**. The planner does not invent one.

---

## Candidate generation and complexity

Beam search bounds how many *states* survive each round. It does not bound how
many are *generated*, and in V3 that number grew: every stay now branches over
room tiers as well as destinations, transport options and stay lengths.

The branching factor at each extension is

```
destinations × transport options × stay lengths × accommodation options
```

With the 16-city catalog, four transport options per leg, up to four stay
lengths and two room tiers, one beam state expands into roughly
`15 × 4 × 4 × 2 ≈ 480` candidates, and a 40-wide beam therefore evaluates on the
order of 19,000 per iteration. The measured numbers agree: scenario B generates
~12,000 states across four iterations and rejects ~9,700 of them.

Four caps keep that finite and configurable, and every one of them defaults to
"no artificial limit" except where a limit is clearly right:

| cap | default | what it bounds |
| --- | --- | --- |
| `max_candidate_destinations` | `None` | how many cities are considered from a state |
| `max_transport_options_per_leg` | `4` | options per city pair, cheapest and fastest first |
| `max_stay_lengths` | `None` | distinct stay durations tried per city |
| `accommodation_options_per_stay` | `2` | room tiers per stay |

Three things stop this from becoming a cost problem:

**The pool is fetched once per state, not once per candidate.** Transport
options for a leg are looked up as a batch and reused across every stay length
and room tier built on top of them.

**Bounds prune before the expensive work.** A candidate that cannot afford to
get home is rejected before its accommodation is priced — see
[Constraints and admissible bounds](#4-constraints-and-admissible-bounds).

**Everything goes through a cached provider.** Twelve thousand price lookups
become seventeen hundred upstream calls on a cold cache and zero on a warm one.
That distinction is what makes this design viable against a metered API, and
it is measured on every run rather than assumed.

Complexity is `O(iterations × beam_width × branching)` with `iterations ≤
max_cities + 1`, and the measurement matches the arithmetic almost exactly —
doubling the beam doubles the time and doubles the states:

| `beam_width` | effective | time | states | completed | best score found |
| --- | --- | --- | --- | --- | --- |
| 10 | 20 | 0.45 s | 5,770 | 345 | 0.6676 |
| 20 (default) | 40 | 0.89 s | 12,374 | 786 | 0.6729 |
| 40 | 80 | 1.78 s | 26,362 | 1,584 | **0.6802** |
| 80 | 80 (capped) | 1.78 s | 26,362 | 1,584 | 0.6802 |

Two honest observations. **The default beam does not find the best trip in this
network** — a 40-wide beam finds a strictly better one (0.6802 vs 0.6729, a
different route entirely) for twice the time. That is what "heuristic" means,
and it is why the number is a configuration field with a measured cost rather
than a constant buried in the search. **And the last row is the cap doing its
job**: `max_effective_beam_width = 80` stops flexible-date scaling from
multiplying a wide beam into an unbounded one, so `beam_width=80` here buys
nothing over 40.

---

## The adaptive beam

V3 measured the cost of its own heuristic and published the awkward result: a
beam of 40 found a **0.6802** itinerary that the default 20 never reached. That
is what "heuristic" means, and widening the guess would only have moved the
problem to a different request.

V4 stops guessing. `adaptive_beam=True` runs the search, doubles the beam, and
keeps doubling while the best completed score is still improving by more than
`adaptive_beam_tolerance`:

```
$ python examples/v4_capabilities.py --beam

  fixed (V3)      0.94s  beam 40   score 0.6729  CGN → Munich → Vienna → CGN
  adaptive (V4)  13.98s  beam 320  score 0.6823  CGN → Prague → Vienna → CGN
       rung w=40   best 0.6729  completed 786   states 12374    gain +0.0000
       rung w=80   best 0.6802  completed 1584  states 26362    gain +0.0073
       rung w=160  best 0.6823  completed 3658  states 50864    gain +0.0021
       rung w=320  best 0.6823  completed 7348  states 95090    gain +0.0000
```

The ladder is the point. It finds the trip V3 knew it was missing, it shows
*where* the gain came from (the first doubling bought 0.0073, the second
0.0021, the third nothing), and it stops on evidence rather than on a constant.
`metadata.beam_rounds` carries every rung, so a caller can tell whether the
search stopped because widening stopped paying or because it hit a ceiling.

**What it costs.** Doubling means the whole climb is a little under twice the
widest round alone — 14s against 0.94s here, for +0.0094 of score. Re-running
is cheaper than it sounds because every provider lookup is already cached from
the previous rung, so a widened round pays for search work and never for
upstream calls; but it is not free, and `metadata.states_generated` reports the
*whole* ladder including the rounds whose results were thrown away. Reporting
only the final round would understate what the answer cost.

**Off by default.** Turning it on changes which itinerary wins, and every
number published for V3 was measured with a fixed beam. One config field.

| field | default | |
| --- | --- | --- |
| `adaptive_beam` | `False` | opt in |
| `adaptive_beam_tolerance` | `0.002` | gain below which a rung did not pay |
| `adaptive_beam_max_rounds` | `4` | hard ceiling on widenings |
| `adaptive_beam_max_width` | `320` | widest rung — deliberately *not* `max_effective_beam_width`, which exists to cap flexible-date scaling and would stop the ladder at 80 before it had plateaued |

---

## Inventory and availability

V3's limitations said it plainly: *"rooms never sell out and flights never fill;
there is no inventory model"*, and `free_cancellation` was carried but unpriced.

**Unknown is not unlimited.** `TransportOption.seats_available` and
`AccommodationOption.rooms_available` are `int | None`, and `None` means the
provider did not say — which keeps the option bookable. A feed that quotes
fares without inventory counts is most of the real world, and an engine that
refused to book anything a provider declines to count would refuse to work.
`0` is a genuine sell-out.

**Sold out is not the same as absent.** Two rejection reasons, because
"there are no hotels in Prague" and "the last double went" are different facts
and a traveler told the wrong one has been misled:

```
SOLD_OUT_TRANSPORT           2601
SOLD_OUT_ACCOMMODATION        635
NO_ACCOMMODATION_AVAILABLE      …
```

**Sold-out fares are excluded before the branching cap, not after.** With
`max_transport_options_per_leg = 4`, letting an unbookable fare occupy one of
those four slots would push a bookable one out of the search entirely. A test
constructs exactly that case: two fares, the cheaper sold out, one slot.

**Refundability is now worth something.** V3 paid a flat `+0.05` for free
cancellation, which says that being able to cancel the last room in town is
worth exactly as much as cancelling one of forty. The bonus now scales with
scarcity, up to `+0.05` more — and stays exactly flat when the provider reports
no inventory, so an uncounted feed reproduces V3 to the last decimal.

Scarcity bites, which is the point of modelling it:

```
$ python examples/v4_capabilities.py --inventory

  no inventory data      786 completed   #1 CGN → Munich → Vienna → CGN
  with inventory         305 completed   #1 DUS → Budapest → Vienna → CGN
```

The synthetic counts are a **deterministic** function of city, date and tier —
the cheap rooms go first, some dates are much tighter than others, and no
randomness is involved, because the determinism guarantee is worth more than a
plausible-looking simulation. `simulate_scarcity=True` on either synthetic
provider turns them on; `require_availability=False` on the config turns
enforcement off for a caller who intends to re-check at booking time.

---

## Learning the profile weights

V3 was honest about the three profiles: *"tuned against the synthetic dataset.
They are a starting point, not a claim about real travelers."*

**The signal already exists.** Every planning run shows a handful of
itineraries and one gets chosen. That single fact — *this one beat those* — is
a pairwise preference, and a few dozen of them pin down nine weights. Nothing
extra needs collecting:

```python
observation = observations_from_result(result, chosen_rank=3)
report = fit_weights(history)          # a list of those
profile = learned_profile(report)      # plan() already accepts this
```

**The method** is exponentiated gradient ascent on the Bradley-Terry likelihood
— a few dozen lines of arithmetic, deterministic, no dependency. Two details
carry it:

- **Direction and sharpness are fitted separately.** The weights live on the
  simplex, which fixes their total and so also fixes how large a score gap the
  model can express. Coupled, the only way for the likelihood to sharpen its
  margins is to pile the whole budget onto the single most discriminative
  component — a traveler weighing experience 0.45 and city count 0.30 came back
  as experience **0.97**. Giving sharpness its own parameter recovers
  **0.62 / 0.28**, with pairwise agreement rising from 80% to 94%.
- **The prior is scaled against the amount of evidence.** This was the second
  bug: the likelihood gradient is averaged over pairs, so two observations
  shoved as hard as two hundred and *thin evidence produced the wilder
  profile*. Scaled by `REFERENCE_PAIRS / pairs`, the prior now dominates when
  evidence is thin and is out-voted as it accumulates — which is what the
  docstring had been claiming all along.

A cohort that always books the cheapest thing it was shown:

```
$ python examples/v4_capabilities.py --learning

Fitted on 7 choices (28 pairs)
  ranking accuracy  50.0% -> 75.0%  (+25.0%)
    cost           0.3170  (+0.1270)
    experience     0.1393  (-0.0707)
    city_count     0.0077  (-0.0723)
    …
  default profile #1:  409.74 EUR  CGN → Munich → Vienna → CGN
  learned profile #1:  342.32 EUR  CGN → Berlin → CGN
```

**Weights are taste; thresholds are policy.** `learned_profile` fits the nine
weights and inherits everything else — the budget-utilization split, the
diversity threshold, the city-count bias — from a template profile. Nothing in
a click stream identifies those, and pretending otherwise would be fitting
noise.

**It does not touch the optimizer.** It produces a `RecommendationProfile`,
which the planner has accepted from any caller since V2. An AST test asserts
that `algorithms/`, `constraints/`, `providers/`, `data/` and `models/` never
import it.

---

## Budget sensitivity

"Would another fifty euros change the answer?" is the question a traveler
actually asks, and a single plan cannot answer it. `analyze_budget_sensitivity`
replans across a range of budgets and reports where the answer *changes*:

```
$ python examples/v3_scenarios.py --budget-sensitivity

Budget sensitivity (BEST_VALUE)
----------------------------------
 *    250   244.78  score 0.5442  CGN → Brussels → CGN
            + unlocks Brussels
 *    300   289.50  score 0.6069  CGN → Berlin → CGN
            + unlocks Berlin
 *    350   327.16  score 0.6117  CGN → Vienna → CGN
            + unlocks Vienna, Prague, Munich, Budapest
 *    400   396.03  score 0.6570  CGN → Prague → Vienna → CGN
            + unlocks Prague + Vienna, Munich + Vienna, Amsterdam + London
 *    450   409.74  score 0.6729  CGN → Munich → Vienna → CGN
            + unlocks Budapest + Vienna, Berlin + Prague, Berlin + Munich
 *    550   461.10  score 0.6943  DUS → Budapest → Vienna → CGN
            + unlocks Amsterdam + Brussels, Barcelona + Milan
```

The interesting line is **€400**: that is where two-city trips become reachable
at all, and the score jumps 0.0453 for €50. Between €300 and €350 the same €50
buys 0.0048 — nearly nothing, because it only swaps one single-city trip for
another. A traveler deciding how much to save up wants that difference stated,
not buried.

`*` marks a threshold: a budget at which new city sets became reachable.
`minimum_feasible_budget` reports the level below which nothing is affordable at
all, and it is `None` rather than a fabricated number when no budget in the
sweep works. Two invariants are tested: a larger budget can never produce a
*worse* best score, and no step ever returns a trip that exceeds its own budget.

The sweep reuses the planner's provider caches, so a six-point sweep costs the
provider calls of one plan.

---

## Recommendation profiles

There is no single right answer to "what is the best trip?". A profile is a
complete answer to that question, expressed as the nine Travel Value weights
(normalized, so they always sum to 1) plus a few behavioural overrides.

| | cost | experience | preferences | time | diversity | city count | accommodation | convenience | intensity |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **CHEAPEST** | 0.62 | 0.08 | 0.08 | 0.04 | 0.03 | 0.03 | 0.04 | 0.04 | 0.04 |
| **BEST_VALUE** (default) | 0.19 | 0.21 | 0.15 | 0.11 | 0.06 | 0.08 | 0.07 | 0.05 | 0.08 |
| **ADVENTURE** | 0.13 | 0.19 | 0.11 | 0.07 | 0.19 | 0.12 | 0.03 | 0.03 | 0.13 |

| | budget utilization | min duration utilization | diversity threshold | city count bias |
| --- | --- | --- | --- | --- |
| **CHEAPEST** | 0.00 | 0.50 | 0.5 | −1 |
| **BEST_VALUE** | 0.35 | — | 0.5 | 0 |
| **ADVENTURE** | 0.40 | — | 0.6 | +1 |

Three things are worth reading off that table.

**CHEAPEST is not "cost only".** At 0.62 it still owes the traveler a trip that
works: a €10 saving cannot buy a 5am connection through three airports. Setting
cost to 1.0 would make the profile indifferent to everything the rest of the
engine computes, and the cheapest itinerary is rarely the one a person would
book. `min_duration_utilization = 0.5` additionally forbids CHEAPEST from
"winning" by proposing a one-night trip against a five-day request.

**BEST_VALUE spends more on experience than on cost** (0.21 vs 0.19). That is
the intended statement: the default profile is not a price-sorter with
decoration. Cost still enters twice — directly, and through the budget
utilization term at 0.35, which rewards *using* the budget rather than
hoarding it.

**ADVENTURE is not "cost last".** Diversity (0.19) and intensity (0.13) both
matter, and they pull against each other: seeing more places means moving more,
and moving more is penalized. Without the intensity term, ADVENTURE degenerates
into an airport-hopping slog that visits four cities and sees none of them.
`city_count_bias = +1` shifts the *ideal* city count up by one before scoring,
rather than making more cities unconditionally better.

Profiles are data (`travel_planner/profiles.py`), not constants scattered
through the algorithms; callers can construct their own `RecommendationProfile`
and pass it to `plan()`.

---

## Worked example

`python examples/v3_scenarios.py` runs three budgets against all three
profiles. This is scenario B: **Köln, €450, 2 travelers, 5 days**, prefers
Madrid, avoids Paris, flexible dates, preferred experiences culture / history /
food. Baseline: **Madrid €338.47**, 3 nights.

Columns: value = Travel Value score, total = €, T/A/G = transport /
accommodation / ground transfer, transit = hours in motion, usable = hours of
usable destination time, exp = experience score, int = travel intensity.

**CHEAPEST** — price-ordered, single cities:

```
 #   value    total       T       A      G  transit   usable   exp   int  route
 1  0.5221   241.78   103.8   118.0   20.0     6.5h    37.4h  0.70  0.09  CGN → Brussels → CGN
 2  0.4970   277.50   139.5   118.0   20.0     3.2h    38.2h  0.80  0.04  CGN → Berlin → CGN
 3  0.4423   316.96   168.3   128.7   20.0     4.0h    37.8h  0.83  0.05  CGN → Vienna → CGN
 4  0.4189   304.24   150.0   134.2   20.0     8.0h    35.9h  0.69  0.11  CGN → Amsterdam → Brussels → CGN
 5  0.4112   338.47   179.1   139.4   20.0     6.5h    36.6h  0.81  0.09  CGN → Madrid → CGN
```

**BEST_VALUE** — every one of the five is a two-city trip:

```
 #   value    total       T       A      G  transit   usable   exp   int  route
 1  0.6729   409.74   205.3   184.5   20.0     7.8h    57.8h  0.81  0.07  CGN → Munich → Vienna → CGN
 2  0.6676   441.54   220.8   180.7   40.0     7.2h    56.8h  0.80  0.06  CGN → Vienna → Budapest → DUS
 3  0.6643   425.70   229.2   176.5   20.0     7.5h    53.9h  0.79  0.06  CGN → Berlin → Prague → CGN
 4  0.6544   446.62   217.0   209.6   20.0     6.2h    52.8h  0.78  0.06  CGN → London → Amsterdam → CGN
 5  0.6530   439.66   242.2   177.5   20.0     7.1h    58.1h  0.79  0.07  CGN → Munich → Berlin → CGN
```

**ADVENTURE** — same shape, reordered, and it will leave from a different
airport to reach a more distinctive pair:

```
 #   value    total       T       A      G  transit   usable   exp   int  route
 1  0.6278   403.04   212.2   150.8   40.0     7.2h    56.4h  0.76  0.06  DUS → Budapest → Vienna → CGN
 2  0.6244   418.10   204.7   193.4   20.0     7.8h    59.2h  0.79  0.07  CGN → Munich → Vienna → CGN
 3  0.6200   447.78   255.2   172.6   20.0     8.7h    53.9h  0.79  0.07  CGN → Prague → Munich → CGN
 4  0.6120   436.60   230.7   185.9   20.0     7.5h    53.1h  0.76  0.07  CGN → London → Brussels → CGN
 5  0.6099   418.52   212.8   185.8   20.0     7.7h    50.2h  0.72  0.07  CGN → Prague → Vienna → CGN
```

Three things to read off this.

**The top CHEAPEST trip is €168 cheaper than the top BEST_VALUE trip and buys
20 fewer hours of usable time.** €241.78 for 37.4 usable hours is €6.46/hour;
€409.74 for 57.8 usable hours is €7.09/hour, for a second city and a better
experience score. Whether that is worth €168 is the traveler's call, which is
exactly why it is a profile and not a constant.

**Madrid — the user's own preferred destination — is #5 under CHEAPEST and
absent under the other two.** The planner does not simply hand back the request.
It surfaces Madrid because it was asked to and it fits, then ranks it against
what else the money buys.

**Nothing on the BEST_VALUE list is a slog.** Travel intensity sits at
0.06–0.07 across all five: two cities over five days is one move, and the
engine has no incentive to add a third because `city_count` scores against an
*ideal* derived from the catalog's recommended stays, not against "more".

Scenario C (€600, 7 days) is where three- and four-city trips take over:

```
 1  0.6794   581.66   263.2   278.5   40.0    10.9h    77.4h  0.79  0.07  CGN → Prague → Vienna → Budapest → DUS
    rooms: Prague 3n standard (4.1★), Vienna 2n budget (3.3★), Budapest 2n budget (3.3★)
```

Note the mixed accommodation tiers on a single trip: the engine paid up for a
better room in Prague, where it stays three nights, and took budget rooms for
the two-night stops. That is the accommodation branching doing its job — it is
one search over rooms *and* routes, not a route search with hotels bolted on
afterwards.

---

## Observability

Everything discarded is recorded as a typed object, never a log string. Run
counters are always collected; `debug=True` returns the whole trace.

```python
result = planner.plan(request, debug=True)
print(result.debug.render())
```

```
Search setup
------------------
Origin airports: CGN, DUS, EIN, FRA
Start dates:     2026-09-10, 2026-09-11
Initial states:  8

Iteration 2
------------------
States in:    40
Generated:    7024
Rejected:     5072
Remaining:    1454
Beam width:   40
Beam pruning: 1414
Kept:         40
Completed:    498
Rejections:
  DURATION_UNDERUSED: 1831
  BUDGET_EXCEEDED: 1238
  UNREACHABLE_RETURN_BUDGET: 822
  TRANSPORT_TYPE_NOT_ALLOWED: 748
  UNAFFORDABLE_ACCOMMODATION: 206
  AVOIDED_DESTINATION: 203
  UNREACHABLE_RETURN_TIME: 24

...

Post-processing
------------------
Completed itineraries: 786
Pareto:    786 -> 124
Diversity: 25 -> 5
```

Read that as a funnel: 786 complete round trips survived every constraint,
ε-dominance cut them to 124, and two de-duplication passes plus `max_results`
produced 5. A run where `Pareto: 786 -> 700` would mean the frontier had stopped
filtering; a run where `Completed: 0` in every iteration means the constraints,
not the ranking, are what rejected the request — and the rejection histogram
says which one.

| What you want to know | Where |
| --- | --- |
| Why a state was rejected | `debug.iterations[i].rejected_examples[j].reason` / `.detail` |
| Why a state lost the beam | `debug.iterations[i].pruned_examples[j].estimated_score` |
| Why an itinerary scored what it did | `itinerary.value_breakdown` (9 components + weights + raw diagnostics) |
| Where the money went | `itinerary.cost_breakdown` (transport / accommodation / ground_transfer) |
| Where the time went | `total_travel_minutes`, `ground_transfer_minutes`, `usable_destination_minutes` |
| What each stay cost and bought | `itinerary.stays[j]` (tier, rating, location score, refundability) |
| Why a city was chosen | `itinerary.destination_insights[j]` (quality, preference match, matched/missed attributes) |
| Why it is worth showing | `itinerary.explanation_factors` |
| Why an itinerary was Pareto-filtered | `debug.filtered` where `stage == PARETO`, with `dominated_by` |
| Why an itinerary was dropped as a duplicate | `debug.filtered` where `stage == DUPLICATE_ROUTE`, similarity `1.0` |
| Why an itinerary was diversity-filtered | `debug.filtered` where `stage == DIVERSITY`, with `similarity` |
| How hard the providers were hit | `result.metadata.provider_metrics` |
| What the search actually cost | `metadata.states_generated`, `.states_rejected`, `.elapsed_seconds`, `.beam_width` vs `.configured_beam_width` |

### Provider metrics

`provider_metrics` is the number that tells you whether this engine would
survive a paid API. A single scenario-B plan:

```
lookups                12078
hits                   10368
misses                  1710      <- actual upstream calls
hit_rate                 0.86
transport_lookups       5898   ->  1551 upstream
accommodation_lookups   6172   ->   155 upstream
ground_transfer_lookups    8   ->     4 upstream
```

Twelve thousand price lookups, seventeen hundred upstream calls. Re-plan the
same request — a different profile, a budget sweep, a user changing one slider —
and the hit rate goes to 100%: the `--budget-sensitivity` run replans six times
and adds **zero** upstream calls after the first. This is why caching lives in a
decorator at the provider boundary and not inside the search: the algorithm asks
for whatever it needs, and the boundary decides what that costs.

### Explanation factors

`explanation_factors` are typed flags derived deterministically from the
finished itinerary — `great_destination_match`, `good_budget_usage`,
`two_cities`, `low_travel_intensity`, `good_accommodation_value`,
`high_destination_quality`, `cheaper_than_baseline`, `late_arrival`, … The
optimizer states facts; turning them into prose is the LLM layer's job (see
[Where an LLM fits](#where-an-llm-fits)). Because every flag is computed from
the itinerary, the explanation cannot drift from what was actually optimized.

---

## Configuration

One frozen pydantic model, `PlannerConfig`, holds every tunable. Nothing in the
algorithms reads a module-level constant that isn't either a documented
model coefficient or reachable from here.

```python
PlannerConfig(
    # Search shape
    beam_width=20, max_results=5, max_cities=4, beam_slots_per_route=2,
    scale_beam_with_start_dates=True, max_effective_beam_width=80,

    # Stay shape
    min_city_stay_days=1, max_city_stay_days=4,
    min_duration_utilization=0.6,
    stay_overrun_tolerance_days=3.0, city_change_overhead_days=0.5,

    # Origin
    max_origin_distance_km=250.0, max_origin_airports=4,

    # Candidate generation (V3) — None means "no cap"
    max_candidate_destinations=None,
    max_transport_options_per_leg=4,
    max_stay_lengths=None,
    accommodation_options_per_stay=2,

    # Economics
    enable_accommodation=True, enable_ground_transfer=True,
    usable_day_start=time(8, 0), usable_day_end=time(21, 0),
    profile=ProfileName.BEST_VALUE,
    budget_utilization_target=0.6, comfortable_days_per_city=2.0,

    # Post-processing
    enable_pareto=True, enable_diversity=True,
    diversity_similarity_threshold=0.5,

    # V4 - both off by default, so every V3 number reproduces
    adaptive_beam=False, adaptive_beam_tolerance=0.002,
    adaptive_beam_max_rounds=4, adaptive_beam_max_width=320,
    require_availability=True,   # harmless when a feed reports no inventory

    # Instrumentation
    collect_provider_metrics=True, debug_example_limit=5,
    currency="EUR",

    # V1 diagnostic engine, still computed and still exposed
    score_weights=ScoreWeights(...),
    max_travel_time_fraction=0.25,
    preferred_destination_bonus=0.5, must_visit_bonus=0.3,
)
```

The four knobs that matter most:

- **`beam_width`** trades runtime for quality, linearly. `scale_beam_with_start_dates`
  widens it when flexible dates multiply the number of independent starting
  points, so a flexible request doesn't silently search *worse* than a fixed one
  by spreading the same beam over five times as many days — that bug is
  described in [trap 4](#the-five-traps).
- **`max_transport_options_per_leg`** is the single biggest lever on cost: the
  branching factor is `destinations × transport × stay lengths × room tiers`,
  and this caps the second term. Options are ranked before the cut, so the cheap
  and the fast ones survive.
- **`accommodation_options_per_stay`** is what makes the price/quality trade-off
  real. At `1` there is no trade-off to make and the engine reduces to V2
  behaviour; at `2` (the default) each stay branches into a cheap room and a
  better one.
- **`enable_accommodation=False`** and **`enable_ground_transfer=False`** restore
  the V1 economics exactly, and a test asserts it.

---

## The API

The API is only an adapter: it validates, calls the planner, serializes. A test
plans the same request directly and over HTTP and asserts the two results are
identical.

```
POST /plan-trip[?debug=true][&profile=CHEAPEST|BEST_VALUE|ADVENTURE]
POST /budget-sensitivity[?steps=5&span=0.5&profile=…]
GET  /profiles                 the profiles and their nine weights
GET  /destinations             the synthetic catalog, with all 12 attributes
GET  /config                   the active PlannerConfig
GET  /health
```

The profile may also be set in the request body (`"profile": "ADVENTURE"`); the
query parameter wins. Omitted, it is `BEST_VALUE`.

New V3 request fields, all optional and all backward compatible:

```json
{
  "origin": "Köln", "budget": 450, "travelers": 2, "duration_days": 5,
  "date_from": "2026-09-10", "date_to": "2026-09-15", "date_flexible": true,
  "transport_preferences": ["flight", "train"],
  "preferred_destinations": ["Madrid"], "avoid_destinations": ["Paris"],

  "preferred_experiences": ["culture", "history", "food"],
  "disliked_experiences": ["nightlife"],
  "previously_visited": ["Amsterdam"],
  "preferred_city_count": 2,
  "accommodation_preference": "COMFORT",
  "travel_style": "BALANCED",
  "preferences": {"history": 0.8, "culture": 0.8, "nature": 0.7, "food": 0.6}
}
```

An unknown experience name or a city in both `preferred_experiences` and
`disliked_experiences` is a `422`, not a silent no-op. Every V3 field left out
falls back to V2 behaviour.

Response for scenario B (abridged — synthetic data):

```json
{
  "profile": "BEST_VALUE",
  "baseline": {
    "destination": "Madrid", "total_cost": 338.47, "nights": 3,
    "cost_breakdown": {"transport": 179.06, "accommodation": 139.41, "ground_transfer": 20.0}
  },
  "recommendations": [
    {
      "rank": 1, "score": 0.672931, "total_cost": 409.74, "currency": "EUR",
      "cities": ["Munich", "Vienna"], "stay_days": [2, 2],
      "cost_breakdown": {"transport": 205.26, "accommodation": 184.48, "ground_transfer": 20.0},
      "stays": [
        {"city": "Munich", "arrival": "2026-09-10T08:40:00",
         "departure": "2026-09-12T11:20:00", "nights": 2,
         "accommodation_cost": 100.62, "accommodation_name": "Munich budget stay",
         "accommodation_tier": "budget", "accommodation_type": "hostel",
         "accommodation_rating": 0.66, "accommodation_location_score": 0.55,
         "free_cancellation": false, "usable_minutes": 1720}
      ],
      "total_travel_minutes": 425,
      "ground_transfer_minutes": 40,
      "usable_destination_minutes": 3470,
      "value_breakdown": {
        "profile": "BEST_VALUE",
        "cost": 0.4082, "experience": 0.8083, "preferences": 0.6574,
        "time": 0.8200, "diversity": 0.7222, "city_count": 1.0,
        "accommodation": 0.605, "convenience": 0.7762, "intensity": 0.4042,
        "total": 0.672931,
        "weights": {"cost": 0.19, "experience": 0.21, "preferences": 0.15,
                    "time": 0.11, "diversity": 0.06, "city_count": 0.08,
                    "accommodation": 0.07, "convenience": 0.05, "intensity": 0.08},

        "budget_utilization": 0.9105, "usable_ratio": 0.8897,
        "transport_minutes": 465, "travel_intensity": 0.0712,
        "legs_per_day": 0.6611, "destination_quality": 0.7336,
        "stay_quality": 1.0, "ideal_city_count": 2
      },
      "destination_insights": [
        {"city": "Munich", "score": 0.7916, "quality": 0.7182,
         "preference_match": 0.7449, "stay_quality": 1.0, "usable_days": 2.205,
         "strengths": ["culture", "food", "nature"], "weaknesses": [],
         "dislikes_present": [], "previously_visited": false,
         "stay_note": "2.2 usable days is within the recommended 1-3"}
      ],
      "baseline_comparison": {
        "baseline_destination": "Madrid", "baseline_cost": 338.47,
        "money_saved": -71.27, "additional_cities": 1,
        "additional_travel_minutes": 75
      },
      "explanation_factors": [
        "fits_budget", "good_budget_usage", "high_destination_quality",
        "two_cities", "reasonable_travel_time", "good_use_of_window",
        "great_destination_match", "low_travel_intensity", "ideal_city_count"
      ]
    }
  ],
  "metadata": {
    "origin": "Köln", "profile": "BEST_VALUE",
    "origin_airports": ["CGN", "DUS", "EIN", "FRA"],
    "start_dates": ["2026-09-10", "2026-09-11"],
    "beam_width": 40, "configured_beam_width": 20, "max_cities": 4,
    "states_generated": 12374, "states_rejected": 9804,
    "completed_itineraries": 786, "pareto_kept": 124, "diversity_kept": 5,
    "returned": 5, "elapsed_seconds": 0.83, "currency": "EUR", "warnings": [],
    "provider_metrics": {"lookups": 12078.0, "misses": 1710.0, "hit_rate": 0.8584, "...": "..."}
  }
}
```

Note `value_breakdown`: the nine weighted components come first, then the raw
diagnostics they were computed from (`budget_utilization`, `usable_ratio`,
`travel_intensity`, `legs_per_day`, `destination_quality`, `stay_quality`,
`ideal_city_count`). A client can render the score, and it can also show its
working.

`money_saved` is **negative** here, and that is the honest answer: this trip is
€71 dearer than the traveler's own Madrid idea. It also buys a second city and
15 more usable hours, which is what the other fields are for. The engine does
not hide the number that makes it look worse.

`beam_width` vs `configured_beam_width` is deliberate: the first is what the run
actually used after scaling for flexible dates, the second is what you asked
for. A run that silently searched wider than configured is a run whose timings
you would otherwise misread.

`POST /budget-sensitivity` returns the sweep described below, and both endpoints
return `422` for invalid input (unknown origin, unknown profile, contradictory
destinations, negative budget, `steps=0`) rather than an empty result.

---

## Synthetic data

- **Transport** — 198 directed connections over 5 airports and 16 cities;
  flights, trains and buses; 2–3 departures a day across 2026-09-01 → 09-30.
  Prices vary by date and slot through fixed multiplier tables indexed by
  `date.toordinal() % 7` — deterministic, no randomness. Outbound and return
  prices are declared **separately per link**, which is what makes trap 1
  possible.
- **Accommodation** — a standard nightly rate per city (Budapest €40 →
  Zurich €95), three tiers, a weekend surcharge. `date_variation=False` and an
  injected rate table let fixtures pin the arithmetic exactly.
- **Ground transfers** — an explicit table for the common origins, with a
  distance-based fallback so no airport is ever silently free to reach.
- **Destinations** — 16 cities, each rated on all **12 experience attributes**
  plus a recommended stay range. Vienna, for example: history 0.92, culture
  0.95, museums 0.95, architecture 0.92, food 0.80, romance 0.85, nightlife
  0.60, shopping 0.70, family 0.75, nature 0.45, adventure 0.35, beaches 0.00,
  recommended 2–4 days. The ratings are deliberately *not* uniform — a city that
  is strong at everything teaches the optimizer nothing.

Adding a city means one `_destination(…)` entry with all 12 attributes, a
nightly rate and a few `_link(…)` rows. The 12 attributes are keyword-only in
that constructor precisely so a new city cannot silently inherit a default.

---

## Plugging in real APIs

Three protocols, no algorithm changes. The optimizer imports the *interfaces*;
it has never imported an implementation.

```python
class TransportDataProvider(Protocol):
    def search(self, origin, destination, departure_date) -> list[TransportOption]: ...

class AccommodationDataProvider(Protocol):
    def search(self, city, check_in, check_out, travelers) -> list[AccommodationOption]: ...
    def min_price_per_night(self, city, travelers) -> float | None: ...

class GroundTransferProvider(Protocol):
    def search(self, origin, airport) -> list[GroundTransferOption]: ...
```

Two further protocols supply the pruning bounds, declared in
`constraints/validator.py` so the constraint layer stays a leaf:

```python
class ReturnEstimator(Protocol):
    def min_return_price_per_person(self, city: str) -> float | None: ...
    def min_return_minutes(self, city: str) -> int | None: ...

class AccommodationEstimator(Protocol):
    def min_stay_cost(self, city: str, nights: int, travelers: int) -> float: ...
```

### One is now written (V4)

V3 shipped the protocols and a stub that raised `NotImplementedError`, and
listed *"the HTTP clients and their response mapping"* as what remained.
`providers/amadeus.py` is that, written against the Amadeus Flight Offers
Search shape:

```python
planner = TravelPlanner(
    transport_provider=AmadeusTransportProvider(
        client_id=..., client_secret=...,
    )
)
```

That is the entire migration. It satisfies `TransportDataProvider`, so no
algorithm changed, and `TravelPlanner` wraps it in the caching decorator
automatically — which is what turns twelve thousand lookups into seventeen
hundred calls.

What is actually implemented: OAuth2 client-credentials with a cached token and
an expiry margin, request construction, offer→`TransportOption` mapping,
per-party→per-person conversion, currency normalization, ISO-8601 durations,
seat inventory, deterministic cheapest-first ordering, an admissible `min_price`
bound, and the failure behaviour below.

**The only thing missing is a credential**, and the tests do not need one: the
HTTP client is injected, so all 57 of them drive the real provider through
recorded payloads. An integration whose test suite needs a live API and a secret
is an integration nobody runs.

The transport layer underneath it (`providers/http.py`) is stdlib-only —
`urllib`, behind an `HttpClient` protocol — with retries on 408/425/429/5xx,
exponential backoff that honours `Retry-After`, a rate limiter, and call
metrics. A deployment that already has `httpx` supplies its own client in about
fifteen lines and inherits the retry behaviour, because it is a decorator.

Implement the other two the same way — Booking / Expedia for rooms, a routing
API for transfers.

### Price normalization

Real quotes arrive in different currencies, on different bases, with tax
sometimes included. `models/money.py` makes that explicit instead of implied:

```python
Money(amount=129.0, currency="GBP", tax_included=False)
PriceBasis.PER_PERSON | PER_ROOM_NIGHT | TOTAL

normalizer = PriceNormalizer(rates=FixedExchangeRates({"GBP": 0.85}))
normalizer.party_total(quote, PriceBasis.PER_ROOM_NIGHT, travelers=2)  # -> EUR float
```

`Money.__add__` refuses to add two currencies, or a tax-inclusive amount to a
tax-exclusive one, rather than producing a plausible wrong number. Conversion
happens **once, at the provider boundary**; nothing downstream of a `SearchState`
has ever seen a foreign currency. The module deliberately does not fetch rates —
`ExchangeRateSource` is the seam, `FixedExchangeRates` is the test double.

### Caching and call metrics

`TravelPlanner` wraps whatever you inject, so a real provider is cached by
construction:

```python
planner = TravelPlanner(transport_provider=RealTransportProvider(...))
# -> planner.transport is a CachingTransportProvider around it
result.metadata.provider_metrics   # lookups, misses, hit_rate, per-provider
```

They are decorators, not a base class, so they wrap any implementation of the
protocol and forward everything else through `__getattr__`. Deliberately
in-process, no TTL, no Redis: a cache for the life of one planning run is what
this needs, and anything more is infrastructure the project has not earned. On
the standard scenario that is **12,078 lookups → 1,710 upstream calls**, and
zero on a replan.

### What a real provider must still guarantee

- **Never raise on "nothing found"** — return an empty list. An exception in a
  provider aborts a search that had 800 other viable itineraries. `Amadeus`
  returns `[]` for an empty page and for a 404, and raises for auth failures
  and exhausted retries: silently returning nothing for a broken integration
  would look like a network with no flights in it.
- **Distinguish a bad record from a bad configuration.** One malformed offer in
  a page of twenty is skipped; a missing exchange rate is not. That was a real
  bug in this code: skipping unpriceable offers turned "nobody configured GBP"
  into "there are no flights" — silent, total, and indistinguishable from a
  quiet route. It now fails loudly.
- **`min_return_*` and `min_price_per_night` must be admissible** — a true lower
  bound, or pruning discards feasible trips. Returning `None` (unknown) is
  always safe and is handled as "assume zero"; returning an optimistic guess is
  not. This is exactly the bug V3 found and fixed in the return-cost bound.
- **Be deterministic within a run.** The cache gives you this for free for
  repeated identical queries; if the upstream is not stable across a run, the
  determinism guarantee weakens to "stable for a fixed snapshot", and that
  should be stated rather than assumed.
- **Normalize at the boundary**, using `PriceNormalizer`, so no per-person /
  per-room ambiguity reaches the search.

---

## Where an LLM fits

```
free text ─► PreferenceParser ─► TripRequest ─► [ deterministic optimizer ] ─► Itinerary ─► ItineraryExplainer ─► prose
             (Claude lives here)                (never an LLM)                             (Claude lives here)
```

V3 defined both protocols and shipped dependency-free stand-ins
(`KeywordPreferenceParser`, `TemplateItineraryExplainer`), and listed *"a model
call, and the prompt/guardrails around it"* as what remained. V4 adds both,
backed by `claude-opus-5`.

**The optimizer is still LLM-free, and the test that says so now guards a
package with a real model in it.** An AST walk over every module in
`algorithms/`, `constraints/`, `services/`, `providers/` and `data/` fails if
any of them imports `travel_planner.llm`. Prices, availability, travel time,
feasibility and scores are computed; nothing at either seam can change one.

`anthropic` is an optional extra (`pip install ".[llm]"`). The package imports
and the whole test suite runs without it, and **no test in this repository makes
a network call** — `LlmClient` is a protocol, so both seams are driven by
scripted replies, including the replies a real model gets wrong.

### Behind: the explainer

The low-risk seam, and deliberately first. V3 built the material it consumes —
typed factors, per-city insights, the nine-component breakdown — precisely so a
generative layer would have nothing left to invent:

| structured input | what the explainer says with it |
| --- | --- |
| `explanation_factors` | which claims are true of this trip, as typed flags |
| `destination_insights[j].strengths` / `.weaknesses` | *why this city*, in the traveler's own preference terms |
| `value_breakdown` (9 components + weights) | which trade-off the profile actually made |
| `cost_breakdown`, `stays[j]` | where the money went, tier by tier |
| `baseline_comparison` | how it compares to what they asked for |

"It only restates" is a claim, though, and an unchecked claim about a language
model is worth nothing. So **every number in the generated prose is checked
against the numbers that were actually put in the prompt**, and a reply
containing a figure the optimizer never computed is rejected:

```
model said : A wonderful trip, and only 199.99 EUR for the two of you.
rejected   : True (199.99 was never computed)
shipped    : #1: CGN → Munich → Vienna → CGN  …   ← the template explainer
```

A slightly duller sentence beats a confidently wrong price. The check is cheap
and total — an explanation is a handful of sentences over a known set of
figures, so grounding is decidable here in a way it is not in general.

The guard is deliberately not stricter than the prompt. Every reasonable
rendering of a given number is allowed: two decimals, one, rounded, or
truncated, with or without thousands separators, and minutes read back as
hours. *"57 hours on the ground"* for 57.8 is good prose, and a guard that
fires on the writing instead of on the facts is useless exactly when it
matters. `explainer.rejections` keeps what was thrown away, so the firing rate
is observable rather than invisible.

### In front: the parser

The higher-risk seam, and second on purpose: a model here decides *what gets
searched*, so a hallucinated destination is not a cosmetic error — it is a
wrong trip, confidently delivered.

**The guard is the type.** `TripRequest` already rejects unknown experience
names, contradictory destination lists, impossible windows and non-positive
budgets. V3 tightened that validation "precisely because it is the layer that
would eventually face model output"; this is that layer arriving.

Three lines of defence, in order:

1. **Structured output** constrains the reply to a JSON schema derived from the
   domain model — so a new experience attribute cannot be added to the engine
   and forgotten here.
2. **Validation** rejects what a schema cannot express: a schema can say "a
   string", only `TripRequest` knows whether *this* string is a city the catalog
   has heard of.
3. **One retry with the error fed back**, then the keyword parser answers. The
   retry earns its cost because the common failure is one the model can fix
   when it is told:

```
attempt 1 rejected: 1 validation error for TripRequest …
accepted request  : 600 EUR, 2 travelers, ['culture', 'food']
```

The pipeline degrades to V3, never to nothing. What the traveler did not say —
the origin, the date window — comes from the caller's defaults, not the model:
those are the application's business, and a model asked to guess them will.

---

## Running the tests

```bash
pytest                                       # 654 tests
pytest tests/test_beam_search.py -v          # the non-greedy proof
pytest tests/test_adversarial.py -v          # the 20 V2 spec scenarios
pytest tests/test_v3_adversarial.py -v       # the 13 V3 scenarios
pytest -k "v4"                               # the V4 additions
pytest -k "not api"                          # skip the FastAPI tests
```

**All 443 V3 tests pass unmodified.** Not one was edited, skipped, or deleted —
the only change to a pre-existing test file is an additive optional argument on
a `conftest.py` helper. Where V3 had to update sixteen tests because it changed
behaviour they asserted, V4 changed none, because the two additions that *would*
have changed behaviour (the adaptive beam and inventory simulation) are both
opt-in. Every published V3 figure still reproduces to the decimal.

| File | Covers | tests |
| --- | --- | --- |
| `test_models.py` | model validation, per-person vs. total price, state transitions | 21 |
| `test_constraints.py` | every rejection reason, pruning bounds | 22 |
| `test_scoring.py` | the V1 engine, each component formula, reweighting | 30 |
| `test_beam_search.py` | **trap 1**, beam width, determinism, stay lengths | 13 |
| `test_pareto.py` | domination algebra and the frontier | 12 |
| `test_diversity.py` | Jaccard, exact-duplicate collapse, back-fill | 14 |
| `test_baseline.py` | baseline round trip and comparisons | 11 |
| `test_providers.py` | dataset coverage, trap 1's data property, origin resolution | 22 |
| `test_api.py` | HTTP shape, error codes, adapter equivalence | 10 |
| `test_llm_interfaces.py` | the seams, and that the optimizer never imports them | 7 |
| `test_end_to_end.py` | the full Köln scenario, requirement by requirement | 27 |
| `test_v2_accommodation.py` | rooms, tiers, capacity, pruning, **trap 2** | 26 |
| `test_v2_ground_transfer.py` | transfers, door-to-door cost, **trap 3** | 19 |
| `test_v2_usable_time.py` | the usable-day model and its effect on scoring | 16 |
| `test_v2_profiles.py` | Travel Value, the three profiles | 25 |
| `test_v2_explainability.py` | every exposed figure, factors, API surface | 16 |
| `test_adversarial.py` | the 20 numbered V2 scenarios | 33 |
| `test_v3_experience.py` | the 12 attributes, ExperienceScore, preferences, city count | 33 |
| `test_v3_accommodation_value.py` | AccommodationScore, tier branching, the price/quality trade | 16 |
| `test_v3_intensity.py` | travel intensity, transit share, airport churn, travel style | 13 |
| `test_v3_providers.py` | Money, PriceBasis, normalization, caching, call metrics | 28 |
| `test_v3_budget_sensitivity.py` | the sweep, its invariants, the HTTP endpoint | 16 |
| `test_v3_adversarial.py` | the 13 V3 scenarios, including **traps 4 and 5** | 13 |
| `test_v4_value_for_money.py` | the premium, the reference rate, monotonicity, the dead zone | 22 |
| `test_v4_adaptive_beam.py` | the ladder, the stopping rule, honest cost reporting | 19 |
| `test_v4_availability.py` | unknown vs. sold out, scarcity, priced refundability | 35 |
| `test_v4_learning.py` | weight recovery, held-out accuracy, the prior, validation | 33 |
| `test_v4_real_providers.py` | auth, mapping, currency, retries, backoff, rate limits | 57 |
| `test_v4_llm.py` | the grounding guard, schema, retry, fallback, isolation | 45 |
| | **total** | **654** |

Determinism is asserted, not assumed: several tests plan the same request twice
and compare the full result object, the budget sweep is compared for exact
equality across runs, and the weight fit is asserted to be reproducible.

**No test makes a network call.** The real provider is driven through recorded
payloads and both LLM seams through scripted replies, because an integration
whose tests need a live API and a secret is an integration nobody runs.

---

## Performance

Measured on the standard request (Köln, €450, 2 travelers, 5 days, prefers
Madrid, avoids Paris), cold caches, best of three runs, same machine.

| | V2 | V3 | V4 | states | completed |
| --- | --- | --- | --- | --- | --- |
| fixed dates | 0.283 s | 0.397 s | **0.428 s** | 5,804 | 385 |
| flexible dates | 0.284 s | 0.830 s | **0.873 s** | 12,374 | 783 |
| 7 days, €600 | 0.570 s | 0.739 s | **0.802 s** | 8,428 | 833 |

**V4 costs about 5–8% over V3 and searches exactly the same space** — the state
and completion counts are identical to the decimal, which is the strongest
available evidence that nothing about the search changed. The overhead is the
value-for-money plumbing: each stay now carries the cheapest alternative the
provider offered, and every completed itinerary is scored for it.

The two features that *would* change the search are opt-in, and here is what
each costs when you opt in:

| | time | states | best score |
| --- | --- | --- | --- |
| flexible dates, V3 defaults | 0.873 s | 12,374 | 0.6719 |
| **+ inventory** (`simulate_scarcity`) | 0.488 s | 10,126 | 0.6761 |
| **+ adaptive beam** (`adaptive_beam=True`) | 15.3 s | 184,888 | 0.6810 |

Inventory is *faster*: sold-out fares are excluded before the branching cap, so
the search does less work and finds fewer itineraries (305 against 783) — that
is scarcity being real, not an optimization. The adaptive beam is 17× slower
and finds the trip V3 knew it was missing. Both numbers are the trade stated
rather than hidden.

The V3 optimization history still applies — the first V3 build ran 5.1× slower
than V2 until `canonical_key` was cached, Pareto tuples were precomputed,
`SearchState` was constructed directly, and `city_score` was memoized. With
`accommodation_options_per_stay=1` (V2's behaviour) the same request still runs
in **0.29 s** against V2's 0.283 s.

Provider cost, which is what would dominate against a real API: **12,078
lookups → 1,710 upstream calls** cold, **0** on any replan — unchanged from V3,
and now with a real provider behind it and an `HttpMetrics` counter on the
transport as well.

---

## Known limitations

### Implemented, with limits

**Data**

- Everything is synthetic. No external call is made anywhere. **No output should
  be read as a real price, a real hotel, or real availability.**
- Inventory is modelled but **synthetic**: room and seat counts are a
  deterministic function of city, date and tier, not a simulation of demand.
  They produce a realistic *shape* (the cheap rooms go first, some dates are
  much tighter) and nothing more. Off by default.
- Ground transfers are a static table plus a distance fallback — no traffic, no
  timetables, no public-transport API.
- The 12 destination attributes are hand-assigned editorial judgements about 16
  cities. They are internally consistent and deliberately not uniform, but they
  are not measurements of anything.
- Origin airports and destination cities are separate graph nodes, so `AMS` and
  `Amsterdam` have independent connections. This keeps "flying out of Schiphol"
  from counting as "visiting Amsterdam", at the cost of some duplication.

**Model**

- `AccommodationScore` still deliberately ignores price, because price is
  already the `cost` component. The gap V3 noted — that it could not say whether
  a room was *good value* — is closed by the `value_for_money` diagnostic, which
  is reported and never weighted. Its reference rate is calibrated against this
  dataset's tier ladder and must be re-derived if the tiers change; a test
  asserts that.
- Room tiers are branched two-deep by default. A trip could in principle want a
  five-star room for one night and a hostel for four; the engine will find that
  only if the tiers it fetched happen to include both.
- Rooms are assumed uniform: `ceil(travelers / capacity)` identical rooms, no
  family rooms, no single supplements, no breakfast.
- Travel intensity is calibrated against this dataset's European hop distances.
  `MAX_TRANSIT_SHARE = 0.15` is a judgement, not a measurement, and a
  long-haul catalog would need it re-tuned.
- The ideal city count comes from the catalog's recommended stay ranges plus a
  hop overhead. It is a good heuristic and it is not a preference model: a
  traveler who genuinely wants four cities in five days must say so with
  `preferred_city_count`, and even then a hard ceiling applies.
- Ground transfers are symmetric and time-independent — the same price and
  duration at 06:00 and at midnight.
- Stay length is whole calendar days. Sub-day connections are not modelled.
- The usable-day window is one global 08:00–21:00 setting: no seasonality, no
  opening hours, no jet lag.
- The shipped profile weights are still tuned against the synthetic dataset.
  They are now a *prior* rather than the answer: `learning.py` fits them from
  observed choices. The fit is biased towards the dominant component — inherent
  to learning a fixed-budget weight vector from choices alone — which is why it
  is anchored to that prior rather than trusted outright.

**Search**

- Beam search is a heuristic and nothing guarantees the global optimum. V3's
  finding — that the default beam misses a better trip — is now **addressed
  rather than merely reported**: `adaptive_beam=True` climbs until widening
  stops paying and finds the 0.6823 itinerary. It is off by default and costs
  ~17× the runtime, so the limitation is now a priced choice rather than an
  unknown.
- The ε-dominance grid is a deliberate approximation. Two itineraries within €5
  and 15 minutes of each other are treated as equivalent, so a genuinely
  slightly-better trip can be filtered as a near-duplicate. Set
  `OBJECTIVE_RESOLUTION` entries to `0` for exact comparison and a much larger
  frontier.
- `max_cities = 4` with a 5-day window makes four-city itineraries mostly
  infeasible — expect one- and two-city results for short trips.
- Only the first preferred destination drives the baseline.
- The date window is a hard boundary for every leg, arrivals included, so a trip
  cannot land the morning after `date_to`.
- Accommodation roughly halves the number of feasible itineraries at a given
  budget. That is correct, and it means budgets that worked in V1 may now return
  few results or none.

### Ready for future implementation, not implemented

V3's table had five rows. Four are now implemented; what remains is genuinely
one row plus the two providers nobody has written yet.

| | what exists | what is missing |
| --- | --- | --- |
| **Real transport API** | ✅ **implemented** — `AmadeusTransportProvider`, HTTP transport with retries/backoff/rate limiting, 57 tests against recorded payloads | a credential |
| **Real accommodation / transfer APIs** | the protocols, the caching decorators, and a worked example to copy in `providers/amadeus.py` | the two clients and their response mapping |
| **Currency conversion** | ✅ **wired end to end** — `Money`, `PriceBasis`, `PriceNormalizer`, applied at the provider boundary, and a loud failure when a rate is missing | a live rate feed |
| **LLM preference parsing** | ✅ **implemented** — `LlmPreferenceParser`, schema-constrained, validated, one retry, keyword fallback | nothing; install the `llm` extra |
| **LLM explanations** | ✅ **implemented** — `LlmItineraryExplainer` with the numeric grounding guard and template fallback | nothing; install the `llm` extra |
| **Learned profiles** | ✅ **implemented** — `learning.py`, fitted from `observations_from_result` | a place to persist a cohort's history |
| **Persistent caching** | in-process `ProviderCache` with hit/miss metrics | TTLs, eviction, a shared store |

The distinction still matters and still holds: the deterministic core does not
import any of it. Two AST tests enforce that — one for `travel_planner.llm`,
one for `travel_planner.learning` — and a third asserts that no module under
`algorithms/` or `constraints/` imports a networking library.

**Not built, deliberately**: restaurants, visas, weather, maps, auth, a
database, a frontend.

---

## What a V5 would be for

V4 finished V3's list. What is left is no longer a list of missing pieces — it
is the two questions this design has not had to answer yet.

1. **Real data will break the admissible bounds.** Every pruning bound here is
   admissible because the synthetic dataset is complete and static. A live feed
   is neither: prices move between the bound and the booking, and a bound
   computed from a cached minimum is only admissible until the cache is stale.
   The honest V5 answer is probably to make bounds *probabilistic* and let the
   search prune on a confidence level it reports, rather than to pretend to a
   guarantee it no longer has.
2. **The engine has no notion of a traveler who is wrong about what they want.**
   It optimizes the stated request faithfully, and the learning module now
   corrects the *weights* from behaviour — but a traveler who asks for four
   cities in five days gets an argument from `city_count`, not a conversation.
   That is a product question before it is an engineering one, and it is the
   first thing here that genuinely wants the LLM seams to be more than
   one-directional.

Everything else — the second and third real providers, persistence, a frontend —
is work, not design.
