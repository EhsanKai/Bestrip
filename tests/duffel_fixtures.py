"""OFFLINE DUFFEL FIXTURES (V7.5).

**These are not real Duffel responses.** They are minimal payloads *modelled on
the documented shape* of Duffel's Offer Request API, written so the adapter and
its normalization can be built and tested with no network and no token.

The distinction matters and is kept deliberately loud. A fixture that claimed
to be a captured response would let "the parser works" quietly mean "the parser
works against what we imagined". Until a real ``duffel_test_`` token has been
used to fetch one sandbox response and compare it against these shapes, the
live path is **UNVERIFIED**, and no test in this repository may report
otherwise.

What the adapter is allowed to assume is therefore narrow: the field *names*
below, and that absent fields are absent rather than null-shaped. Everything
else the parser must tolerate, which is why the malformed and missing-field
cases exist alongside the happy ones.
"""

from __future__ import annotations

from copy import deepcopy

#: Duffel quotes money as decimal *strings*, not numbers - preserving them as
#: strings is how a provider avoids binary-float drift, and the adapter must
#: not undo that carelessly.
_ADULT = {"id": "pas_0000000000000000000000", "type": "adult"}


def _segment(
    origin: str,
    destination: str,
    departing_at: str,
    arriving_at: str,
    duration: str,
    *,
    carrier: str = "VY",
    carrier_name: str = "Vueling",
    flight_number: str = "1234",
    baggages: list[dict] | None = None,
    include_passengers: bool = True,
) -> dict:
    segment = {
        "id": f"seg_{origin}{destination}{flight_number}",
        "origin": {"iata_code": origin, "name": f"{origin} Airport"},
        "destination": {"iata_code": destination, "name": f"{destination} Airport"},
        "departing_at": departing_at,
        "arriving_at": arriving_at,
        "duration": duration,
        "marketing_carrier": {"iata_code": carrier, "name": carrier_name},
        "operating_carrier": {"iata_code": carrier, "name": carrier_name},
        "marketing_carrier_flight_number": flight_number,
    }
    if include_passengers:
        segment["passengers"] = [
            {
                "passenger_id": _ADULT["id"],
                "cabin_class": "economy",
                # Duffel states inclusion as a quantity. Zero means "not in this
                # fare" - it does *not* say whether it can be bought, which is
                # the whole reason UNKNOWN has to survive this mapping.
                "baggages": baggages
                if baggages is not None
                else [{"type": "carry_on", "quantity": 1},
                      {"type": "checked", "quantity": 0}],
            }
        ]
    return segment


def _offer(
    offer_id: str,
    slices: list[dict],
    *,
    total_amount: str = "142.51",
    total_currency: str = "EUR",
    expires_at: str | None = "2026-10-15T12:30:00Z",
    requires_instant_payment: bool = True,
    available_services: list[dict] | None = None,
    owner: str = "VY",
) -> dict:
    offer = {
        "id": offer_id,
        "total_amount": total_amount,
        "total_currency": total_currency,
        "base_amount": total_amount,
        "base_currency": total_currency,
        "owner": {"iata_code": owner, "name": "Vueling"},
        "passengers": [_ADULT],
        "slices": slices,
        "payment_requirements": {
            "requires_instant_payment": requires_instant_payment,
            "payment_required_by": None if requires_instant_payment else "2026-10-14T00:00:00Z",
            "price_guarantee_expires_at": None if requires_instant_payment else "2026-10-13T00:00:00Z",
        },
    }
    if expires_at is not None:
        offer["expires_at"] = expires_at
    if available_services is not None:
        offer["available_services"] = available_services
    return offer


def _slice(origin: str, destination: str, duration: str, segments: list[dict]) -> dict:
    return {
        "id": f"sli_{origin}{destination}",
        "duration": duration,
        "origin": {"iata_code": origin, "name": f"{origin} Airport", "city_name": origin},
        "destination": {"iata_code": destination, "name": f"{destination} Airport",
                        "city_name": destination},
        "segments": segments,
    }


def response(offers: list[dict]) -> dict:
    """Wrap offers the way an Offer Request response carries them."""
    return {"data": {"id": "orq_0000000000000000000000", "offers": offers}}


