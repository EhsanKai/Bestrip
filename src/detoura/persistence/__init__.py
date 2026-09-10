"""Durable storage for Detoura's commercial + ops records (V8.5).

Everything else in Detoura is in-memory. This package is the one exception:
promo codes, markup policy versions, the booking economics ledger and the admin
audit trail, in a single SQLite file.
"""

from __future__ import annotations

from . import analytics, audit, bookings, economics, policies, promos
from .db import Database, configure_db, db_path_from_env, get_db, init_db

__all__ = [
    "Database",
    "analytics",
    "audit",
    "bookings",
    "bootstrap",
    "configure_db",
    "db_path_from_env",
    "economics",
    "get_db",
    "init_db",
    "policies",
    "promos",
]


def bootstrap(database: Database | None = None) -> Database:
    """Install the database and seed the example markup policy + promo.
    Idempotent. Called once at app startup, and by tests with an in-memory db.
    """
    db = configure_db(database)
    policies.seed_defaults(db)
    promos.seed_defaults(db)
    return db
