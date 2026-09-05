"""Honest before/after benchmark for QUICK/SMART/DEEP (V6).

Builds one realistic, generous-budget, flexible-dates request - large enough
to push DEEP's adaptive ladder through several rounds, unlike the tight
``request_for_the_ladder()`` fixture in ``tests/test_v4_adaptive_beam.py``
(budget=450), which exists to prove the ladder *triggers*, not to represent a
real search's cost.

Run it directly::

    python3 scripts/bench_search_modes.py [--runs N]

It prints, per mode: median and best wall-clock latency over ``--runs``
repetitions (default 3), the provider call counts for the last run (upstream
misses - the calls that would actually have reached a real API), the
completed/pareto/recommendation counts, and the top recommendation's route,
cost and score. Run it before and after a change and diff the two outputs -
that diff is the only honest evidence a change helped.

Context this script was built to answer: does wrapping the beam search's
independent, synchronous provider lookups (transport/accommodation search
calls within one DEEP round) in a ``concurrent.futures.ThreadPoolExecutor``
measurably speed up DEEP mode? A correct, determinism-preserving,
no-duplicate-calls implementation was built and benchmarked with this script.
Result: no. Two independent clean before/after sessions on this request both
showed DEEP ~2% *slower* with the thread pool, never faster, and QUICK/SMART
(never touching that code path) unchanged within noise. ``cProfile`` on the
same request confirms why: the synthetic transport/accommodation providers'
own ``.search()`` calls cost well under 1% of a DEEP run's wall-clock time
(~0.075s of ~29s profiled) - the search is CPU-bound (state expansion,
constraint validation, Travel Value scoring, O(n^2) Pareto filtering), not
I/O-bound, so there is no blocking I/O for a thread pool to overlap under
CPython's GIL. That lever was therefore not shipped; this script - and the
measurement method it encodes - is the artifact kept from that investigation.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from detoura.config import PlannerConfig  # noqa: E402
from detoura.models.transport import TransportType  # noqa: E402
from detoura.models.trip import TravelPreferences, TripRequest  # noqa: E402
from detoura.search_modes import SearchMode, apply_mode  # noqa: E402
from detoura.services.planner import TravelPlanner  # noqa: E402


def realistic_request() -> TripRequest:
    """A request with a generous budget and flexible dates.

    Tuned (empirically, against the synthetic network) to push DEEP through
    all four of its adaptive rounds in the neighbourhood of the "8-20s"
    range ``search_modes.py`` documents as DEEP's honest estimate, rather
    than the pathological >100s a wide-open budget and month-long window
    produce, or the sub-second run a unit-test fixture produces.
    """
    return TripRequest(
        origin="Köln",
        budget=500.0,
        travelers=2,
        duration_days=3,
        date_from=date(2026, 9, 10),
        date_to=date(2026, 9, 14),
        date_flexible=True,
        transport_preferences=[TransportType.FLIGHT, TransportType.TRAIN],
        preferences=TravelPreferences(
            history=0.6,
            nature=0.6,
            nightlife=0.4,
            culture=0.7,
            food=0.6,
            multiple_cities=0.8,
        ),
    )


def run_once(mode: SearchMode, request: TripRequest):
    config = apply_mode(PlannerConfig(), mode)
    planner = TravelPlanner(config=config)
    started = time.perf_counter()
    result = planner.plan(request)
    elapsed = time.perf_counter() - started
    return elapsed, result


def bench(mode: SearchMode, request: TripRequest, runs: int):
    timings: list[float] = []
    result = None
    for _ in range(runs):
        elapsed, result = run_once(mode, request)
        timings.append(elapsed)
    return timings, result


def report(mode: SearchMode, timings: list[float], result) -> None:
    metadata = result.metadata
    print(f"\n=== {mode.value} ===")
    print(f"  runs:              {[round(t, 4) for t in timings]}")
    print(f"  median:            {statistics.median(timings):.4f}s")
    print(f"  best:              {min(timings):.4f}s")
    print(f"  beam_rounds:       {len(metadata.beam_rounds)}")
    print(f"  states_generated:  {metadata.states_generated}")
    print(f"  completed:         {metadata.completed_itineraries}")
    print(f"  pareto_kept:       {metadata.pareto_kept}")
    print(f"  recommendations:   {len(result.recommendations)}")
    print(f"  provider_metrics:  {metadata.provider_metrics}")
    if result.recommendations:
        top = result.recommendations[0]
        print(
            f"  top recommendation: {top.route_label()} "
            f"cost={top.total_cost} score={top.score}"
        )
        print(
            "  all recommendation routes: "
            + ", ".join(r.route_label() for r in result.recommendations)
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=3)
    args = parser.parse_args()

    request = realistic_request()
    for mode in (SearchMode.QUICK, SearchMode.SMART, SearchMode.DEEP):
        timings, result = bench(mode, request, args.runs)
        report(mode, timings, result)


if __name__ == "__main__":
    main()