# ---------------------------------------------------------------------------
# A. Direct flight, cabin bag included, checked bag not in fare
# ---------------------------------------------------------------------------
DIRECT = response([
    _offer("off_direct", [
        _slice("CGN", "BCN", "PT2H10M", [
            _segment("CGN", "BCN", "2026-10-15T08:00:00", "2026-10-15T10:10:00", "PT2H10M"),
        ]),
    ]),
])

# ---------------------------------------------------------------------------
# B. One stop. The connection airport must NOT become a visited city.
# ---------------------------------------------------------------------------
ONE_STOP = response([
    _offer("off_onestop", [
        _slice("CGN", "MAD", "PT6H05M", [
            _segment("CGN", "LHR", "2026-10-15T07:00:00", "2026-10-15T07:50:00",
                     "PT1H50M", carrier="BA", carrier_name="British Airways",
                     flight_number="911"),
            _segment("LHR", "MAD", "2026-10-15T10:30:00", "2026-10-15T13:05:00",
                     "PT2H35M", carrier="BA", carrier_name="British Airways",
                     flight_number="456"),
        ]),
    ], total_amount="203.40"),
])

# ---------------------------------------------------------------------------
# C. Two slices (a return). Only the requested direction may be priced as a leg.
# ---------------------------------------------------------------------------
MULTI_SLICE = response([
    _offer("off_return", [
        _slice("CGN", "BCN", "PT2H10M", [
            _segment("CGN", "BCN", "2026-10-15T08:00:00", "2026-10-15T10:10:00", "PT2H10M"),
        ]),
        _slice("BCN", "CGN", "PT2H15M", [
            _segment("BCN", "CGN", "2026-10-22T18:00:00", "2026-10-22T20:15:00",
                     "PT2H15M", flight_number="1235"),
        ]),
    ], total_amount="266.80"),
])

# ---------------------------------------------------------------------------
# F. Cabin bag included / G. checked bag purchasable / H. unknown / I. neither
# ---------------------------------------------------------------------------
CABIN_INCLUDED_CHECKED_FOR_SALE = response([
    _offer("off_bagsale", [
        _slice("CGN", "BCN", "PT2H10M", [
            _segment("CGN", "BCN", "2026-10-15T08:00:00", "2026-10-15T10:10:00", "PT2H10M",
                     baggages=[{"type": "carry_on", "quantity": 1},
                               {"type": "checked", "quantity": 0}]),
        ]),
    ], available_services=[
        {"id": "ase_checked", "type": "baggage", "total_amount": "25.00",
         "total_currency": "EUR",
         "metadata": {"type": "checked", "maximum_weight_kg": 23}},
    ]),
])

#: No baggage information at all on the segment. Must stay UNKNOWN - the single
#: most important negative case in this file.
BAGGAGE_SILENT = response([
    _offer("off_nobag", [
        _slice("CGN", "BCN", "PT2H10M", [
            _segment("CGN", "BCN", "2026-10-15T08:00:00", "2026-10-15T10:10:00",
                     "PT2H10M", baggages=[]),
        ]),
    ]),
])

#: The `passengers` block is missing entirely, so nothing is stated about bags.
BAGGAGE_ABSENT = response([
    _offer("off_nopax", [
        _slice("CGN", "BCN", "PT2H10M", [
            _segment("CGN", "BCN", "2026-10-15T08:00:00", "2026-10-15T10:10:00",
                     "PT2H10M", include_passengers=False),
        ]),
    ]),
])

#: Cabin bag explicitly included on leg 1 and explicitly absent on leg 2 - a
#: journey where the traveller could not carry the same bag throughout.
BAGGAGE_MIXED_LEGS = response([
    _offer("off_mixedbag", [
        _slice("CGN", "MAD", "PT6H05M", [
            _segment("CGN", "LHR", "2026-10-15T07:00:00", "2026-10-15T07:50:00", "PT1H50M",
                     baggages=[{"type": "carry_on", "quantity": 1}]),
            _segment("LHR", "MAD", "2026-10-15T10:30:00", "2026-10-15T13:05:00", "PT2H35M",
                     flight_number="456", baggages=[]),
        ]),
    ]),
])

