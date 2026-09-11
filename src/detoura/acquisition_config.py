"""Configuration for Authorized Market-Prior Acquisition (V9 Phase 2.5).

Separate from :mod:`detoura.search_intel_config` on purpose: this governs
*how Detoura fills the Bootstrap Market Prior*, not how a live search scores
candidates. The two economics are tracked separately (§28) and this module's
knobs never widen a live search's own provider-call budget.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .search_intel_config import DEFAULT_BOOTSTRAP_ORIGINS


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


#: Named origin tiers for origin-first rollout (§14). Business-driven, not
#: hardcoded into any algorithm — the planner just takes whatever list of
#: origins it is given; these are the *documented default* groupings an
#: operator can start from.
ORIGIN_TIERS: dict[str, tuple[str, ...]] = {
    "TIER_1": DEFAULT_BOOTSTRAP_ORIGINS[:5],
    "TIER_2": DEFAULT_BOOTSTRAP_ORIGINS[:10],
    "TIER_3": DEFAULT_BOOTSTRAP_ORIGINS,
}


@dataclass(frozen=True, slots=True)
class AcquisitionConfig:
    #: The hard external-request ceiling for one job-slice invocation.
    #: Conservative by design - a single Ops action should not be able to
    #: fire an unbounded number of requests at any source (§16).
    max_requests_per_run: int = 50
    #: How many already-fresh cells the dedup planner treats as "still
    #: covered" without re-acquiring (§12, §33).
    dedup_freshness_days: int = 90
    #: Default lease duration for a claimed task before it is considered
    #: abandoned and eligible for stale-lease recovery (§19).
    task_lease_seconds: int = 120
    #: Response-size ceiling for an AUTHORIZED_WEB_SOURCE / API_SOURCE fetch.
    max_response_bytes: int = 2_000_000

    @classmethod
    def from_env(cls) -> "AcquisitionConfig":
        return cls(
            max_requests_per_run=max(1, _int("MARKET_PRIOR_MAX_REQUESTS_PER_RUN", 50)),
            dedup_freshness_days=max(0, _int("MARKET_PRIOR_DEDUP_FRESHNESS_DAYS", 90)),
            task_lease_seconds=max(10, _int("MARKET_PRIOR_TASK_LEASE_SECONDS", 120)),
            max_response_bytes=max(1024, _int("MARKET_PRIOR_MAX_RESPONSE_BYTES", 2_000_000)),
        )


_CONFIG: AcquisitionConfig | None = None


def acquisition_config() -> AcquisitionConfig:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = AcquisitionConfig.from_env()
    return _CONFIG


def reset_acquisition_config() -> None:
    global _CONFIG
    _CONFIG = None
