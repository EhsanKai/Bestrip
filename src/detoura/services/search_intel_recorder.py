"""The Search Intelligence recorder (V9 Phase 1).

Wraps one real-supply search so that it teaches Detoura something:

* scores every candidate market against Price Memory, classifies it EXPLOIT or
  EXPLORE, and records why it was selected or skipped;
* mints a stable ``acquisition_call_id`` per provider call and tags every
  :class:`TransportOption` it produced, so an optimizer result can be
  attributed back to the exact call;
* records one :class:`PriceObservation` per answered edge (the cheapest
  retained offer, with offer-count context) — historical market data, never a
  quote;
* after the optimizer runs, walks the recommendations to mark which calls
  reached the candidate set, the Top-K and the winner;
* persists observations + a :class:`SearchIntelligenceTrace`, and prunes Price
  Memory past its retention window.

Nothing here is on the hot path of a booking, and none of it changes a price.
If `SearchIntelConfig.enabled` is false the recorder is inert.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Iterable, Sequence

from ..models.destination import Destination
from ..models.money import BASE_CURRENCY, to_minor_units
from ..models.search_intel import (
    AcquisitionStance,
    CandidateProvenance,
    EdgeKind,
    PriceObservation,
    SearchModeTag,
    TripShape,
    travelers_bucket,
)
from ..models.search_trace import (
    CandidateDecision,
    ProviderCallOutcome,
    SearchEconomicsSnapshot,
    SearchIntelligenceTrace,
)
from ..models.transport import TransportOption
from ..models.trip import TripRequest
from ..persistence import price_memory as pm
from ..persistence.db import Database
from ..search_intel_config import SearchIntelConfig, search_intel_config
from .acquisition import AcquisitionEdge, preference_affinity
from .acquisition_scoring import CandidateInput, allocate, score_candidates
from .market_intel import batch_market_signals, primary_signal
from . import provider_economics


def new_search_id() -> str:
    return "srch_" + secrets.token_urlsafe(12)


@dataclass(slots=True)
class _CallRecord:
    call_id: str
    edge: AcquisitionEdge
    ordinal: int
    stance: AcquisitionStance
    from_cache: bool = False
    offers_received: int = 0
    offers_retained: int = 0
    error: str = ""
    elapsed_ms: float = 0.0
    cheapest: TransportOption | None = None
    candidate_reason: str = ""
    candidate_rank: int | None = None
    edge_kind: str = ""
    candidate_id: str = ""
    secondary_candidate_id: str = ""
    scoring_reference_date: date | None = None
    #: The IATA codes the edge actually resolved to for the provider call, so an
    #: observation is keyed by airport, not by a catalog city id.
    origin_iata: str = ""
    destination_iata: str = ""


class SearchIntelRecorder:
    """One per real-supply search. Not thread-safe; a search is single-threaded
    through acquisition."""

    def __init__(
        self,
        db: Database,
        request: TripRequest,
        *,
        mode: str = "SMART",
        provider: str = "duffel",
        provider_call_budget: int = 0,
        cfg: SearchIntelConfig | None = None,
        now: Callable[[], datetime] | None = None,
        search_id: str | None = None,
    ) -> None:
        self.db = db
        self.request = request
        self.cfg = cfg or search_intel_config()
        self.provider = provider
        self.budget = provider_call_budget
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.search_id = search_id or new_search_id()
        self.started_at = self._now()
        self.mode = _mode_tag(mode)
        self.travelers = max(request.travelers, 1)

        self._calls: list[_CallRecord] = []
        self._by_call: dict[str, _CallRecord] = {}
        self._candidates: list[CandidateDecision] = []
        self._scoring_reference_date: date | None = None
        """The date candidate decisions were scored against. When acquisition
        spans more than one date variant, the decision is shared across them and
        this records which date carried the intelligence (V9 QA fix §3)."""
        self._ordinal = 0

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled

    # ------------------------------------------------------------------
    # 1. candidate scoring / allocation
    # ------------------------------------------------------------------
    def plan_candidates(
        self,
        candidates: Sequence[Destination],
        *,
        city_airports: dict[str, str],
        origin_airports: Sequence[str],
        departure_date: date,
        slots: int,
    ) -> tuple[list[Destination], dict[str, CandidateProvenance]]:
        """Score the ranked candidates against Price Memory, choose ``slots`` of
        them (guaranteeing an EXPLORE share), and record the decisions.

        Returns ``(chosen destinations, {dest id -> CandidateProvenance})``. The
        provenance dict is handed to ``build_plan`` so every edge — OUTBOUND,
        RETURN and INTER_CITY — carries the right stance/reason/rank; stance is
        never inferred later from ``edge.destination``.

        Cold start (empty Price Memory) → every candidate is EXPLORE and
        selection falls back to the incoming rank order, so search correctness
        never depends on history.
        """
        self._scoring_reference_date = departure_date
        if not self.enabled or not candidates:
            chosen = list(candidates[:slots])
            return chosen, {}

        affinity = preference_affinity(candidates, self.request)
        from ..models.search_intel import MarketKey

        # A candidate's market is (each resolved origin airport) -> its airport,
        # on the departure date. Query all (origin, dest) pairs in one batch and
        # merge per destination — never merge currencies.
        origins = [o.upper() for o in origin_airports] or [self.request.origin.upper()]
        markets: list[MarketKey] = []
        market_owner: dict[tuple, str] = {}
        for d in candidates:
            airport = (city_airports.get(d.id, d.id) or d.id).upper()
            for o in origins:
                mk = MarketKey.build(
                    provider=self.provider, origin=o, destination=airport,
                    departure_date=departure_date, travelers=self.travelers,
                )
                markets.append(mk)
                market_owner[mk.as_tuple()] = d.id

        signals = batch_market_signals(self.db, markets, cfg=self.cfg, now=self._now())
        # merge every (origin, dest) signal for a destination into one, keeping
        # currencies apart, then pick the primary.
        per_dest: dict[str, dict[str, list]] = {}
        for key_tuple, ccy_sigs in signals.items():
            dest_id = market_owner.get(key_tuple)
            if dest_id is None:
                continue
            for ccy, sig in ccy_sigs.items():
                per_dest.setdefault(dest_id, {}).setdefault(ccy, []).append(sig)

        inputs: list[CandidateInput] = []
        for rank, d in enumerate(candidates):
            airport = (city_airports.get(d.id, d.id) or d.id).upper()
            sig = _merge_signals(per_dest.get(d.id, {}))
            inputs.append(CandidateInput(
                market=f"→{d.id}", pre_rank=rank, signal=sig,
                preference_affinity=affinity.get(d.id), feasible=bool(airport),
            ))

        scored = score_candidates(inputs, cfg=self.cfg)
        chosen = allocate(scored, slots=slots, cfg=self.cfg)
        chosen_markets = {c.market for c in chosen}

        by_market_score = {s.market: s for s in scored}
        chosen_dest: list[Destination] = []
        provenance: dict[str, CandidateProvenance] = {}
        for rank, d in enumerate(candidates):
            s = by_market_score[f"→{d.id}"]
            selected = s.market in chosen_markets
            self._candidates.append(CandidateDecision(
                market=f"{self.request.origin}→{d.id}",
                pre_acquisition_rank=rank,
                stance=s.stance,
                selected=selected,
                selection_reason=s.reason,
                intelligence_confidence=s.confidence,
                historical_signal_used=s.historical_signal_used,
                baseline_score=s.score,
                score_components={**s.components, "scoring_reference_date": departure_date.isoformat()},
            ))
            if selected:
                chosen_dest.append(d)
                provenance[d.id] = CandidateProvenance(
                    stance=s.stance, reason=s.reason, rank=rank,
                    scoring_reference_date=departure_date,
                )
        # honour the allocation's ordering (exploit first, then explore)
        order = {c.market: i for i, c in enumerate(chosen)}
        chosen_dest.sort(key=lambda d: order.get(f"→{d.id}", 1_000))
        ordered_prov = {d.id: provenance[d.id] for d in chosen_dest}
        return chosen_dest, ordered_prov

    # ------------------------------------------------------------------
    # 2. per-call recording
    # ------------------------------------------------------------------
    def wrap_fetch(
        self, inner: Callable[[AcquisitionEdge], Iterable[TransportOption]],
        *, cache_probe: Callable[[AcquisitionEdge], bool] | None = None,
        resolve: Callable[[str], str | None] | None = None,
    ) -> Callable[[AcquisitionEdge], list[TransportOption]]:
        """Return a fetch that records a :class:`ProviderCallOutcome` and a
        :class:`PriceObservation` per edge, and tags each option with the
        call id. ``resolve`` maps an edge node (city id or code) to the IATA
        code the provider call used, so observations are keyed by airport."""
        if not self.enabled:
            return lambda edge: list(inner(edge))

        def fetch(edge: AcquisitionEdge) -> list[TransportOption]:
            self._ordinal += 1
            ordinal = self._ordinal
            call_id = f"{self.search_id}:{ordinal:04d}"
            # Provenance travels ON the edge (attached by build_plan). It is
            # never inferred from edge.origin/destination here.
            p = edge.provenance
            if p is not None:
                stance, reason = p.stance, p.reason
                rank, kind = p.candidate_rank, p.kind.value
                cand_id, sec_id = p.candidate_id, p.secondary_candidate_id
                sref = p.scoring_reference_date
            else:
                # No provenance => this edge was not planned by the recorder
                # (recorder disabled, or a caller bypassed plan_candidates).
                stance, reason = AcquisitionStance.EXPLORE, "unplanned edge"
                rank, kind, cand_id, sec_id, sref = None, "", "", "", None
            rec = _CallRecord(
                call_id=call_id, edge=edge, ordinal=ordinal, stance=stance,
                candidate_reason=reason, candidate_rank=rank, edge_kind=kind,
                candidate_id=cand_id, secondary_candidate_id=sec_id,
                scoring_reference_date=sref,
                from_cache=bool(cache_probe(edge)) if cache_probe else False,
                origin_iata=(resolve(edge.origin) if resolve else edge.origin) or edge.origin,
                destination_iata=(resolve(edge.destination) if resolve else edge.destination) or edge.destination,
            )
            started = time.perf_counter()
            try:
                options = list(inner(edge))
            except Exception as error:
                rec.error = type(error).__name__
                rec.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
                self._calls.append(rec)
                self._by_call[call_id] = rec
                raise
            rec.elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
            rec.offers_received = len(options)
            tagged = [o.model_copy(update={"acquisition_call_id": call_id}) for o in options]
            rec.offers_retained = len(tagged)
            if tagged:
                rec.cheapest = min(tagged, key=lambda o: o.price_per_person)
            self._calls.append(rec)
            self._by_call[call_id] = rec
            return tagged

        return fetch

    # ------------------------------------------------------------------
    # 3. attribution — after the optimizer has run
    # ------------------------------------------------------------------
    def attribute(self, recommendations: Sequence, *, top_k: int | None = None) -> None:
        """Mark which calls reached the candidate set / Top-K / winner.

        **Canonical rule — the planner rank contract.** Every recommendation
        carries a total order via ``Itinerary.rank`` (the planner assigns
        ``1..N``, one best trip). This method uses ``rank`` only — never list
        position:

        * *candidate*  — the call id appears on any recommendation's legs.
        * *Top-K*      — appears on one of the ``top_k`` lowest-``rank`` recs.
        * *winner*     — appears on the single lowest-``rank`` rec.

        If a recommendation object has no ``rank`` (a test double), it is
        treated as rank ``+inf`` and can never be the winner or Top-K — it must
        opt in by carrying a rank.
        """
        if not self.enabled:
            return
        top_k = top_k or self.cfg.top_k
        recs = list(recommendations)

        def _rank(r) -> float:
            v = getattr(r, "rank", None)
            return float(v) if isinstance(v, (int, float)) else float("inf")

        def _calls(r) -> set[str]:
            return {
                getattr(leg, "acquisition_call_id", "")
                for leg in getattr(r, "legs", [])
                if getattr(leg, "acquisition_call_id", "")
            }

        candidate_calls: set[str] = set()
        for r in recs:
            candidate_calls |= _calls(r)

        by_rank = sorted(recs, key=_rank)
        top_k_calls: set[str] = set()
        for r in by_rank[:top_k]:
            if _rank(r) != float("inf"):
                top_k_calls |= _calls(r)
        winner_calls: set[str] = set()
        if by_rank and _rank(by_rank[0]) != float("inf"):
            winner_calls = _calls(by_rank[0])

        self._candidate_calls = candidate_calls
        self._top_k_calls = top_k_calls
        self._winner_calls = winner_calls

    # ------------------------------------------------------------------
    # 4. finalize — build the trace, persist, prune
    # ------------------------------------------------------------------
    def finalize(
        self,
        *,
        cache_hits: int = 0,
        cache_misses: int = 0,
        provider_calls_used: int = 0,
        recommendations_produced: int = 0,
    ) -> SearchIntelligenceTrace:
        candidate_calls = getattr(self, "_candidate_calls", set())
        top_k_calls = getattr(self, "_top_k_calls", set())
        winner_calls = getattr(self, "_winner_calls", set())

        observations: list[PriceObservation] = []
        outcomes: list[ProviderCallOutcome] = []
        calls_failed = 0
        for rec in self._calls:
            in_cand = rec.call_id in candidate_calls
            in_top = rec.call_id in top_k_calls
            in_win = rec.call_id in winner_calls
            if rec.error:
                calls_failed += 1
            outcomes.append(ProviderCallOutcome(
                acquisition_call_id=rec.call_id,
                edge=f"{rec.edge.origin}→{rec.edge.destination} on {rec.edge.day.isoformat()}",
                ordinal=rec.ordinal, stance=rec.stance, from_cache=rec.from_cache,
                offers_received=rec.offers_received,
                offers_normalized=rec.offers_retained,
                offers_retained=rec.offers_retained,
                error=rec.error, elapsed_ms=rec.elapsed_ms,
                contributed_to_optimizer=in_cand,
                contributed_to_top_k=in_top,
                contributed_to_winner=in_win,
            ))
            if rec.cheapest is not None and self.enabled:
                observations.append(self._observation(rec, in_cand, in_top, in_win))

        if self.enabled and observations:
            pm.record_observations(self.db, observations)
        if self.enabled:
            pm.update_contributions(
                self.db,
                entered=candidate_calls, top_k=top_k_calls, winner=winner_calls,
            )

        explore = sum(1 for r in self._calls if r.stance is AcquisitionStance.EXPLORE)
        exploit = sum(1 for r in self._calls if r.stance is AcquisitionStance.EXPLOIT)

        econ_excess, econ_cost, econ_conf = provider_economics.estimate_for_search(
            provider_calls_this_search=provider_calls_used or len(self._calls),
            cfg=self.cfg.economics,
        )
        econ = SearchEconomicsSnapshot(
            provider_searches_performed=provider_calls_used or len(self._calls),
            included_search_allowance=(
                float(self.cfg.economics.included_searches_flat)
                if self.cfg.economics.included_searches_flat is not None else None
            ),
            estimated_excess_searches=econ_excess,
            estimated_excess_search_cost_minor=econ_cost,
            currency=self.cfg.economics.currency,
            economics_configured=econ_conf,
        )

        trace = SearchIntelligenceTrace(
            search_id=self.search_id,
            started_at=self.started_at,
            finished_at=self._now(),
            provider=self.provider,
            origin=self.request.origin.upper(),
            date_from=self.request.date_from,
            date_to=self.request.date_to,
            flexible=self.request.date_to > self.request.date_from,
            duration_days=self.request.duration_days,
            travelers=self.travelers,
            search_mode=self.mode,
            provider_call_budget=self.budget,
            provider_calls_used=provider_calls_used or len(self._calls),
            provider_calls_failed=calls_failed,
            cache_hits=cache_hits,
            cache_misses=cache_misses,
            calls_explore=explore,
            calls_exploit=exploit,
            candidates=tuple(self._candidates),
            call_outcomes=tuple(outcomes),
            recommendations_produced=recommendations_produced,
            winner_recommendation_id="",
            economics=econ,
        )
        if self.enabled:
            pm.record_trace(self.db, trace)
            try:
                pm.prune(self.db, retention_days=self.cfg.retention_days)
                pm.prune_traces(self.db, retention_days=self.cfg.retention_days)
                pm.prune_stale_provenance(self.db)
            except Exception:
                pass
        return trace

    # ------------------------------------------------------------------
    def _observation(
        self, rec: _CallRecord, in_cand: bool, in_top: bool, in_win: bool,
    ) -> PriceObservation:
        opt = rec.cheapest
        assert opt is not None
        # Currency invariant: the acquisition pipeline
        # (real_supply.acquire_real_supply -> DuffelTransportProvider) normalizes
        # every retained option's price to money.BASE_CURRENCY and drops any
        # offer it cannot convert (ProviderFailureKind.CURRENCY_UNAVAILABLE), so
        # a TransportOption that reaches the recorder is, by contract, in
        # BASE_CURRENCY. This encodes that invariant rather than stamping a
        # literal.
        currency = BASE_CURRENCY
        party = max(rec.edge.travelers, 1)
        per_person_minor = to_minor_units(round(opt.price_per_person, 2))
        total_minor = to_minor_units(round(opt.price_per_person * party, 2))
        mk_ref = opt.provider_ref
        stops = None
        if mk_ref is not None and mk_ref.raw_segments:
            stops = max(0, len(mk_ref.raw_segments) - 1)
        return PriceObservation(
            observation_id=pm.new_observation_id(),
            observed_at=self.started_at,
            provider=self.provider,
            origin=(rec.origin_iata or rec.edge.origin).upper(),
            destination=(rec.destination_iata or rec.edge.destination).upper(),
            departure_date=rec.edge.day,
            # An acquisition edge is a one-way route query; the aggregation and
            # scoring lookups use the same shape, so the market keys line up.
            trip_shape=TripShape.ONE_WAY,
            travelers=party,
            travelers_bucket=travelers_bucket(party),
            total_amount_minor=total_minor,
            per_person_minor=per_person_minor,
            currency=currency,
            direct=(stops == 0) if stops is not None else None,
            stops=stops,
            marketing_carrier=getattr(mk_ref, "marketing_carrier", None),
            operating_carrier=getattr(mk_ref, "operating_carrier", None),
            offer_count_for_edge=rec.offers_received,
            search_id=self.search_id,
            acquisition_call_id=rec.call_id,
            search_mode=self.mode,
            candidate_reason=rec.candidate_reason[:200],
            exploration=rec.stance is AcquisitionStance.EXPLORE,
            candidate_rank=rec.candidate_rank,
            edge_kind=rec.edge_kind,
            secondary_market=(rec.secondary_candidate_id or None),
            scoring_reference_date=rec.scoring_reference_date,
            provider_call_ordinal=rec.ordinal,
            provider_call_budget=self.budget or None,
            normalized_ok=not rec.error,
            retained_after_limits=rec.offers_retained > 0,
            entered_candidate_set=in_cand,
            contributed_to_top_k=in_top,
            contributed_to_winner=in_win,
        )


def _merge_signals(by_ccy: dict[str, list]):
    """Combine one destination's per-(origin,currency) signals into a single
    representative signal for scoring. Picks the currency with the most total
    samples, then the signal with the most samples within it — never blends
    currencies or invents an average across origins."""
    if not by_ccy:
        return None
    best_ccy = max(by_ccy, key=lambda c: sum(s.sample_count for s in by_ccy[c]))
    sigs = by_ccy[best_ccy]
    return max(sigs, key=lambda s: (s.sample_count, s.confidence.score))


def _mode_tag(mode: str) -> SearchModeTag:
    try:
        return SearchModeTag(str(mode).upper())
    except ValueError:
        return SearchModeTag.UNKNOWN
