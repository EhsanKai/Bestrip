# Intelligent Budget Travel Planner

A deterministic, multi-objective trip optimizer. It answers:

> *Given my money, my time and my preferences, what is the best trip you can build me?*

Not "find me a cheap flight". The optimizer prices and scores the **whole
trip** — the ride to the airport, the flights and trains, the hotel, the days
you actually get to spend somewhere — and searches the space of complete round
trips for the best one under the profile you ask for.

> ⚠️ **All data in this MVP is synthetic.** Flights, trains, buses, hotel rates
> and airport transfers are fabricated to exercise the optimizer. Nothing here
> reflects real prices or availability.

---

## Table of contents

- [What V2 changed](#what-v2-changed)
- [The three traps](#the-three-traps)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [How the algorithm works](#how-the-algorithm-works)
  - [1. Origin discovery and ground transfer](#1-origin-discovery-and-ground-transfer)
  - [2. Beam search](#2-beam-search)
  - [3. Accommodation](#3-accommodation)
  - [4. Constraints](#4-constraints)
  - [5. Travel Value](#5-travel-value)
  - [6. Usable destination time](#6-usable-destination-time)
  - [7. Pareto filtering](#7-pareto-filtering)
  - [8. Diversity filtering](#8-diversity-filtering)
  - [9. Baseline comparison](#9-baseline-comparison)
- [Recommendation profiles](#recommendation-profiles)
- [Worked example](#worked-example)
- [Observability](#observability)
- [Configuration](#configuration)
- [The API](#the-api)
- [Synthetic data](#synthetic-data)
- [Plugging in real APIs](#plugging-in-real-apis)
- [Where an LLM fits](#where-an-llm-fits)
- [Running the tests](#running-the-tests)
- [Known limitations](#known-limitations)

---

## What V2 changed

V1 was a route optimizer that happened to be good at it. V2 is a trip
optimizer. The search, the constraint engine, the Pareto and diversity filters
and the non-greedy guarantee are all unchanged — what changed is *what gets
priced* and *what "best" means*.

| | V1 | V2 |
| --- | --- | --- |
| Cost of a trip | transport only | transport **+ accommodation + ground transfer** |
| A city stay | free | priced per room per night, rooms scale with party size |
| Getting to the airport | free and instant | priced and timed per airport |
| Objective | 6 components, budget-dominated (0.40) | **Travel Value**: 5 components under a named profile |
| "5 days" | elapsed calendar time | **usable destination time**, arrival and departure clock included |
| One answer for everyone | yes | **CHEAPEST / BEST_VALUE / ADVENTURE** |
| Duration measured | from midnight of the start date | from the actual first departure |
| Explanation | score components | structured `explanation_factors` + full cost/time breakdown |

**The headline consequence.** Once a hotel and a train to the airport are part
of the price, €250 for two people over five days buys exactly one trip in the
synthetic network (`CGN → Brussels → CGN`, €241.78) and no Madrid baseline at
all. That is not a regression — it is the model finally telling the truth. The
worked examples use €450.

---

## The three traps

The project is built around three synthetic scenarios where the naive answer is
wrong. Each has a dedicated fixture and test.

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

---

## Quick start

```bash
pip install -e ".[dev]"

pytest                                        # 323 tests
python examples/profiles_demo.py              # scenarios A, B and C
python examples/profiles_demo.py --budget 250 # what V2 economics do to the V1 budget
python examples/koln_scenario.py --debug      # one profile, full search trace

uvicorn travel_planner.api.app:app --reload   # http://127.0.0.1:8000/docs
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
    ),
    profile=ProfileName.ADVENTURE,     # or BEST_VALUE (default), or CHEAPEST
)

for itinerary in result.recommendations:
    print(itinerary.rank, itinerary.route_label(), itinerary.total_cost)
    print("  ", itinerary.cost_breakdown)
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
├── profiles.py                ProfileName, TravelValueWeights, PROFILES  (V2)
├── usable_time.py             the usable-day model                       (V2)
├── models/
│   ├── trip.py                TripRequest, TravelPreferences
│   ├── transport.py           TransportOption, TransportType
│   ├── accommodation.py       AccommodationOption, AccommodationTier     (V2)
│   ├── transfer.py            GroundTransferOption                       (V2)
│   ├── destination.py         Destination
│   ├── search.py              SearchState, CityStay
│   ├── itinerary.py           Itinerary, CostBreakdown, TravelValueBreakdown,
│   │                          StaySummary, ExplanationFactor, PlanResult
│   └── debug.py               RejectionReason, IterationDebug, SearchDebug
├── data/
│   ├── destinations.py        city catalog + origin-airport distances
│   ├── synthetic_transport.py the transport network
│   ├── synthetic_accommodation.py  nightly rates and tiers               (V2)
│   └── ground_transfers.py    origin ↔ airport table                     (V2)
├── providers/
│   ├── transport.py           TransportDataProvider  + Synthetic / Real
│   ├── accommodation.py       AccommodationDataProvider + Synthetic / No / Real
│   ├── ground_transfer.py     GroundTransferProvider + Synthetic / Free / Real
│   └── destinations.py        DestinationProvider
├── algorithms/
│   ├── beam_search.py         the state-space search
│   ├── travel_value.py        the V2 objective                           (V2)
│   ├── scoring.py             the V1 six-component engine (still used)
│   ├── pareto.py              domination and frontier
│   └── diversity.py           Jaccard-based diversification
├── constraints/validator.py   ConstraintValidator + the estimator protocols
├── services/
│   ├── planner.py             TravelPlanner — the entry point
│   ├── baseline.py            the naive single-destination round trip
│   ├── origin_resolver.py     "Köln" → CGN, DUS, FRA, EIN
│   ├── return_estimator.py    lower bounds on getting home
│   ├── accommodation_estimator.py  lower bounds on sleeping              (V2)
│   └── explanation.py         structured explanation factors             (V2)
├── llm/interfaces.py          PreferenceParser / ItineraryExplainer seams
└── api/                       FastAPI adapter (optional)
```

Dependency direction is strict: `api → services → algorithms → constraints →
providers → models`. `usable_time.py` and `profiles.py` sit at the root because
`config` needs them. The optimizer never imports `llm` — a test parses the ASTs
and fails if it does.

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
Pareto filter        drop dominated itineraries
     │
     ▼
Diversity filter     drop near-duplicate trips
     │
     ▼
Top N + baseline comparison + explanation factors
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

### 2. Beam search

One iteration adds one leg. From any state there are two actions: **continue**
to an unvisited city, or **return** to an origin airport. Stay lengths are
searched, not assumed — every stay length in
`min_city_stay_days … max_city_stay_days` yields a different departure date and
therefore a different state.

```
initial states (airport × start date, ground transfer pre-charged)
        ↓
generate next states (continue / return × stay length × timetable × room)
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
ride from that airport, plus the nights the traveler must still pay for**. A
state sitting in London with an €85 return and a €78 room is judged on the trip
it implies, not on the €35 leg that got it there. That last term is what makes
trap 2 work.

Three things keep it practical:

- **Admissible pruning.** A state is dropped when its cheapest possible
  return + cheapest possible remaining nights already exceed the remaining
  budget, or its fastest possible return exceeds the remaining time. Both
  bounds are minima over the whole window, so pruning never discards a feasible
  itinerary.
- **Beam spread** (`beam_slots_per_route`). At most two beam slots go to any one
  city sequence, so the beam doesn't fill with the same trip from four airports.
- **Determinism.** Sorted iteration everywhere; ties break on cost then on the
  state's leg-id signature. Asserted per profile, three runs deep.

### 3. Accommodation

Prices are **per room per night**; a room sleeps `capacity` people and the
search books `ceil(travelers / capacity)` of them. Three tiers exist in every
city (budget ≈ 0.65×, standard, comfort ≈ 1.55×), and Friday/Saturday nights
carry a deterministic surcharge.

By default the search books the **cheapest sufficient room**
(`accommodation_options_per_stay = 1`), which keeps the state space the same
size as V1. Raise it to let the search trade room quality against everything
else, at a multiplicative cost in states.

A required stay with no bookable room is a `NO_ACCOMMODATION_AVAILABLE`
rejection, not a silently skipped candidate.

### 4. Constraints

All feasibility rules live in `ConstraintValidator`. Each failure returns a
typed reason:

| Rule | Reason |
| --- | --- |
| Party total over budget | `BUDGET_EXCEEDED` |
| Trip span over the allowance | `DURATION_EXCEEDED` |
| Trip far shorter than requested | `DURATION_UNDERUSED` |
| Cheapest remaining nights unaffordable | `UNAFFORDABLE_ACCOMMODATION` |
| No room available for a required stay | `NO_ACCOMMODATION_AVAILABLE` |
| Visits an avoided city | `AVOIDED_DESTINATION` |
| Finished without a must-visit city | `MISSING_MANDATORY_DESTINATION` |
| Same city twice | `DUPLICATE_DESTINATION` |
| Left a city too soon | `MIN_CITY_STAY_VIOLATED` |
| Too many cities | `MAX_CITIES_EXCEEDED` |
| Mode the user excluded | `TRANSPORT_TYPE_NOT_ALLOWED` |
| Leg outside the date window | `DATE_WINDOW_VIOLATED` |
| Did not end at an origin airport | `NOT_RETURNED_TO_ORIGIN` |
| Cheapest return already unaffordable | `UNREACHABLE_RETURN_BUDGET` |
| Fastest return no longer fits | `UNREACHABLE_RETURN_TIME` |
| Legs do not chain | `INVALID_CONNECTION` |

**Preferred vs. mandatory.** `preferred_destinations` raises the score;
`must_visit` is hard, and steers the *search* as well as the final check — the
beam ranks progress towards mandatory destinations ahead of score.
**Avoided destinations are rejected, never merely penalized**, matched
accent- and case-insensitively (`"wien"` excludes Vienna).

**Dates.** The window `date_from … date_to` and the trip length `duration_days`
are separate. A 5-day trip in a 10-day window is 5 days, not 10. With
`date_flexible = true` every start date that fits is searched; with `false`
only `date_from` is. Duration is measured as the trip's **span** — first
departure to last arrival — so an afternoon flight doesn't quietly spend a
morning of the allowance.

### 5. Travel Value

Five components, each in `[0, 1]`, weighted by the active profile.

**CostScore** blends efficiency with sensible use of the budget:

```
utilization  = total_cost / budget
efficiency   = clamp(1 - utilization)
sensible_use = clamp(utilization / budget_utilization_target)     # target 0.6
CostScore    = (1 - w) · efficiency + w · sensible_use            # w = profile's
                                                                  #     budget_utilization_weight
```

With `w = 0` (CHEAPEST) this is pure efficiency and strictly monotone: cheaper
always wins. With `w = 0.35` (BEST_VALUE) spending up to ~60% of the budget
costs almost nothing, so a better trip is free to be dearer — €100 → €150 barely
moves the score, €150 → €350 clearly does.

**ExperienceScore** = mean of destination quality (intrinsic city appeal),
stay quality (usable days against each city's recommended range) and pace
(days per city against `comfortable_days_per_city`).

**PreferenceScore** = `0.65 ·` taste match `+ 0.35 ·` wish-list coverage, where
taste is the V1 blend of attribute match with the `multiple_cities` appetite:

```
attribute_match = Σ wₐ · cityₐ / Σ wₐ          per city, then averaged
multi_component = 1 − 1/n                     n = number of cities
taste           = (attribute_match + m · multi_component) / (1 + m)
```

`multiple_cities = 0` gives **zero** benefit from extra cities; `= 1` weights
city count equally with city quality. The `1 − 1/n` curve makes one → two the
meaningful jump and three → four barely register.

**TimeScore** = `0.6 ·` usable ratio `+ 0.4 ·` transit quality. See below.

**DiversityScore** = mean of country variety, transport-mode variety and the
`1 − 1/n` city-count curve — which is the lever ADVENTURE pulls on.

### 6. Usable destination time

A day of a trip is not a day of sightseeing. Each calendar day contributes the
overlap between the traveler's presence and a configurable usable-day window
(default 08:00–21:00):

```
arrive 23:30  → the arrival day contributes 0
depart 06:00  → the departure day contributes 0
a day in between → a full 780 minutes
```

That one function delivers four requirements at once: late arrivals and early
departures cost usable time, a two-day trip cannot masquerade as a five-day
one, and a 15-hour stop in a city that wants two days scores accordingly.
`usable_ratio` is usable minutes over `duration_days × usable_day_minutes`.

### 7. Pareto filtering

An itinerary is **dominated** when another is no worse on all four objectives —
cost (↓), travel time (↓), city count (↑), preference score (↑) — and strictly
better on at least one. Dominated itineraries are inferior under *any*
weighting. In a typical run this cuts ~500 completed itineraries to ~25.

### 8. Diversity filtering

A greedy deterministic pass over the ranked frontier: an itinerary is accepted
only if the Jaccard similarity of its city set against every accepted one stays
at or below the threshold (0.5, or 0.6 under ADVENTURE). If that is too strict
to fill `max_results`, the remaining slots are back-filled in rank order rather
than returning a short list.

### 9. Baseline comparison

The cheapest simple round trip to the user's first preferred destination —
what a conventional search would return — **including its hotel and airport
transfers**, and long enough to satisfy `min_duration_utilization`. Without
that last rule the baseline degenerates into a one-night flying visit that
undercuts every real trip and makes `money_saved` meaningless.

No preferred destination means **no baseline**. The planner does not invent one.

---

## Recommendation profiles

There is no single right answer to "what is the best trip?".

| | cost | experience | preferences | time | diversity | budget utilization |
| --- | --- | --- | --- | --- | --- | --- |
| **CHEAPEST** | 0.70 | 0.10 | 0.10 | 0.05 | 0.05 | 0.00 |
| **BEST_VALUE** (default) | 0.25 | 0.30 | 0.20 | 0.15 | 0.10 | 0.35 |
| **ADVENTURE** | 0.20 | 0.25 | 0.15 | 0.10 | 0.30 | 0.40 |

Profiles are data (`travel_planner/profiles.py`), not scattered constants, and
may also override `min_duration_utilization` and the diversity threshold.
Callers can build their own `RecommendationProfile`.

---

## Worked example

Köln, €450, 2 travelers, 5 days, prefers Madrid, avoids Paris,
`multiple_cities = 0.9`. Baseline: **Madrid €338.47** (3 nights — €179
transport + €139 rooms + €20 transfer).

**CHEAPEST** — ordered by price, single cities:

```
#1  0.5106   €241.78   T 103.8  A 118.0  G 20.0   3.1d  58%   CGN → Brussels → CGN
#2  0.4678   €277.50   T 139.5  A 118.0  G 20.0   3.0d  59%   CGN → Berlin → CGN
#3  0.4145   €309.36   T 149.6  A  85.8  G 74.0   3.1d  58%   EIN → Budapest → DUS
```

**BEST_VALUE** — a dearer, longer, two-city trip climbs to #2:

```
#1  0.6747   €277.50   T 139.5  A 118.0  G 20.0   3.0d  59%   CGN → Berlin → CGN
#2  0.6694   €366.64   T 201.8  A 124.8  G 40.0   4.1d  76%   DUS → Budapest → Vienna → CGN
#3  0.6668   €360.25   T 204.7  A 135.5  G 20.0   4.1d  75%   CGN → Prague → Vienna → CGN
```

**ADVENTURE** — the two-city trips take the top:

```
#1  0.6648   €385.16   T 220.4  A 124.8  G 40.0   3.8d  65%   DUS → Budapest → Vienna → CGN
#2  0.6624   €379.95   T 224.4  A 135.5  G 20.0   3.8d  64%   CGN → Prague → Vienna → CGN
#3  0.6390   €304.16   T 166.2  A 118.0  G 20.0   3.0d  58%   CGN → Berlin → CGN
```

Note `#2` under BEST_VALUE: €89 dearer than `#1` and it still scores higher,
because it buys a second city, a fourth day and 76% usable time instead of 59%.
Under CHEAPEST the same trip is nowhere. **Score and cost are different
questions, and the API exposes both.**

---

## Observability

Everything discarded is recorded as a typed object, never a log string. Run
counters are always collected; `debug=True` returns the whole trace.

```python
result = planner.plan(request, debug=True)
print(result.debug.render())
```

```
Iteration 2
------------------
States in:    20
Generated:    2000
Rejected:     1167
Remaining:    527
Beam width:   20
Beam pruning: 507
Kept:         20
Completed:    306
Rejections:
  DURATION_UNDERUSED: 480
  TRANSPORT_TYPE_NOT_ALLOWED: 208
  UNREACHABLE_RETURN_BUDGET: 189
  AVOIDED_DESTINATION: 112
  BUDGET_EXCEEDED: 103
  UNREACHABLE_RETURN_TIME: 64
  UNAFFORDABLE_ACCOMMODATION: 11
```

| What you want to know | Where |
| --- | --- |
| Why a state was rejected | `debug.iterations[i].rejected_examples[j].reason` / `.detail` |
| Why a state lost the beam | `debug.iterations[i].pruned_examples[j].estimated_score` |
| Why an itinerary scored what it did | `itinerary.value_breakdown` (5 components + weights) |
| Where the money went | `itinerary.cost_breakdown` (transport / accommodation / ground_transfer) |
| Where the time went | `total_travel_minutes`, `ground_transfer_minutes`, `usable_destination_minutes` |
| What each stay cost and bought | `itinerary.stays[j]` |
| Why it is worth showing | `itinerary.explanation_factors` |
| Why an itinerary was Pareto-filtered | `debug.filtered` where `stage == PARETO`, with `dominated_by` |
| Why an itinerary was diversity-filtered | `debug.filtered` where `stage == DIVERSITY`, with `similarity` |

`explanation_factors` are typed flags (`strong_preference_match`,
`good_budget_usage`, `two_cities`, `reasonable_travel_time`,
`late_arrival`, …) derived deterministically from the itinerary. The optimizer
states facts; prose is the LLM layer's job, and because every flag comes from
the itinerary it cannot drift from what was computed.

---

## Configuration

```python
PlannerConfig(
    beam_width=20, max_results=5, max_cities=4, beam_slots_per_route=2,

    min_city_stay_days=1, max_city_stay_days=4,
    min_duration_utilization=0.6,

    max_origin_distance_km=250.0, max_origin_airports=4,

    # V2
    enable_accommodation=True,
    accommodation_options_per_stay=1,
    enable_ground_transfer=True,
    usable_day_start=time(8, 0), usable_day_end=time(21, 0),
    profile=ProfileName.BEST_VALUE,
    budget_utilization_target=0.6,
    comfortable_days_per_city=2.0,

    score_weights=ScoreWeights(...),        # the V1 diagnostic engine
    max_travel_time_fraction=0.25,
    preferred_destination_bonus=0.5, must_visit_bonus=0.3,

    enable_pareto=True, enable_diversity=True,
    diversity_similarity_threshold=0.5,
)
```

`enable_accommodation=False` and `enable_ground_transfer=False` restore the V1
economics exactly, and a test asserts it.

---

## The API

The API is only an adapter. A test plans the same request directly and over
HTTP and asserts the two agree.

```
POST /plan-trip[?debug=true][&profile=CHEAPEST|BEST_VALUE|ADVENTURE]
GET  /profiles                 the profiles and their weights
GET  /destinations             the synthetic catalog
GET  /config                   the active PlannerConfig
GET  /health
```

The profile may also be set in the request body (`"profile": "ADVENTURE"`); the
query parameter wins. Omitted, it is `BEST_VALUE`.

Response (abridged — synthetic data):

```json
{
  "profile": "BEST_VALUE",
  "baseline": {
    "destination": "Madrid",
    "total_cost": 338.47,
    "nights": 3,
    "cost_breakdown": {"transport": 179.06, "accommodation": 139.41, "ground_transfer": 20.0}
  },
  "recommendations": [
    {
      "rank": 2,
      "score": 0.6694,
      "total_cost": 366.64,
      "currency": "EUR",
      "cost_breakdown": {"transport": 201.84, "accommodation": 124.8, "ground_transfer": 40.0},
      "cities": ["Budapest", "Vienna"],
      "stay_days": [3, 1],
      "stays": [
        {"city": "Budapest", "nights": 3, "accommodation_cost": 85.8,
         "accommodation_tier": "budget", "usable_minutes": 2270}
      ],
      "duration_days": 4.07,
      "total_travel_minutes": 360,
      "ground_transfer_minutes": 75,
      "usable_destination_minutes": 2970,
      "value_breakdown": {
        "profile": "BEST_VALUE",
        "cost": 0.4704, "experience": 0.8245, "preferences": 0.6073,
        "time": 0.7382, "diversity": 0.7222,
        "total": 0.6694, "usable_ratio": 0.7615, "budget_utilization": 0.8148
      },
      "baseline_comparison": {"baseline_cost": 338.47, "money_saved": -28.17},
      "explanation_factors": [
        "fits_budget", "good_budget_usage", "low_accommodation_cost",
        "high_destination_quality", "two_cities", "reasonable_travel_time",
        "good_use_of_window"
      ]
    }
  ],
  "metadata": {"profile": "BEST_VALUE", "origin_airports": ["CGN", "DUS", "EIN", "FRA"], "...": "..."}
}
```

Invalid input (unknown origin, unknown profile, contradictory destinations,
negative budget) returns `422`.

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

Adding a city means one `Destination` entry, a nightly rate and a few `_link(…)`
rows.

---

## Plugging in real APIs

Three protocols, no algorithm changes:

```python
class TransportDataProvider(Protocol):
    def search(self, origin, destination, departure_date) -> list[TransportOption]: ...

class AccommodationDataProvider(Protocol):
    def search(self, city, check_in, check_out, travelers) -> list[AccommodationOption]: ...
    def min_price_per_night(self, city, travelers) -> float | None: ...

class GroundTransferProvider(Protocol):
    def search(self, origin, airport) -> list[GroundTransferOption]: ...
```

Implement against Amadeus / Kiwi / DB for transport, Booking / Expedia for
rooms, a routing API for transfers, normalize into the domain models, and pass
the instances to `TravelPlanner(...)`. `Real*Provider` classes mark each seam
and deliberately raise `NotImplementedError` — the MVP must not present invented
numbers as real availability.

Notes for that step: cache aggressively (the search issues thousands of calls,
which the synthetic providers memoize); never raise on "nothing found", return
an empty list; keep results deterministic within a run, or the determinism
guarantee weakens to "stable for a fixed snapshot". `min_price_per_night` must
stay an *admissible* lower bound or pruning can discard feasible trips.

---

## Where an LLM fits

```
free text ─► PreferenceParser ─► TripRequest ─► [ deterministic optimizer ] ─► Itinerary ─► ItineraryExplainer ─► prose
             (LLM may live here)                 (never an LLM)                             (LLM may live here)
```

`llm/interfaces.py` defines both protocols and ships dependency-free local
implementations so the pipeline runs end to end with no external API. Prices,
availability, travel time, route feasibility and scores are never an LLM's job.
`explanation_factors` plus the cost and time breakdowns are the structured
input a generative explainer would consume — it restates, it does not compute.

---

## Running the tests

```bash
pytest                                    # 323 tests
pytest tests/test_beam_search.py -v       # the non-greedy proof
pytest tests/test_adversarial.py -v       # the 20 spec scenarios
pytest -k "not api"                       # skip the FastAPI tests
```

| File | Covers |
| --- | --- |
| `test_models.py` | model validation, per-person vs. total price, state transitions |
| `test_constraints.py` | every rejection reason, pruning bounds |
| `test_scoring.py` | the V1 engine, each component formula, reweighting |
| `test_beam_search.py` | **trap 1**, beam width, determinism, stay lengths |
| `test_pareto.py` | domination algebra and the frontier |
| `test_diversity.py` | Jaccard, deduplication, back-fill |
| `test_baseline.py` | baseline round trip and comparisons |
| `test_providers.py` | dataset coverage, trap 1's data property, origin resolution |
| `test_api.py` | HTTP shape, error codes, adapter equivalence |
| `test_llm_interfaces.py` | the seams, and that the optimizer ignores them |
| `test_end_to_end.py` | the full Köln scenario, requirement by requirement |
| `test_v2_accommodation.py` | rooms, tiers, capacity, pruning, **trap 2** |
| `test_v2_ground_transfer.py` | transfers, door-to-door cost, **trap 3** |
| `test_v2_usable_time.py` | the usable-day model and its effect on scoring |
| `test_v2_profiles.py` | Travel Value, the three profiles, §39/§40 scenarios |
| `test_v2_explainability.py` | every exposed figure, factors, API surface |
| `test_adversarial.py` | the 20 numbered scenarios from the spec |

---

## Known limitations

**Data**

- Everything is synthetic. No external call is made. **No output should be read
  as a real price, a real hotel, or real availability.**
- Rooms never sell out and flights never fill; there is no inventory model.
- Ground transfers are a static table plus a distance fallback — no traffic, no
  timetables, no public-transport API.
- Origin airports and destination cities are separate graph nodes, so `AMS` and
  `Amsterdam` have independent connections. It keeps "flying out of Schiphol"
  from counting as "visiting Amsterdam", at the cost of some duplication.

**Model**

- The search books the **cheapest sufficient room** by default. Room quality
  therefore does not trade against anything unless you raise
  `accommodation_options_per_stay`, and `AccommodationOption.rating` is carried
  but unused by the scorer.
- Rooms are assumed uniform: `ceil(travelers / capacity)` identical rooms, no
  family rooms, no single supplements, no breakfast.
- Ground transfers are symmetric and time-independent — the same price and
  duration at 06:00 and at midnight.
- Stay length is whole calendar days; a stay of *n* days means departing *n*
  days after arriving. Sub-day connections are not modelled.
- The usable-day window is one global 08:00–21:00 setting: no seasonality, no
  opening hours, no jet lag.
- `min_duration_utilization` is an addition to the spec, not part of it. It is
  configurable and can be switched off.
- Profile weights are tuned against the synthetic dataset. They are a starting
  point, not a claim about real travelers.

**Search**

- Beam search is a heuristic. A wider beam explores more but nothing guarantees
  the global optimum; `beam_width` is the knob.
- `max_cities = 4` with a 5-day window makes four-city itineraries mostly
  infeasible — expect one- and two-city results for short trips.
- Only the first preferred destination drives the baseline.
- The date window is a hard boundary for every leg, arrivals included, so a trip
  cannot land the morning after `date_to`.
- Adding accommodation roughly halved the number of feasible itineraries at a
  given budget. That is correct, but it means budgets that worked in V1 may now
  return few results or none.

**Not built** (deliberately): real APIs, restaurants, visas, weather, maps,
LLM calls, auth, a database, a frontend.
