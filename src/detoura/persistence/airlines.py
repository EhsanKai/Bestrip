"""Airline performance analytics (V8.5 C3).

Aggregated from the persisted operational record - ``bookings`` +
``booking_items`` + ``booking_economics``. Read-only: nothing here writes.

Two carrier dimensions are kept apart. The **marketing** dimension keys on the
ticketed carrier (``booking_items.carrier``); the **operating** dimension keys
on ``operating_carrier`` where the provider gave one, falling back to the
marketing carrier when it did not. They are never silently merged - a report
asks for one or the other.

Cost-derived figures (Detoura margin) are only populated for bookings whose
economics row has no UNKNOWN cost. Everything unknown stays UNKNOWN / excluded,
never zero.
"""

from __future__ import annotations

from datetime import datetime

from ..models.airline import airline_for
from ..models.money import from_minor_units
from .db import Database

#: Item states that mean a ticket was actually issued.
_ISSUED = ("CONFIRMED",)
#: Item states that mean issuance was attempted and failed.
_FAILED = ("FAILED", "PROVIDER_FAILURE", "TIMEOUT", "UNAVAILABLE")


def _window(since: datetime | None, until: datetime | None) -> tuple[str, list]:
    where, params = [], []
    if since is not None:
        where.append("b.created_at >= ?")
        params.append(since.isoformat())
    if until is not None:
        where.append("b.created_at <= ?")
        params.append(until.isoformat())
    return (" WHERE " + " AND ".join(where)) if where else "", params


