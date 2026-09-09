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

#: Seed policy - **an example for the sandbox, not commercial truth**. Every
#: number here is meant to be re-tuned through ops configuration. Basic is
#: self-service and costs more in percentage terms; All-in-One is a lower
#: percentage plus an explicit service fee for the orchestration Detoura does.
_DEFAULT_POLICY = DynamicMarkupPolicy(
    policy_id=DEFAULT_POLICY_ID,
    version=1,
    label="Detoura sandbox markup v1 (example - not commercial truth)",
    rules=(
        MarkupRule(
            label="All-in-One",
            when_tier=ServiceTier.ALL_IN_ONE,
            percentage=0.03,
            fixed_fee=8.0,
        ),
        MarkupRule(
            label="Basic / self-service",
            when_tier=ServiceTier.BASIC,
            percentage=0.05,
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
    """Idempotent. Installs the example policy only if the table is empty."""
    row = db.query_one("SELECT COUNT(*) AS n FROM markup_policies")
    if row and row["n"]:
        return
    save_policy(db, _DEFAULT_POLICY, active=True, actor="system:seed")


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
