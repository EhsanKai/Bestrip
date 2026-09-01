# Intelligent Budget Travel Planner

A deterministic, multi-objective route optimizer that answers:

> *Given my budget, dates, party size and preferences, what are the best trips I can take?*

It does **not** just look for the cheapest return flight to one city. It searches
the space of complete round trips — including multi-city routes and alternative
departure airports — and ranks them on cost, time, destination fit, convenience
and variety.

> ⚠️ **All transport data in this MVP is synthetic.** Prices, schedules and
> availability are fabricated to exercise the optimizer. Nothing here reflects
> real-world flights, trains or buses.

---

## Table of contents

- [The problem](#the-problem)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [How the algorithm works](#how-the-algorithm-works)
  - [1. Origin discovery](#1-origin-discovery)
  - [2. Beam search](#2-beam-search)
  - [3. Constraints](#3-constraints)
  - [4. Scoring](#4-scoring)
  - [5. Pareto filtering](#5-pareto-filtering)
  - [6. Diversity filtering](#6-diversity-filtering)
  - [7. Baseline comparison](#7-baseline-comparison)
- [Observability](#observability)
- [Configuration](#configuration)
- [The API](#the-api)
- [Synthetic data](#synthetic-data)
- [Plugging in real APIs](#plugging-in-real-apis)
- [Where an LLM fits](#where-an-llm-fits)
- [Running the tests](#running-the-tests)
- [Known limitations](#known-limitations)

---

## The problem

A naive planner answers `Köln → Madrid → Köln, €200, 5 days, 1 city`.

The optimizer may find that `Köln → Prague → Vienna → Köln` costs less, fits the
same five days, and shows two cities instead of one. But multi-city is **not**
automatically better: if Madrid really is the best answer, Madrid is returned.

The central trap this project is built to avoid is committing to a cheap first
leg. In the synthetic network, `DUS → London` is the cheapest departure anywhere
(€35/pp) — and every way home from London is punitive. `DUS → Prague` costs more
up front (€55/pp) but `Prague → Vienna` (€12) and `Vienna → DUS` (€25) make the
complete trip far cheaper:

```
                ┌─→ London ──→ Brussels ──→ DUS      120  (cheapest first leg, worst trip)
                │
   Düsseldorf ──┤
                │
                └─→ Prague ──→ Vienna ────→ DUS       92  ← best complete itinerary
                     ↑
                     the *expensive* first leg
```

`tests/test_beam_search.py` implements a greedy cheapest-next-hop reference
algorithm, shows it picks the 120 route, and asserts the optimizer picks the 92
route. That test is the project's reason for existing.

---

## Quick start

```bash
pip install -e ".[dev]"          # or: pip install pydantic fastapi uvicorn pytest httpx

pytest                            # run the suite
python examples/koln_scenario.py  # run the Köln scenario and print the itineraries
python examples/koln_scenario.py --debug   # ... plus the full search trace

uvicorn travel_planner.api.app:app --reload   # http://127.0.0.1:8000/docs
```

Using it as a library — no FastAPI required:

```python
from datetime import date
from travel_planner import TravelPlanner, TripRequest, TravelPreferences

planner = TravelPlanner()          # defaults to the synthetic providers
result = planner.plan(TripRequest(
    origin="Köln",
    budget=250, travelers=2, duration_days=5,
    date_from=date(2026, 9, 10), date_to=date(2026, 9, 15), date_flexible=True,
    transport_preferences=["flight", "train"],
    preferred_destinations=["Madrid"],
    avoid_destinations=["Paris"],
    preferences=TravelPreferences(history=0.8, culture=0.8, multiple_cities=0.9),
))

for itinerary in result.recommendations:
    print(itinerary.rank, itinerary.route_label(), itinerary.total_cost)
```

You can also inject your own providers and configuration:

```python
planner = TravelPlanner(
    transport_provider=my_provider,
    destination_provider=my_destinations,
    config=PlannerConfig(beam_width=40, max_cities=3),
)
```

---

## Architecture

```
travel_planner/
├── config.py                  PlannerConfig, ScoreWeights — every tunable number
├── models/
│   ├── trip.py                TripRequest, TravelPreferences
│   ├── transport.py           TransportOption, TransportType
│   ├── destination.py         Destination
│   ├── search.py              SearchState (immutable)
│   ├── itinerary.py           Itinerary, BaselineResult, PlanResult, ScoreBreakdown
│   └── debug.py               RejectionReason, IterationDebug, SearchDebug
├── data/
│   ├── destinations.py        synthetic city catalog + origin-airport distances
│   └── synthetic_transport.py synthetic European network
├── providers/
│   ├── transport.py           TransportDataProvider protocol, Synthetic + Real
│   └── destinations.py        DestinationProvider protocol
├── algorithms/
│   ├── beam_search.py         the state-space search
│   ├── scoring.py             ScoringEngine, Objectives
│   ├── pareto.py              domination and frontier
│   └── diversity.py           Jaccard-based diversification
├── constraints/validator.py   ConstraintValidator, ConstraintResult
├── services/
│   ├── planner.py             TravelPlanner — the entry point
│   ├── baseline.py            naive single-destination round trip
│   ├── origin_resolver.py     "Köln" → CGN, DUS, FRA, EIN
│   └── return_estimator.py    admissible lower bounds for pruning
├── llm/interfaces.py          PreferenceParser / ItineraryExplainer seams
└── api/                       FastAPI adapter (optional)
```

The dependency direction is strict: `api → services → algorithms → constraints
→ providers → models`. The optimizer never imports `llm`, and never imports a
concrete provider — a test asserts both.

---

## How the algorithm works

The problem is treated as **state-space search + multi-objective optimization +
constraint satisfaction + route diversification** — not as flight search.

```
TripRequest
     │
     ▼
OriginResolver  ──►  CGN, DUS, FRA, EIN        (candidate departure nodes)
     │
     ▼
Beam search  ◄──►  ConstraintValidator         (reject infeasible states)
     │       ◄──►  ScoringEngine               (rank surviving states)
     │       ◄──►  TransportDataProvider       (what can I board?)
     ▼
Completed round trips
     │
     ▼
Pareto filter        (drop dominated itineraries)
     │
     ▼
Diversity filter     (drop near-duplicate trips)
     │
     ▼
Top N + baseline comparison
```

### 1. Origin discovery

The origin is a *region*, not an airport. `OriginResolver` turns `"Köln"` into
the departure nodes within `max_origin_distance_km` (default 250 km), nearest
first, capped at `max_origin_airports`:

| Airport | km from Köln | in range at 250 km |
| ------- | ------------ | ------------------ |
| CGN     | 15           | ✅                  |
| DUS     | 40           | ✅                  |
| FRA     | 155          | ✅                  |
| EIN     | 170          | ✅                  |
| AMS     | 260          | ❌ (raise the radius) |

An itinerary may return to a *different* origin airport than it left from —
`EIN → Vienna → DUS` is a legal answer.

### 2. Beam search

One iteration adds one leg. From any state there are two actions:

- **Continue** — travel to a destination city not yet visited.
- **Return** — travel to one of the origin airports, completing the trip.

Stay lengths are *searched*, not assumed: for every state the next departure
date is generated for each stay length in `min_city_stay_days …
max_city_stay_days`, so "London for 1 day", "London for 2 days" and "London for
3 days" are three distinct states that get scored separately.

```
initial states (airport × start date)
        ↓
generate next states (continue / return × stay length × timetable)
        ↓
validate constraints          ← rejected states are recorded with a reason
        ↓
estimate score                ← optimistic completion, see below
        ↓
rank, keep top `beam_width`   ← pruned states are recorded with their estimate
        ↓
repeat, up to max_cities + 1 legs
```

**Why it is not greedy.** Partial states are ranked by an *optimistic
completion*: the state's cost and travel time plus the cheapest possible way
home from where it currently is. So a state sitting in London with an €85 return
is judged on the €120 round trip it implies, not on the €35 leg that got it
there. Combined with a beam that carries `beam_width` alternatives at every
depth, several different first moves stay alive and compete on their *finished*
trips.

Three further things make the search practical:

- **Admissible pruning** (`CachedReturnEstimator`). A state is dropped when its
  cheapest possible return already exceeds the remaining budget, or its fastest
  possible return exceeds the remaining time. Because the bounds are minima over
  the whole window, this never discards a feasible itinerary.
- **Beam spread** (`beam_slots_per_route`). Without a cap, the beam fills with
  the same trip departing from four airports on two dates at three times of day.
  At most two slots go to any one city sequence.
- **Determinism.** Candidate generation iterates sorted collections; ranking
  breaks ties on cost and then on the state's leg-id signature. Two runs over
  the same data produce identical output, asserted by a test.

### 3. Constraints

All feasibility rules live in `ConstraintValidator`; beam search only asks and
obeys. Each failure returns a typed reason:

| Rule | Reason |
| --- | --- |
| Party total over budget | `BUDGET_EXCEEDED` |
| Elapsed time over the allowance | `DURATION_EXCEEDED` |
| Trip far shorter than requested | `DURATION_UNDERUSED` |
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
`must_visit` is a hard requirement, and it steers the *search* as well as the
final check — the beam ranks progress towards mandatory destinations ahead of
score, so routes that can actually satisfy them stay alive.

**Avoided destinations are rejected, never merely penalized.** Matching is
accent- and case-insensitive, so `"wien"` excludes Vienna.

**Prices are always party totals.** `TransportOption.price_per_person ×
travelers` is the only way a cost enters a state; the same trip costs twice as
much for two people, and the budget check sees the total.

### 4. Scoring

Six components, each in `[0, 1]`, combined with configurable weights that are
normalized so the total is also in `[0, 1]`.

| Component | Default weight | What it measures |
| --- | --- | --- |
| Budget | 0.40 | `1 - cost / budget`, clamped |
| Preference | 0.20 | destination fit blended with city count |
| Destination | 0.15 | stay-length fit + preferred/mandatory coverage |
| Convenience | 0.10 | leg count, mode comfort, hop length, pace |
| Time | 0.10 | time on the ground vs. in transit, days actually used |
| Diversity | 0.05 | countries seen and modes used *within* one trip |

**Preference** is the weighted overlap between the user's attribute weights
(`history`, `nature`, `nightlife`, `culture`, `food`) and each city's profile,
averaged over the visited cities, then blended with a city-count term:

```
attribute_match = Σ wₐ · cityₐ / Σ wₐ                    (per city, then averaged)
multi_component = 1 − 1/n                                (n = number of cities)
preference      = (attribute_match + m · multi_component) / (1 + m)
                                                          m = preferences.multiple_cities
```

`multiple_cities = 0` gives **zero** benefit from extra cities; `= 1` weights
city count equally with city quality. The `1 − 1/n` curve means one → two cities
is the meaningful jump and three → four barely moves.

**Travel time is penalized twice**, deliberately. `TimeScore` compares transit
time against the trip length (zero above `max_travel_time_fraction`, default
25%) and rewards using the days that were asked for. `ConvenienceScore` adds a
*pace* term: `London → Rome → Copenhagen → Madrid` in four days means packing up
almost every day, and scores badly no matter how cheap the tickets are — a test
asserts it loses to a two-city trip at the same price.

### 5. Pareto filtering

An itinerary is **dominated** when another is no worse on all four objectives —
cost (↓), travel time (↓), city count (↑), preference score (↑) — and strictly
better on at least one. Dominated itineraries are inferior under *any* weighting
and are dropped. Genuine trade-offs (cheap-but-slow vs. fast-but-pricey) both
survive, which is what keeps different travel styles in the results.

In a typical Köln run this cuts ~700 completed itineraries to ~25.

### 6. Diversity filtering

A greedy deterministic pass over the ranked frontier. An itinerary is accepted
only if the Jaccard similarity of its city set against every already-accepted
itinerary stays at or below `diversity_similarity_threshold` (default 0.5). This
is what prevents:

```
1. DUS → London → Brussels → DUS      ✗ five spellings of one trip
2. CGN → London → Brussels → CGN
3. DUS → London → Brussels → DUS
```

If the threshold is too strict to fill `max_results`, the remaining slots are
back-filled in rank order rather than returning a short list.

### 7. Baseline comparison

Before optimizing, the planner computes the cheapest simple round trip to the
user's *first* preferred destination — literally what a conventional search
would return. Every recommendation then carries a `BaselineComparison` with
`money_saved`, `additional_cities` and `additional_travel_minutes`.

If `preferred_destinations` is empty there is **no baseline**; the planner does
not invent a destination, and optimization proceeds normally.

---

## Observability

Everything the optimizer discards is recorded as a typed object, not a log
string. Run counters are always collected; `debug=True` returns the whole trace.

```python
result = planner.plan(request, debug=True)
print(result.debug.render())
```

```
Iteration 2
------------------
States in:    20
Generated:    1872
Rejected:     938
Remaining:    538
Beam width:   20
Beam pruning: 518
Kept:         20
Completed:    396
Rejections:
  DURATION_UNDERUSED: 396
  UNREACHABLE_RETURN_TIME: 188
  TRANSPORT_TYPE_NOT_ALLOWED: 168
  UNREACHABLE_RETURN_BUDGET: 114
  AVOIDED_DESTINATION: 48
  DURATION_EXCEEDED: 24
```

Structured access:

| What you want to know | Where |
| --- | --- |
| Why a state was rejected | `debug.iterations[i].rejected_examples[j].reason` / `.detail` |
| Why a state lost the beam | `debug.iterations[i].pruned_examples[j].estimated_score` |
| Why an itinerary scored what it did | `itinerary.score_breakdown` (all six components + weights) |
| Why an itinerary was Pareto-filtered | `debug.filtered` where `stage == PARETO`, with `dominated_by` |
| Why an itinerary was diversity-filtered | `debug.filtered` where `stage == DIVERSITY`, with `similarity` |

Over HTTP: `POST /plan-trip?debug=true`.

---

## Configuration

```python
PlannerConfig(
    beam_width=20,
    max_results=5,
    max_cities=4,
    beam_slots_per_route=2,

    min_city_stay_days=1,
    max_city_stay_days=4,
    min_duration_utilization=0.6,

    max_origin_distance_km=250.0,
    max_origin_airports=4,

    score_weights=ScoreWeights(
        budget=0.40, preference=0.20, destination=0.15,
        convenience=0.10, time=0.10, diversity=0.05,
    ),
    max_travel_time_fraction=0.25,
    preferred_destination_bonus=0.5,
    must_visit_bonus=0.3,

    enable_pareto=True,
    enable_diversity=True,
    diversity_similarity_threshold=0.5,
)
```

Two settings deserve a note:

- **`min_duration_utilization`** (0.6). `duration_days` is an upper bound in the
  spec, but a user asking for five days does not want a 40-hour round trip. A
  completed itinerary must use at least this fraction of the allowance. Set it
  to `0.0` to accept anything that fits.
- **`score_weights`**. Budget at 0.40 is the single strongest signal, so a very
  cheap one-city trip often outranks a pricier two-city one — by design (see
  [Known limitations](#known-limitations)). Reweighting changes the answer, and
  a test asserts it does.

---

## The API

The API is only an adapter. A test plans the same request directly and over
HTTP and asserts the two agree.

```
POST /plan-trip[?debug=true]   plan a trip
GET  /destinations             the synthetic catalog
GET  /config                   the active PlannerConfig
GET  /health
```

Request:

```json
{
  "origin": "Köln",
  "budget": 250,
  "travelers": 2,
  "duration_days": 5,
  "date_from": "2026-09-10",
  "date_to": "2026-09-15",
  "date_flexible": true,
  "transport_preferences": ["flight", "train"],
  "must_visit": [],
  "preferred_destinations": ["Madrid"],
  "avoid_destinations": ["Paris"],
  "preferences": {
    "history": 0.8, "nature": 0.7, "nightlife": 0.2,
    "culture": 0.8, "food": 0.6, "multiple_cities": 0.9
  }
}
```

Response (abridged — synthetic data):

```json
{
  "baseline": {
    "destination": "Madrid",
    "total_cost": 175.10,
    "currency": "EUR",
    "duration_days": 2.12,
    "legs": ["CGN → Madrid", "Madrid → CGN"]
  },
  "recommendations": [
    {
      "rank": 1,
      "score": 0.6116,
      "total_cost": 96.68,
      "currency": "EUR",
      "duration_days": 3.1,
      "origin_airport": "DUS",
      "return_airport": "DUS",
      "cities": ["Amsterdam"],
      "stay_days": [3],
      "total_travel_minutes": 290,
      "legs": [ "..." ],
      "score_breakdown": {
        "budget": 0.613, "preference": 0.364, "destination": 0.667,
        "convenience": 0.839, "time": 0.719, "diversity": 0.80,
        "total": 0.6116
      },
      "baseline_comparison": {
        "baseline_destination": "Madrid",
        "baseline_cost": 175.10,
        "money_saved": 78.42,
        "additional_cities": 0,
        "additional_travel_minutes": -60
      }
    }
  ],
  "metadata": {
    "origin_airports": ["CGN", "DUS", "EIN", "FRA"],
    "start_dates": ["2026-09-10", "2026-09-11"],
    "beam_width": 20,
    "states_generated": 3981,
    "states_rejected": 2392,
    "completed_itineraries": 717,
    "pareto_kept": 25,
    "elapsed_seconds": 0.216
  }
}
```

Invalid input (unknown origin, contradictory destinations, negative budget)
returns `422`.

---

## Synthetic data

`data/synthetic_transport.py` defines ~200 directed connections over 5 departure
airports (CGN, DUS, FRA, EIN, AMS) and 16 destination cities, covering flights,
trains and buses. Each connection has 2–3 daily departures across
2026-09-01 → 2026-09-30.

Prices vary by date and departure slot through fixed multiplier tables indexed
by `date.toordinal() % 7` — no randomness anywhere, so runs are reproducible.
Pass `price_variation=False` for a flat timetable (used by the trap fixture).

Outbound and return prices are declared **separately per link**. That asymmetry
is what makes the cheap-first-leg trap possible, and `tests/test_providers.py`
asserts the property directly:

```python
DUS → London  = 35/pp        London → DUS = 85/pp     round trip 120
DUS → Prague  = 55/pp        Prague → Vienna = 12/pp
                             Vienna → DUS = 25/pp     round trip  92
```

Adding a city means one `Destination` entry plus a few `_link(...)` rows.

---

## Plugging in real APIs

The optimizer talks to one protocol:

```python
class TransportDataProvider(Protocol):
    def search(self, origin: str, destination: str,
               departure_date: date) -> list[TransportOption]: ...
```

To go live: implement it against Amadeus / Kiwi / Deutsche Bahn / FlixBus,
normalize each offer into a `TransportOption`, and pass the instance to
`TravelPlanner(transport_provider=...)`. Nothing in `algorithms/`,
`constraints/` or `services/` changes. `RealTransportDataProvider` marks the
seam and deliberately raises `NotImplementedError` — the MVP must not present
invented numbers as real availability.

Practical notes for that step: cache aggressively (the search issues thousands
of `search()` calls, which the synthetic provider memoizes), keep the method
non-raising on "no route", and keep it deterministic within a run or the
determinism guarantee weakens to "stable for a fixed snapshot".

`OriginResolver` and `DestinationProvider` are the same story — swap the static
distance table for geocoding, swap the catalog for a real POI database.

---

## Where an LLM fits

```
free text ──► PreferenceParser ──► TripRequest ──► [ deterministic optimizer ] ──► Itinerary ──► ItineraryExplainer ──► prose
              (LLM may live here)                   (never an LLM)                                (LLM may live here)
```

`llm/interfaces.py` defines both protocols and ships dependency-free local
implementations (`KeywordPreferenceParser`, `TemplateItineraryExplainer`) so the
pipeline runs end to end with no external API. The explainer only restates
numbers taken from the itinerary, which is the failure mode a generative
explainer has to be guarded against.

Prices, route search, availability, constraint checks and scores are never an
LLM's job. A test parses the optimizer packages' ASTs and fails if any of them
import `travel_planner.llm`.

---

## Running the tests

```bash
pytest                       # everything
pytest tests/test_beam_search.py -v      # the non-greedy proof
pytest -k "not api"          # skip the FastAPI tests
```

| File | Covers |
| --- | --- |
| `test_models.py` | model validation, per-person vs. total price, state transitions |
| `test_constraints.py` | every rejection reason, pruning bounds |
| `test_scoring.py` | each component formula, weight reconfiguration |
| `test_beam_search.py` | **the non-greedy proof**, beam width, determinism, stay lengths |
| `test_pareto.py` | domination algebra and the frontier |
| `test_diversity.py` | Jaccard, deduplication, back-fill |
| `test_baseline.py` | baseline round trip and comparisons |
| `test_providers.py` | dataset coverage, the trap property, origin resolution |
| `test_api.py` | HTTP shape, error codes, adapter equivalence |
| `test_llm_interfaces.py` | the seams, and that the optimizer ignores them |
| `test_end_to_end.py` | the full Köln scenario, requirement by requirement |

---

## Known limitations

**Data**

- Everything is synthetic. No external call is made, and no output should be
  read as a real offer.
- Origin airports and destination cities are separate graph nodes, so `AMS` (the
  airport) and `Amsterdam` (the city) have independent connections. It keeps
  "flying out of Schiphol" from counting as "visiting Amsterdam", at the cost of
  a little duplication in the dataset.
- No hotels, no accommodation cost, no visas, no weather, no seat availability.
  A stay is free, which biases the optimizer towards longer stays.

**Model**

- `BudgetScore = 1 − cost/budget` rewards *not spending*, as the spec requires.
  Combined with the 0.40 weight, a €97 one-city trip often outranks a €151
  two-city one even at `multiple_cities = 0.9`. That is the specified trade-off,
  not a bug — but if you want the planner to spend the budget it is given, lower
  `score_weights.budget` or raise `preference`. Pareto and diversity filtering
  keep multi-city options in the returned set either way.
- Stay length is modelled in whole calendar days; a stay of *n* days means
  departing *n* calendar days after arriving. Sub-day connections are not
  modelled.
- Trip duration is measured from midnight of the start date, which is slightly
  conservative for a late first departure.
- `min_duration_utilization` is an addition to the spec, not part of it. It is
  configurable and can be switched off.

**Search**

- Beam search is a heuristic. A wide beam explores more but nothing guarantees
  the global optimum; `beam_width` is the knob.
- `max_cities = 4` combined with a 5-day window makes four-city itineraries
  mostly infeasible — expect one- and two-city results for short trips.
- Only one preferred destination (the first) drives the baseline.
- The date window is treated as a hard boundary for every leg, including
  arrivals, so a trip cannot land the morning after `date_to`.

**Not built** (deliberately, per the MVP scope): hotels, restaurants, visas,
weather, maps, real-time APIs, LLM calls, auth, a database, a frontend.
