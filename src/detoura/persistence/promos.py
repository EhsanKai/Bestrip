"""Promo code storage + redemption tracking (V8.5).

Codes live in ``promo_codes`` (one row, the whole definition as JSON).
Redemptions live in ``promo_redemptions``, one row per accepted use, unique on
``(code, booking_id)`` so a poll or a retry cannot double-count.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..models.money import from_minor_units, to_minor_units
from ..models.promo import PromoCode, PromoRedemption
from . import audit
from .db import Database

#: A single enabled example so price transparency + promo can be exercised
#: end-to-end before the ops console exists. Operators can disable it.
_SEED_PROMO = PromoCode(
    code="WELCOME5",
    label="Example: 5% off the Detoura service fee (sandbox seed)",
    kind="PERCENTAGE",
    value=5.0,
    currency="EUR",
    per_user_limit=1,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_defaults(db: Database) -> None:
    row = db.query_one("SELECT COUNT(*) AS n FROM promo_codes")
    if row and row["n"]:
        return
    save_promo(db, _SEED_PROMO, actor="system:seed")


def save_promo(db: Database, promo: PromoCode, *, actor: str) -> None:
    existing = get_promo(db, promo.code)
    payload = promo.model_copy(
        update={"created_at": promo.created_at or datetime.now(timezone.utc)}
    )
    with db.write() as conn:
        conn.execute(
            "INSERT INTO promo_codes (code, definition_json, enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (code) DO UPDATE SET "
            "  definition_json = excluded.definition_json, "
            "  enabled = excluded.enabled, updated_at = excluded.updated_at",
            (payload.code, payload.model_dump_json(),
             1 if payload.enabled else 0, _now(), _now()),
        )
    audit.record(
        db, actor=actor,
        action="PROMO_UPDATED" if existing else "PROMO_CREATED",
        target_type="promo", target_id=promo.code,
        before=_safe(existing), after=_safe(payload),
    )


def set_enabled(db: Database, code: str, enabled: bool, *, actor: str) -> PromoCode:
    promo = get_promo(db, code)
    if promo is None:
        raise KeyError(code)
    updated = promo.model_copy(update={"enabled": enabled})
    with db.write() as conn:
        conn.execute(
            "UPDATE promo_codes SET definition_json = ?, enabled = ?, updated_at = ? "
            "WHERE code = ?",
            (updated.model_dump_json(), 1 if enabled else 0, _now(), code),
        )
    audit.record(
        db, actor=actor,
        action="PROMO_ENABLED" if enabled else "PROMO_DISABLED",
        target_type="promo", target_id=code,
        before={"enabled": promo.enabled}, after={"enabled": enabled},
    )
    return updated


def get_promo(db: Database, code: str) -> PromoCode | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    row = db.query_one(
        "SELECT definition_json FROM promo_codes WHERE code = ?", (code,)
    )
    if row is None:
        return None
    return PromoCode.model_validate_json(row["definition_json"])


def list_promos(db: Database) -> list[PromoCode]:
    rows = db.query("SELECT definition_json FROM promo_codes ORDER BY code")
    return [PromoCode.model_validate_json(r["definition_json"]) for r in rows]


def redemption_counts(db: Database, code: str, user_key: str) -> tuple[int, int]:
    """``(global_redemptions, this_user_redemptions)``."""
    code = (code or "").strip().upper()
    g = db.query_one(
        "SELECT COUNT(*) AS n FROM promo_redemptions WHERE code = ?", (code,)
    )
    u = db.query_one(
        "SELECT COUNT(*) AS n FROM promo_redemptions WHERE code = ? AND user_key = ?",
        (code, user_key or "anonymous"),
    )
    return (int(g["n"]) if g else 0, int(u["n"]) if u else 0)


def record_redemption(db: Database, redemption: PromoRedemption) -> bool:
    """Write one redemption. Returns False if this booking already redeemed
    this code (the unique constraint holds), so callers never double-count."""
    with db.write() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO promo_redemptions "
            "(code, booking_id, user_key, discount_minor, currency, redeemed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (redemption.code.strip().upper(), redemption.booking_id,
             redemption.user_key, to_minor_units(redemption.discount_amount),
             redemption.currency, redemption.redeemed_at.isoformat()),
        )
        return cur.rowcount > 0


def redemptions_for(db: Database, code: str) -> list[PromoRedemption]:
    rows = db.query(
        "SELECT * FROM promo_redemptions WHERE code = ? ORDER BY id DESC",
        ((code or "").strip().upper(),),
    )
    return [
        PromoRedemption(
            code=r["code"], booking_id=r["booking_id"], user_key=r["user_key"],
            discount_amount=from_minor_units(r["discount_minor"]),
            currency=r["currency"],
            redeemed_at=datetime.fromisoformat(r["redeemed_at"]),
        )
        for r in rows
    ]


def _safe(promo: PromoCode | None) -> dict | None:
    if promo is None:
        return None
    return {
        "code": promo.code, "kind": promo.kind.value, "value": promo.value,
        "currency": promo.currency, "enabled": promo.enabled,
        "target": promo.target.value, "global_limit": promo.global_limit,
        "per_user_limit": promo.per_user_limit,
    }
