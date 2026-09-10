"""Markup policy storage + versioning (V8.5).

A policy is stored by ``(policy_id, version)``; exactly one version per
``policy_id`` is ``active`` at a time. Saving a new version does not rewrite the
old one - a booking priced under v1 keeps pointing at v1 forever.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..models.commercial import ServiceTier
from ..models.markup import DynamicMarkupPolicy, MarkupBounds, MarkupRule
from . import audit
from .db import Database

DEFAULT_POLICY_ID = "detoura.markup"
BUILTIN_VERSION = 2
_SEED_LABEL_PREFIX = "Detoura sandbox markup"

#: Seed policy - **an example for the sandbox, not commercial truth**. Every
#: number here is meant to be re-tuned through ops configuration.
#:
#: The product rule the numbers encode: All-in-One is the higher-service
#: product, so its Detoura fee is higher - a bigger percentage plus an explicit
#: orchestration fee for the multi-ticket booking, monitoring and recovery
#: Detoura runs. Basic is self-service and cheaper. The commercial service also
#: enforces All-in-One >= Basic regardless of what a policy says.
_DEFAULT_POLICY = DynamicMarkupPolicy(
    policy_id=DEFAULT_POLICY_ID,
    version=BUILTIN_VERSION,
    label="Detoura sandbox markup v2 (example - not commercial truth)",
    rules=(
        MarkupRule(
            label="All-in-One",
            when_tier=ServiceTier.ALL_IN_ONE,
            percentage=0.05,
            fixed_fee=6.0,
        ),
        MarkupRule(
            label="Basic / self-service",
            when_tier=ServiceTier.BASIC,
            percentage=0.03,
            fixed_fee=0.0,
        ),
    ),
    bounds=MarkupBounds(
        max_percentage=0.15,
        max_fixed_fee=25.0,
        min_total_fee=0.0,
        max_total_fee=120.0,
    ),
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_defaults(db: Database) -> None:
    """Idempotent. Installs the built-in example policy if none exists, and
    upgrades an *un-edited* built-in seed to the current version - but never
    touches a policy an operator has authored or changed."""
    row = db.query_one("SELECT COUNT(*) AS n FROM markup_policies")
    if not row or not row["n"]:
        save_policy(db, _DEFAULT_POLICY, active=True, actor="system:seed")
        return
    active = _active_row(db, DEFAULT_POLICY_ID)
    if (
        active is not None
        and str(active["label"]).startswith(_SEED_LABEL_PREFIX)
        and int(active["version"]) < BUILTIN_VERSION
        and get_policy(db, DEFAULT_POLICY_ID, BUILTIN_VERSION) is None
    ):
        save_policy(db, _DEFAULT_POLICY, active=True, actor="system:seed-upgrade")


def save_policy(
    db: Database, policy: DynamicMarkupPolicy, *, active: bool, actor: str
) -> None:
    before = _active_row(db, policy.policy_id)
    with db.write() as conn:
        if active:
            conn.execute(
                "UPDATE markup_policies SET active = 0 WHERE policy_id = ?",
                (policy.policy_id,),
            )
        conn.execute(
            "INSERT INTO markup_policies "
            "(policy_id, version, label, active, definition_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (policy_id, version) DO UPDATE SET "
            "  label = excluded.label, active = excluded.active, "
            "  definition_json = excluded.definition_json",
            (policy.policy_id, policy.version, policy.label,
             1 if active else 0, policy.model_dump_json(), _now()),
        )
    audit.record(
        db, actor=actor, action="MARKUP_POLICY_SAVED",
        target_type="markup_policy",
        target_id=f"{policy.policy_id}@v{policy.version}",
        before={"active_version": before["version"]} if before else None,
        after={"version": policy.version, "active": active,
               "rules": len(policy.rules)},
    )


def _active_row(db: Database, policy_id: str):
    return db.query_one(
        "SELECT * FROM markup_policies WHERE policy_id = ? AND active = 1",
        (policy_id,),
    )


def active_policy(db: Database, policy_id: str = DEFAULT_POLICY_ID) -> DynamicMarkupPolicy:
    row = _active_row(db, policy_id)
    if row is None:
        seed_defaults(db)
        row = _active_row(db, policy_id)
    if row is None:  # pragma: no cover - only if policy_id is unknown
        return _DEFAULT_POLICY
    return DynamicMarkupPolicy.model_validate_json(row["definition_json"])


def get_policy(
    db: Database, policy_id: str, version: int
) -> DynamicMarkupPolicy | None:
    row = db.query_one(
        "SELECT definition_json FROM markup_policies "
        "WHERE policy_id = ? AND version = ?",
        (policy_id, version),
    )
    if row is None:
        return None
    return DynamicMarkupPolicy.model_validate_json(row["definition_json"])


def list_policies(db: Database) -> list[dict]:
    rows = db.query(
        "SELECT policy_id, version, label, active, created_at "
        "FROM markup_policies ORDER BY policy_id, version DESC"
    )
    return [dict(r) for r in rows]


def next_version(db: Database, policy_id: str) -> int:
    row = db.query_one(
        "SELECT MAX(version) AS v FROM markup_policies WHERE policy_id = ?",
        (policy_id,),
    )
    return int(row["v"] + 1) if row and row["v"] is not None else 1


def set_active(
    db: Database, policy_id: str, version: int, *, actor: str
) -> DynamicMarkupPolicy:
    """Make one stored version the active one. A historical booking still
    points at whatever version it was priced with - this only changes what new
    bookings use."""
    policy = get_policy(db, policy_id, version)
    if policy is None:
        raise KeyError((policy_id, version))
    before = _active_row(db, policy_id)
    with db.write() as conn:
        conn.execute(
            "UPDATE markup_policies SET active = 0 WHERE policy_id = ?",
            (policy_id,),
        )
        conn.execute(
            "UPDATE markup_policies SET active = 1 "
            "WHERE policy_id = ? AND version = ?",
            (policy_id, version),
        )
    audit.record(
        db, actor=actor, action="MARKUP_POLICY_ACTIVATED",
        target_type="markup_policy", target_id=f"{policy_id}@v{version}",
        before={"active_version": before["version"] if before else None},
        after={"active_version": version},
    )
    return policy


def build_policy(
    *,
    policy_id: str,
    version: int,
    label: str,
    basic_percentage: float,
    basic_fixed_fee: float,
    all_in_one_percentage: float,
    all_in_one_fixed_fee: float,
    max_percentage: float,
    max_fixed_fee: float,
    min_total_fee: float,
    max_total_fee: float,
) -> DynamicMarkupPolicy:
    """Turn the small set of numbers an operator configures into a full
    two-rule policy. The server builds this; a client never supplies a
    computed fee."""
    return DynamicMarkupPolicy(
        policy_id=policy_id,
        version=version,
        label=label or f"ops policy v{version}",
        rules=(
            MarkupRule(
                label="All-in-One", when_tier=ServiceTier.ALL_IN_ONE,
                percentage=all_in_one_percentage, fixed_fee=all_in_one_fixed_fee,
            ),
            MarkupRule(
                label="Basic / self-service", when_tier=ServiceTier.BASIC,
                percentage=basic_percentage, fixed_fee=basic_fixed_fee,
            ),
        ),
        bounds=MarkupBounds(
            max_percentage=max_percentage, max_fixed_fee=max_fixed_fee,
            min_total_fee=min_total_fee, max_total_fee=max_total_fee,
        ),
    )


def policy_config(policy: DynamicMarkupPolicy) -> dict:
    """The operator-facing view of a stored policy: just the tunable numbers."""
    by_tier = {r.when_tier: r for r in policy.rules if r.when_tier is not None}
    basic = by_tier.get(ServiceTier.BASIC)
    aio = by_tier.get(ServiceTier.ALL_IN_ONE)
    return {
        "basic_percentage": basic.percentage if basic else 0.0,
        "basic_fixed_fee": basic.fixed_fee if basic else 0.0,
        "all_in_one_percentage": aio.percentage if aio else 0.0,
        "all_in_one_fixed_fee": aio.fixed_fee if aio else 0.0,
        "max_percentage": policy.bounds.max_percentage,
        "max_fixed_fee": policy.bounds.max_fixed_fee,
        "min_total_fee": policy.bounds.min_total_fee,
        "max_total_fee": policy.bounds.max_total_fee,
    }
