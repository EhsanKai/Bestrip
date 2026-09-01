# Travel Intelligence Engine

A deterministic, multi-objective trip optimizer. It answers:

> *Given my money, my time and my preferences, what is the best trip you can build me?*

Not "find me a cheap flight". The optimizer prices and scores the **whole
trip** — the ride to the airport, the flights and trains, the hotel and how
good it is, the days you actually get to spend somewhere, and how well the
cities match the traveler — then searches the space of complete round trips for
the best one under the profile you ask for.

> ⚠️ **All data is synthetic.** Flights, trains, buses, hotel rates, ratings,
> airport transfers and destination metadata are fabricated to exercise the
> optimizer. Nothing here reflects real prices or availability.

---

## Table of contents

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

pytest                                            # 443 tests
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
├── providers/
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
├── llm/interfaces.py          PreferenceParser / ItineraryExplainer seams
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

Implement against Amadeus / Kiwi / DB for transport, Booking / Expedia for
rooms, a routing API for transfers, normalize into the domain models, and pass
the instances to `TravelPlanner(...)`. `Real*Provider` classes mark each seam
and deliberately raise `NotImplementedError` — the MVP must not present invented
numbers as real availability.

V3 added the three things a real integration would otherwise have to invent.

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
  provider aborts a search that had 800 other viable itineraries.
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
             (LLM may live here)                 (never an LLM)                             (LLM may live here)