# ---------------------------------------------------------------------------
# E / Q. Expiry
# ---------------------------------------------------------------------------
NO_EXPIRY = response([
    _offer("off_noexpiry", [
        _slice("CGN", "BCN", "PT2H10M", [
            _segment("CGN", "BCN", "2026-10-15T08:00:00", "2026-10-15T10:10:00", "PT2H10M"),
        ]),
    ], expires_at=None),
])

EXPIRED = response([
    _offer("off_expired", [
        _slice("CGN", "BCN", "PT2H10M", [
            _segment("CGN", "BCN", "2026-10-15T08:00:00", "2026-10-15T10:10:00", "PT2H10M"),
        ]),
    ], expires_at="2020-01-01T00:00:00Z"),
])

# ---------------------------------------------------------------------------
# J / K. Hold capability
# ---------------------------------------------------------------------------
HOLDABLE = response([
    _offer("off_hold", [
        _slice("CGN", "BCN", "PT2H10M", [
            _segment("CGN", "BCN", "2026-10-15T08:00:00", "2026-10-15T10:10:00", "PT2H10M"),
        ]),
    ], requires_instant_payment=False),
])

# ---------------------------------------------------------------------------
# L / M. Currency
# ---------------------------------------------------------------------------
EUR_OFFER = DIRECT
USD_OFFER = response([
    _offer("off_usd", [
        _slice("CGN", "BCN", "PT2H10M", [
            _segment("CGN", "BCN", "2026-10-15T08:00:00", "2026-10-15T10:10:00", "PT2H10M"),
        ]),
    ], total_amount="160.00", total_currency="USD"),
])

#: A currency nothing in the rate table can convert.
UNCONVERTIBLE_CURRENCY = response([
    _offer("off_xyz", [
        _slice("CGN", "BCN", "PT2H10M", [
            _segment("CGN", "BCN", "2026-10-15T08:00:00", "2026-10-15T10:10:00", "PT2H10M"),
        ]),
    ], total_amount="500.00", total_currency="ZZZ"),
])

# ---------------------------------------------------------------------------
# N / O. Malformed and missing
# ---------------------------------------------------------------------------
NO_OFFERS = response([])

MALFORMED_OFFERS = response([
    {"id": "off_nosl", "total_amount": "10.00", "total_currency": "EUR"},      # no slices
    {"id": "off_noamount", "slices": [                                          # no price
        _slice("CGN", "BCN", "PT2H10M", [
            _segment("CGN", "BCN", "2026-10-15T08:00:00", "2026-10-15T10:10:00", "PT2H10M")])]},
    {"slices": []},                                                              # no id
    _offer("off_badtime", [
        _slice("CGN", "BCN", "PT2H10M", [
            _segment("CGN", "BCN", "not-a-timestamp", "also-not", "PT2H10M")])]),
    {"id": "off_nocurrency", "slices": [                                        # amount, no currency
        _slice("CGN", "BCN", "PT2H10M", [
            _segment("CGN", "BCN", "2026-10-15T08:00:00", "2026-10-15T10:10:00",
                     "PT2H10M")])], "total_amount": "99.00"},
    _offer("off_negative", [
        _slice("CGN", "BCN", "PT2H10M", [
            _segment("CGN", "BCN", "2026-10-15T08:00:00", "2026-10-15T10:10:00", "PT2H10M")])],
        total_amount="-5.00"),
])

#: One usable offer buried among broken ones. The parser must return the good
#: one rather than failing the whole page - a single malformed offer must not
#: cost a traveller every other flight that day.
MIXED_VALID_AND_BROKEN = response(
    MALFORMED_OFFERS["data"]["offers"] + DIRECT["data"]["offers"]
)

# ---------------------------------------------------------------------------
# P. Provider error payload
# ---------------------------------------------------------------------------
ERROR_PAYLOAD = {
    "errors": [
        {"type": "authentication_error", "title": "Unauthorized",
         "message": "The access token is invalid", "code": "unauthorized"}
    ]
}

RATE_LIMIT_PAYLOAD = {
    "errors": [
        {"type": "rate_limit_error", "title": "Too many requests",
         "message": "Rate limit exceeded", "code": "rate_limit_exceeded"}
    ]
}


def clone(fixture: dict) -> dict:
    """A deep copy, so a test that mutates a fixture cannot poison the next."""
    return deepcopy(fixture)
