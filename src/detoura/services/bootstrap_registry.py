"""Wires a concrete :class:`~detoura.services.bootstrap_executor.SourceFetcher`
to a registered ``source_id`` (V9 Phase 2.5 §29).

Deliberately a fixed, code-reviewed mapping rather than something a request
can specify — an Ops caller can start/resume a *job*, never hand the executor
an arbitrary URL or fetcher. Only this module (edited by a developer, shipped
in a release) decides what actually happens when a job's task runs.

Phase 2.5 ships exactly one fetcher wired up end to end: a deterministic
fixture source. No ``AUTHORIZED_WEB_SOURCE`` is registered by default because
none has been reviewed to ``APPROVED`` (§8) — see
``docs/V9_MARKET_PRIOR_SOURCE_OPTIONS.md``. A future real source is added
here, not by improvising a fetch inside the API layer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from ..models.market_prior_acquisition import AuthorizationStatus, SourceRegistration, SourceType
from ..persistence import market_prior_acquisition as store
from ..persistence.db import Database
from .bootstrap_executor import SourceFetcher
from .bootstrap_fetchers import FixtureSourceFetcher

#: ``job_id`` is threaded through so a factory building a network-capable
#: fetcher can construct its ``AuthorizedHttpFetcher`` with ``db``/``job_id``
#: set — the only way its budget/rate-limit gating becomes the persisted,
#: cross-process-safe kind rather than in-memory-only (see network_adapter.py).
FetcherFactory = Callable[[SourceRegistration, Database, str], SourceFetcher]

DEFAULT_FIXTURE_SOURCE_ID = "fixture-europe-demo"

_FACTORIES: dict[str, FetcherFactory] = {}


def register_fetcher_factory(source_id: str, factory: FetcherFactory) -> None:
    _FACTORIES[source_id] = factory


def get_fetcher(reg: SourceRegistration, *, db: Database, job_id: str) -> SourceFetcher | None:
    """``None`` means "no fetcher is wired up for this source" — the Ops
    layer must treat that as fail-closed (refuse to start/resume the job),
    never fall back to guessing a fetch."""
    factory = _FACTORIES.get(reg.source_id)
    return factory(reg, db, job_id) if factory else None


def has_fetcher(source_id: str) -> bool:
    """Whether a factory is registered at all, without building an instance
    or requiring a job to charge — used by read-only Ops listings."""
    return source_id in _FACTORIES


def reset_registry() -> None:
    _FACTORIES.clear()


register_fetcher_factory(DEFAULT_FIXTURE_SOURCE_ID, lambda reg, db, job_id: FixtureSourceFetcher())


def ensure_default_sources(db: Database) -> None:
    """Idempotently seeds the one demo source Phase 2.5 ships with, if it is
    not already registered. Safe to call on every request — a no-op after
    the first time."""
    if store.get_source(db, DEFAULT_FIXTURE_SOURCE_ID) is not None:
        return
    now = datetime.now(timezone.utc)
    store.upsert_source(db, SourceRegistration(
        source_id=DEFAULT_FIXTURE_SOURCE_ID,
        source_name="Fixture Europe Demo (deterministic, offline)",
        source_type=SourceType.FILE_IMPORT,
        authorization_status=AuthorizationStatus.APPROVED,
        authorization_basis="Detoura-owned deterministic fixture generator — no external "
                             "network access, no third-party data, ships with the release.",
        commercial_reuse_status="N/A",
        adapter_version="fixture-v1",
        reviewed_by="system",
        reviewed_at=now,
        notes="Default demo/test source for the controlled E2E acquisition demo (§37). "
              "Not a real market-data source.",
        created_at=now, updated_at=now,
    ))
