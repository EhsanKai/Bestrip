"""The operational booking record (V8.5 Phase B).

One row per Detoura journey plus one per underlying ticket, kept up to date as
the booking runs. This is what the ops console inspects. It is mutable - states
change, a recovery note gets attached - unlike the immutable commercial ledger
in :mod:`detoura.persistence.economics`.

PII is deliberately minimal: the lead traveller's name and email, for customer
identification, and nothing else. No date of birth, phone, nationality or
document number is written here.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from ..models.money import from_minor_units, to_minor_units
from .db import Database

#: Recovery states surfaced in the Recovery Center. "" means healthy.
RECOVERY_STATES = (
    "PRICE_CHANGED",
    "PARTIAL_FAILURE",
    "RECOVERY_REQUIRED",
    "UNAVAILABLE",
    "CANCELLATION_FAILED",
    "CHANGE_REQUIRES_ACTION",
    "FAILED",
)


class BookingItemRecord(BaseModel):
    sequence: int
    origin_city: str = ""
    origin_airport: str = ""
    destination_city: str = ""
    destination_airport: str = ""
    departure: datetime | None = None
    arrival: datetime | None = None
    carrier: str = ""
    flight_number: str = ""
    offer_id: str = ""
    provider: str = ""
    quoted_price: float = 0.0
    current_price: float | None = None
    booked_price: float | None = None
    currency: str = "EUR"
    cabin_baggage: str = "unknown"
    checked_baggage: str = "unknown"
    required: bool = True
    state: str
    detail: str = ""
    provider_order_id: str | None = None


class BookingRecord(BaseModel):
    booking_id: str
    session_ref: str = ""
    journey_reference: str
    created_at: datetime
    updated_at: datetime
    mode: str
    phase: str
    trip_label: str = ""
    route_cities: list[str] = Field(default_factory=list)
    party_size: int = 1
    lead_name: str = ""
    lead_email: str = ""
    currency: str = "EUR"
    service_tier: str = "BASIC"
    discovered_total: float = 0.0
    current_total: float | None = None
    customer_total: float | None = None
    recovery_state: str = ""
    reconfirm_note: str = ""
    items: list[BookingItemRecord] = Field(default_factory=list)


def _mn(v: float | None) -> int | None:
    return None if v is None else to_minor_units(v)


def upsert(db: Database, rec: BookingRecord) -> None:
    now = rec.updated_at.isoformat()
    with db.write() as conn:
        conn.execute(
            "INSERT INTO bookings ("
            " booking_id, session_ref, journey_reference, created_at, updated_at,"
            " mode, phase, trip_label, route_json, party_size, lead_name,"
            " lead_email, currency, service_tier, discovered_total_minor,"
            " current_total_minor, customer_total_minor, recovery_state,"
            " reconfirm_note"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT (booking_id) DO UPDATE SET "
            " session_ref=excluded.session_ref, updated_at=excluded.updated_at,"
            " mode=excluded.mode, phase=excluded.phase,"
            " trip_label=excluded.trip_label, route_json=excluded.route_json,"
            " party_size=excluded.party_size, lead_name=excluded.lead_name,"
            " lead_email=excluded.lead_email, service_tier=excluded.service_tier,"
            " discovered_total_minor=excluded.discovered_total_minor,"
            " current_total_minor=excluded.current_total_minor,"
            " customer_total_minor=excluded.customer_total_minor,"
            " recovery_state=excluded.recovery_state,"
            " reconfirm_note=excluded.reconfirm_note",
            (
                rec.booking_id, rec.session_ref, rec.journey_reference,
                rec.created_at.isoformat(), now, rec.mode, rec.phase,
                rec.trip_label, json.dumps(rec.route_cities), rec.party_size,
                rec.lead_name, rec.lead_email, rec.currency, rec.service_tier,
                to_minor_units(rec.discovered_total), _mn(rec.current_total),
                _mn(rec.customer_total), rec.recovery_state, rec.reconfirm_note,
            ),
        )
        conn.execute("DELETE FROM booking_items WHERE booking_id = ?",
                     (rec.booking_id,))
        for it in rec.items:
            conn.execute(
                "INSERT INTO booking_items ("
                " booking_id, sequence, origin_city, origin_airport,"
                " destination_city, destination_airport, departure, arrival,"
                " carrier, flight_number, offer_id, provider,"
                " quoted_price_minor, current_price_minor, booked_price_minor,"
                " currency, cabin_baggage, checked_baggage, required, state,"
                " detail, provider_order_id, updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rec.booking_id, it.sequence, it.origin_city, it.origin_airport,
                    it.destination_city, it.destination_airport,
                    it.departure.isoformat() if it.departure else None,
                    it.arrival.isoformat() if it.arrival else None,
                    it.carrier, it.flight_number, it.offer_id, it.provider,
                    to_minor_units(it.quoted_price), _mn(it.current_price),
                    _mn(it.booked_price), it.currency, it.cabin_baggage,
                    it.checked_baggage, 1 if it.required else 0, it.state,
                    it.detail, it.provider_order_id, now,
                ),
            )


def get(db: Database, booking_id: str) -> BookingRecord | None:
    row = db.query_one("SELECT * FROM bookings WHERE booking_id = ?", (booking_id,))
    if row is None:
        return None
    items = db.query(
        "SELECT * FROM booking_items WHERE booking_id = ? ORDER BY sequence",
        (booking_id,),
    )
    return _record(row, items)


def list_bookings(
    db: Database,
    *,
    limit: int = 100,
    phase: str | None = None,
    recovery_only: bool = False,
    search: str | None = None,
) -> list[BookingRecord]:
    limit = max(1, min(int(limit), 500))
    where: list[str] = []
    params: list[Any] = []
    if phase:
        where.append("phase = ?")
        params.append(phase)
    if recovery_only:
        where.append("recovery_state != ''")
    if search:
        like = f"%{search.strip()}%"
        where.append(
            "(booking_id LIKE ? OR journey_reference LIKE ? OR lead_name LIKE ?"
            " OR lead_email LIKE ?)"
        )
        params.extend([like, like, like, like])
    sql = "SELECT * FROM bookings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    rows = db.query(sql, tuple(params))
    out: list[BookingRecord] = []
    for row in rows:
        items = db.query(
            "SELECT * FROM booking_items WHERE booking_id = ? ORDER BY sequence",
            (row["booking_id"],),
        )
        out.append(_record(row, items))
    return out


def recovery_queue(db: Database, *, limit: int = 200) -> list[BookingRecord]:
    return list_bookings(db, limit=limit, recovery_only=True)


def counts_by_phase(db: Database) -> dict[str, int]:
    rows = db.query("SELECT phase, COUNT(*) AS n FROM bookings GROUP BY phase")
    return {r["phase"]: r["n"] for r in rows}


# --- row -> model ---------------------------------------------------------
def _dt(v) -> datetime | None:
    return datetime.fromisoformat(v) if v else None


def _fm(v) -> float | None:
    return None if v is None else from_minor_units(v)


def _record(row, item_rows) -> BookingRecord:
    return BookingRecord(
        booking_id=row["booking_id"],
        session_ref=row["session_ref"],
        journey_reference=row["journey_reference"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        mode=row["mode"],
        phase=row["phase"],
        trip_label=row["trip_label"],
        route_cities=json.loads(row["route_json"] or "[]"),
        party_size=row["party_size"],
        lead_name=row["lead_name"],
        lead_email=row["lead_email"],
        currency=row["currency"],
        service_tier=row["service_tier"],
        discovered_total=from_minor_units(row["discovered_total_minor"]),
        current_total=_fm(row["current_total_minor"]),
        customer_total=_fm(row["customer_total_minor"]),
        recovery_state=row["recovery_state"],
        reconfirm_note=row["reconfirm_note"],
        items=[
            BookingItemRecord(
                sequence=r["sequence"],
                origin_city=r["origin_city"], origin_airport=r["origin_airport"],
                destination_city=r["destination_city"],
                destination_airport=r["destination_airport"],
                departure=_dt(r["departure"]), arrival=_dt(r["arrival"]),
                carrier=r["carrier"], flight_number=r["flight_number"],
                offer_id=r["offer_id"], provider=r["provider"],
                quoted_price=from_minor_units(r["quoted_price_minor"]),
                current_price=_fm(r["current_price_minor"]),
                booked_price=_fm(r["booked_price_minor"]),
                currency=r["currency"],
                cabin_baggage=r["cabin_baggage"],
                checked_baggage=r["checked_baggage"],
                required=bool(r["required"]),
                state=r["state"], detail=r["detail"],
                provider_order_id=r["provider_order_id"],
            )
            for r in item_rows
        ],
    )