def airline_report(
    db: Database,
    *,
    dimension: str = "marketing",
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict:
    """Per-airline aggregates over the operational record.

    ``dimension`` is ``"marketing"`` (ticketed carrier) or ``"operating"``
    (carrier that flies it, marketing as fallback).
    """
    dim = "operating" if dimension == "operating" else "marketing"
    clause, params = _window(since, until)

    rows = db.query(
        "SELECT b.booking_id, b.currency, i.sequence, i.carrier, i.carrier_name,"
        "       i.operating_carrier, i.state, i.provider,"
        "       i.quoted_price_minor, i.current_price_minor, i.booked_price_minor "
        "FROM booking_items i JOIN bookings b ON b.booking_id = i.booking_id"
        f"{clause}",
        tuple(params),
    )
    econ = {
        r["booking_id"]: r
        for r in db.query("SELECT * FROM booking_economics")
    }

    agg: dict[str, dict] = {}
    booking_seen: dict[str, set] = {}

    for r in rows:
        code = (
            (r["operating_carrier"] or r["carrier"])
            if dim == "operating"
            else r["carrier"]
        ) or "??"
        code = code.upper()
        a = agg.setdefault(code, {
            "iata_code": code,
            "tickets_total": 0,
            "tickets_issued": 0,
            "tickets_failed": 0,
            "bookings": set(),
            "supplier_spend": 0.0,
            "fares": [],
            "currency": r["currency"] or "EUR",
            "carrier_name": r["carrier_name"] or "",
        })
        a["tickets_total"] += 1
        if r["state"] in _ISSUED:
            a["tickets_issued"] += 1
            paid = r["booked_price_minor"] or r["current_price_minor"] or r["quoted_price_minor"] or 0
            fare = from_minor_units(paid)
            a["supplier_spend"] += fare
            a["fares"].append(fare)
        elif r["state"] in _FAILED:
            a["tickets_failed"] += 1
        a["bookings"].add(r["booking_id"])
        if not a["carrier_name"] and r["carrier_name"]:
            a["carrier_name"] = r["carrier_name"]

    # Detoura revenue / margin, apportioned per booking to the carriers that
    # actually issued a ticket in it. Only cost-complete economics rows
    # contribute a margin.
    for code, a in agg.items():
        revenue_gross = 0.0
        margin_known = 0.0
        margin_bookings = 0
        for bid in a["bookings"]:
            e = econ.get(bid)
            if e is None:
                continue
            n_issued_here = sum(
                1 for r in rows
                if r["booking_id"] == bid and r["state"] in _ISSUED and (
                    ((r["operating_carrier"] or r["carrier"]) if dim == "operating"
                     else r["carrier"]) or "??"
                ).upper() == code
            )
            n_issued_total = sum(
                1 for r in rows
                if r["booking_id"] == bid and r["state"] in _ISSUED
            ) or 1
            share = n_issued_here / n_issued_total
            gross = from_minor_units(
                (e["service_fee_minor"] or 0) + (e["markup_minor"] or 0)
                - (e["discount_minor"] or 0)
            )
            revenue_gross += round(gross * share, 2)
            unknown = any(
                e[c] is None
                for c in ("provider_cost_estimate_minor", "payment_cost_minor",
                          "recovery_cost_minor")
            )
            if not unknown:
                costs = sum(
                    from_minor_units(e[c] or 0)
                    for c in ("provider_cost_estimate_minor",
                              "payment_cost_minor", "recovery_cost_minor")
                )
                refund = from_minor_units(e["refund_minor"] or 0)
                margin_known += round((gross - costs - refund) * share, 2)
                margin_bookings += 1
        a["detoura_revenue_gross"] = round(revenue_gross, 2)
        a["detoura_margin_known"] = (
            round(margin_known, 2) if margin_bookings else None
        )
        a["margin_bookings"] = margin_bookings

    out = []
    for code, a in sorted(agg.items(), key=lambda kv: -kv[1]["tickets_total"]):
        meta = airline_for(code, name=a["carrier_name"])
        issued = a["tickets_issued"]
        attempted = issued + a["tickets_failed"]
        out.append({
            "iata_code": code,
            "name": meta.display_name,
            "logo_key": meta.logo_key,
            "tickets_total": a["tickets_total"],
            "tickets_issued": issued,
            "tickets_failed": a["tickets_failed"],
            "bookings": len(a["bookings"]),
            "supplier_spend": round(a["supplier_spend"], 2),
            "avg_fare": round(sum(a["fares"]) / len(a["fares"]), 2) if a["fares"] else None,
            "issuance_failure_rate": (
                round(a["tickets_failed"] / attempted, 4) if attempted else None
            ),
            "detoura_revenue_gross": a["detoura_revenue_gross"],
            "detoura_margin_known": a["detoura_margin_known"],
            "margin_bookings": a["margin_bookings"],
            "currency": a["currency"],
        })

    # Operation rates (cancellation / change / recovery) per carrier, from
    # ticket_operations joined back to the item's carrier.
    _attach_operation_rates(db, out, dim, clause, params)
    return {
        "dimension": dim,
        "test_data": True,
        "window": {
            "since": since.isoformat() if since else None,
            "until": until.isoformat() if until else None,
        },
        "airlines": out,
    }


def _attach_operation_rates(db: Database, out: list[dict], dim: str,
                            clause: str, params: list) -> None:
    ops = db.query(
        "SELECT o.kind, o.state, i.carrier, i.operating_carrier "
        "FROM ticket_operations o "
        "JOIN booking_items i ON i.booking_id = o.booking_id "
        "AND (o.sequence = 0 OR o.sequence = i.sequence) "
        "JOIN bookings b ON b.booking_id = o.booking_id"
        f"{clause}",
        tuple(params),
    )
    counts: dict[str, dict[str, int]] = {}
    for r in ops:
        code = (
            (r["operating_carrier"] or r["carrier"]) if dim == "operating"
            else r["carrier"]
        ) or "??"
        c = counts.setdefault(code.upper(), {"cancel": 0, "change": 0, "recovery": 0,
                                             "cancel_ok": 0})
        if r["kind"] == "CANCELLATION":
            c["cancel"] += 1
            if r["state"] in ("CANCELLED", "REFUNDED", "REFUND_PENDING",
                              "PARTIALLY_REFUNDED", "NON_REFUNDABLE"):
                c["cancel_ok"] += 1
        elif r["kind"] == "CHANGE":
            c["change"] += 1
        elif r["kind"] == "RECOVERY":
            c["recovery"] += 1
    for row in out:
        c = counts.get(row["iata_code"], {})
        tickets = row["tickets_total"] or 1
        row["cancellations"] = c.get("cancel", 0)
        row["changes"] = c.get("change", 0)
        row["recoveries"] = c.get("recovery", 0)
        row["cancellation_rate"] = round(c.get("cancel", 0) / tickets, 4)
        row["change_rate"] = round(c.get("change", 0) / tickets, 4)
