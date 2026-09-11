"""Authorized Market-Prior Acquisition domain (V9 Phase 2.5).

Extends the Phase 2 seam — it does not replace it:

    Authorized External Source
          -> MarketPriorSource / Acquisition Adapter
          -> Bootstrap Acquisition Job (this module)
          -> bounded tasks (this module)
          -> fetch / parse / normalize / validate
          -> the existing Market Prior import boundary (market_prior_import.py)
          -> BootstrapMarketPrior (models/market_prior.py, unchanged)
          -> existing opportunity scoring / candidate funnel (unchanged)

The absolute boundary from Phase 2 still holds and nothing here weakens it:
BOOTSTRAP PRIOR != LIVE PRICE != BOOKABLE OFFER.

**Fail closed.** :class:`SourceRegistration.network_allowed` is the single
gate every network-capable adapter must consult before making a request.
Default authorization is ``REVIEW_REQUIRED`` — never ``APPROVED``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class SourceType(str, Enum):
    """What kind of thing a Market-Prior source is. A future official API or
    licensed feed fits the same pipeline as the existing JSON/CSV import — the
    import boundary is not coupled to any one of these (V9 Phase 2.5 §7)."""

    API_SOURCE = "API_SOURCE"
    AUTHORIZED_WEB_SOURCE = "AUTHORIZED_WEB_SOURCE"
    FILE_IMPORT = "FILE_IMPORT"
    MANUAL_DATASET = "MANUAL_DATASET"


class AuthorizationStatus(str, Enum):
    """An explicit, human-reviewed authorization decision. The software
    records the decision; it does not conclude the decision is legally
    correct (V9 Phase 2.5 §4)."""

    APPROVED = "APPROVED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    PROHIBITED = "PROHIBITED"
    DISABLED = "DISABLED"


class SourceHealth(str, Enum):
    """HTTP 200 alone does not imply HEALTHY — parser validity matters too
    (V9 Phase 2.5 §31)."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    RATE_LIMITED = "RATE_LIMITED"
    BLOCKED = "BLOCKED"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    DISABLED = "DISABLED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    NO_DATA = "NO_DATA"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    RATE_LIMITED = "RATE_LIMITED"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


#: A task in one of these statuses will never be picked up again by the
#: planner or the stale-lease recovery sweep.
TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.SUCCEEDED, TaskStatus.NO_DATA, TaskStatus.BLOCKED,
    TaskStatus.FAILED, TaskStatus.CANCELLED,
})

#: A task in one of these statuses is a candidate for a future retry sweep
#: (not implemented as an automatic background scheduler in Phase 2.5 - see
#: docs/V9_PHASE2_5_MARKET_PRIOR_ACQUISITION.md "refresh planner").
RETRYABLE_TASK_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.RETRYABLE_FAILURE, TaskStatus.RATE_LIMITED,
})


class JobStatus(str, Enum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"


class StopReason(str, Enum):
    """Why a job/task run halted rather than continuing to fight a source
    that does not want to be automated (V9 Phase 2.5 §20)."""

    CAPTCHA_DETECTED = "CAPTCHA_DETECTED"
    ACCESS_DENIED = "ACCESS_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    AUTHORIZATION_DISABLED = "AUTHORIZATION_DISABLED"
    SOURCE_CHANGED = "SOURCE_CHANGED"
    DOMAIN_NOT_ALLOWED = "DOMAIN_NOT_ALLOWED"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"


class RateLimitPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    requests_per_minute: int = Field(default=20, ge=1, le=6000)
    max_concurrency: int = Field(default=1, ge=1, le=16)
    min_delay_seconds: float = Field(default=0.0, ge=0.0)

    @property
    def effective_min_interval_seconds(self) -> float:
        """The pacing actually enforced: whichever of the two configured
        floors is stricter."""
        from_rate = 60.0 / max(self.requests_per_minute, 1)
        return max(from_rate, self.min_delay_seconds)


class SourceRegistration(BaseModel):
    """The explicit authorization record for one Market-Prior source.

    ``network_allowed`` is the single fail-closed gate: only a source whose
    ``authorization_status`` is ``APPROVED`` (and, for a network-capable
    type, has a ``base_domain`` and has not failed its robots/machine-policy
    check) may ever be handed to :class:`~detoura.services.network_adapter.AuthorizedHttpFetcher`.
    """

    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1, max_length=64)
    source_name: str = Field(min_length=1, max_length=200)
    source_type: SourceType
    authorization_status: AuthorizationStatus = AuthorizationStatus.REVIEW_REQUIRED
    authorization_basis: str = Field(default="", max_length=2000)
    allowed_scope: str = Field(default="", max_length=2000)
    commercial_reuse_status: str = Field(default="UNKNOWN", max_length=64)
    persistence_allowed: bool = True
    base_domain: str | None = None
    """Required for ``API_SOURCE`` / ``AUTHORIZED_WEB_SOURCE`` — the only
    domain this source's adapter may ever contact. ``None`` for
    ``FILE_IMPORT`` / ``MANUAL_DATASET``, which never make a network call."""
    rate_limit_policy: RateLimitPolicy = Field(default_factory=RateLimitPolicy)
    adapter_version: str = Field(default="v1", max_length=32)
    reviewed_at: datetime | None = None
    reviewed_by: str = Field(default="", max_length=200)
    notes: str = Field(default="", max_length=4000)
    robots_checked_at: datetime | None = None
    robots_allowed: bool | None = None
    """An *operational* signal only, never a legal conclusion (§9). ``None``
    = not checked / not applicable (e.g. a non-web source)."""
    request_cost_minor: int | None = None
    """Per-request cost in minor units, if known. ``None`` = UNKNOWN — never
    reported as free (§34)."""
    created_at: datetime
    updated_at: datetime

    @property
    def is_network_capable(self) -> bool:
        return self.source_type in (SourceType.AUTHORIZED_WEB_SOURCE, SourceType.API_SOURCE)

    @property
    def network_allowed(self) -> bool:
        """Fail-closed network gate. Every reason this can be ``False`` is a
        deliberate check, not an omission — see the property body."""
        if self.authorization_status is not AuthorizationStatus.APPROVED:
            return False
        if self.is_network_capable:
            if not self.base_domain:
                return False
            if self.robots_allowed is False:
                return False
        return True

    @property
    def health_if_disabled_only(self) -> SourceHealth | None:
        """Health implied purely by authorization state, before any live
        signal is considered (used as the floor by the health model)."""
        if self.authorization_status is AuthorizationStatus.DISABLED:
            return SourceHealth.DISABLED
        if self.authorization_status is AuthorizationStatus.PROHIBITED:
            return SourceHealth.BLOCKED
        if self.authorization_status is AuthorizationStatus.REVIEW_REQUIRED:
            return SourceHealth.REVIEW_REQUIRED
        return None


class TaskCell(BaseModel):
    """One planned (origin, destination, horizon) unit of acquisition work —
    the sparse planner's output, before any task row exists (§12, §13)."""

    model_config = ConfigDict(frozen=True)

    origin: str = Field(min_length=3, max_length=3)
    destination: str = Field(min_length=3, max_length=3)
    horizon_days: int = Field(ge=0)

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.origin, self.destination, self.horizon_days)