```

`llm/interfaces.py` defines both protocols and ships dependency-free local
implementations (`KeywordPreferenceParser`, `TemplateItineraryExplainer`) so the
pipeline runs end to end with no external API. **No LLM is called anywhere in
this repository**, and a test walks the AST of every module in `algorithms/`,
`constraints/`, `services/`, `providers/` and `data/` asserting that none of
them imports the `llm` package at all.

Prices, availability, travel time, route feasibility, constraints and scores are
never a model's job. What V3 added is the *material* an explainer would consume,
so that the seam is genuinely usable rather than nominal:

| structured input | what an explainer would say with it |
| --- | --- |
| `explanation_factors` | which claims are true of this trip, as typed flags |
| `destination_insights[j].strengths` / `.weaknesses` | *why this city*, in the traveler's own preference terms |
| `destination_insights[j].stay_note` | whether the stay length suits the city |
| `value_breakdown` (9 components + weights) | which trade-off the profile actually made |
| `cost_breakdown`, `stays[j]` | where the money went, tier by tier |
| `baseline_comparison` | how it compares to what they asked for |

The contract is one-directional: the explainer restates, it never computes.
Because every field above is derived from the finished itinerary, a generated
sentence cannot contradict the optimization — the worst an explainer can do is
omit something, not invent it.

The same is true in front. `PreferenceParser` returns a `TripRequest`, which is
a validated pydantic model: an LLM that hallucinates a destination or an
experience name produces a `422`, not a silently wrong search. That validation
was tightened in V3 precisely because it is the layer that would eventually face
model output.

---

## Running the tests

```bash
pytest                                       # 443 tests
pytest tests/test_beam_search.py -v          # the non-greedy proof
pytest tests/test_adversarial.py -v          # the 20 V2 spec scenarios
pytest tests/test_v3_adversarial.py -v       # the 13 V3 scenarios
pytest -k "not api"                          # skip the FastAPI tests
```

**All 323 V2 tests still pass. None was deleted, skipped, or weakened.**
Sixteen were *updated*, each because V3 intentionally changed the behaviour the
test asserted, and each with the reason written into the test:

| what changed | tests touched | why |
| --- | --- | --- |
| beam scaling on flexible dates | 3 | `metadata.beam_width` is now the effective width; the configured one moved to `configured_beam_width` |
| exact route-duplicate stage | 2 | near-duplicates are usually removed by `DUPLICATE_ROUTE` now, so the assertion accepts either similarity stage |
| nine-component Travel Value | 4 | weight sets are asserted against `COMPONENTS` instead of five hard-coded names; `experience_score` takes the request |
| stay quality moved into the experience engine | 1 | asserted through `assess_experience(...).stay_quality` |
| room-tier branching | 1 | the party-size test pins one tier, so it stays a test about party pricing rather than tier selection |
| eight-objective Pareto | 2 | the ground-transfer trap now asserts on what the *search found* (via the new `completed_states` helper) rather than on what survived filtering — the losing airport is now correctly Pareto-dominated |
| **the return-bound admissibility fix** | 1 | `test_missing_return_connection_is_pruned` asserted the bug: a city with no *direct* flight home was pruned even though it was reachable onward. Renamed to `test_an_unknown_return_bound_does_not_prune` and inverted. |
| BEST_VALUE no longer promises "cheaper" | 1 | the profile can legitimately prefer a dearer, richer trip; the test now checks the comparison is *correct*, and separately that CHEAPEST still undercuts the baseline |

The last two are the ones worth arguing about, and both were changed because
the old assertion was wrong about the world, not because the new code was
inconvenient. One V2 test was added (exact-duplicate collapse), which is why
`test_diversity.py` shows 14.

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
| | **total** | **443** |

Determinism is asserted, not assumed: several tests plan the same request twice
and compare the full result object, and the budget sweep is compared for exact
equality across runs.

---

## Performance

Measured on the standard request (Köln, €450, 2 travelers, 5 days, prefers
Madrid, avoids Paris), cold caches, best of three runs, on the same machine.

| | V2 | V3 | states generated | completed |
| --- | --- | --- | --- | --- |
| fixed dates | 0.283 s | **0.397 s** | 3,086 → 5,804 | 480 → 385 |
| flexible dates | 0.284 s | **0.830 s** | 3,248 → 12,374 | 480 → 783 |
| 7 days, €600 | 0.570 s | **0.739 s** | 4,698 → 8,428 | 882 → 833 |

V3 is 1.3–2.9× slower, and it is slower **because it searches more**, not
because it got sloppier. Two lines are worth reading carefully.

**The flexible-dates row is a bug fix, not a regression.** V2 spent 0.284 s and
generated 3,248 states on a flexible request — essentially the same as on the
fixed one. That is the whole problem: this request has two viable start dates
rather than one, so the search space doubles, and V2 spread the same 20-wide
beam across both of them. *Offering flexibility made the search worse.* V3
scales the beam with the number of start dates (20 → an effective 40), generates
12,374 states, and finds 783 complete itineraries against V2's 480. Roughly
three times the time for 63% more finished trips and a genuinely wider search.
A longer window means more start dates and a proportionally wider beam, up to
`max_effective_beam_width`.

**The fixed-dates row is accommodation branching.** Each stay now branches into
two room tiers, which roughly doubles the state count. With
`accommodation_options_per_stay=1` — one tier, no trade-off to make, V2's
behaviour — V3 runs the same request in **0.293 s** against V2's 0.283 s. That
is the honest measure of V3's added machinery: within noise of V2, despite the
12-attribute experience model, nine-component scoring, eight-objective Pareto
and full provider instrumentation.

Getting there took work. The first V3 build ran **5.1× slower** than V2. cProfile
found four causes, none of them in the search itself:

| fix | why it mattered |
| --- | --- |
| `lru_cache` on `canonical_key` / `normalize_key` | Unicode normalization of city names, called on every candidate |
| precomputed Pareto tuples | objective vectors were rebuilt inside the O(n²) dominance loop |
| direct `SearchState` construction | `dataclasses.replace` was ~40% of state extension |
| memoized `ExperienceEngine.city_score` | the same (city, preferences) pair scored thousands of times |

Note also that the Pareto frontier moved from 22 (V2, four objectives) to
59–123 (V3, eight objectives with ε-dominance). More objectives means a larger
frontier by construction; without the ε grid the same run keeps 248 rather than
124, which is a frontier doing half as much work.

Provider cost, which is what would actually dominate against a real API: the
flexible-date run above issues **12,096 lookups** and makes **1,713 upstream
calls** on a cold cache, and **0** on any replan.

---

## Known limitations

### Implemented, with limits

**Data**

- Everything is synthetic. No external call is made anywhere. **No output should
  be read as a real price, a real hotel, or real availability.**
- Rooms never sell out and flights never fill; there is no inventory model.
- Ground transfers are a static table plus a distance fallback — no traffic, no
  timetables, no public-transport API.
- The 12 destination attributes are hand-assigned editorial judgements about 16
  cities. They are internally consistent and deliberately not uniform, but they
  are not measurements of anything.
- Origin airports and destination cities are separate graph nodes, so `AMS` and
  `Amsterdam` have independent connections. This keeps "flying out of Schiphol"
  from counting as "visiting Amsterdam", at the cost of some duplication.

**Model**

- `AccommodationScore` deliberately ignores price. Price is already the `cost`
  component, and scoring it twice would make expensive rooms lose on both.
  The consequence is that the accommodation component alone cannot tell you
  whether a room was *good value* — only whether it was good.
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
- Profile weights are tuned against the synthetic dataset. They are a starting
  point, not a claim about real travelers.

**Search**

- Beam search is a heuristic, and on the standard scenario **the default beam
  demonstrably misses a better trip**: `beam_width=40` finds a 0.6802 itinerary
  the default 20 never reaches, for twice the runtime. Nothing guarantees the
  global optimum; `beam_width` is the knob, and both its benefit and its cost
  are measured rather than assumed.
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

These are seams with defined contracts, local stand-ins and tests — the work
that remains is an integration, not a redesign.

| | what exists | what is missing |
| --- | --- | --- |
| **Real transport / accommodation / transfer APIs** | three protocols, `Real*Provider` stubs that raise `NotImplementedError`, caching decorators, call metrics | the HTTP clients and their response mapping |
| **Currency conversion** | `Money`, `PriceBasis`, `PriceNormalizer`, `ExchangeRateSource`, `FixedExchangeRates` | a live rate feed |
| **LLM preference parsing** | `PreferenceParser` protocol, `KeywordPreferenceParser` stand-in, a validated `TripRequest` as the contract | a model call, and the prompt/guardrails around it |
| **LLM explanations** | `ItineraryExplainer` protocol, `TemplateItineraryExplainer`, typed factors and per-city insights as structured input | a model call |
| **Persistent caching** | in-process `ProviderCache` with hit/miss metrics | TTLs, eviction, a shared store |

The distinction matters: the deterministic core does not import any of the
right-hand column, and adding it cannot change a score.

**Not built, deliberately**: restaurants, visas, weather, maps, auth, a
database, a frontend, and any LLM call at all.
