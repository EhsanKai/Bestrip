"""One safe, read-only look at Duffel Test Mode (V7.5).

    python -m detoura.tools.duffel_probe --origin CGN --destination BCN --date 2026-10-15

What it does: exactly one Offer Request, and prints what came back through
Detoura's own normalization - so the thing being checked is not "did Duffel
reply" but "did our mapping survive contact with a real reply".

What it will not do, by construction:

* run without a ``duffel_test_`` token - it exits cleanly and says so, rather
  than sending a request that might be live;
* print the token, in any form, including in an error;
* create an Order, a payment, or anything else that costs money or issues a
  ticket. There is no code path here that writes to Duffel.

Until this has been run against a real sandbox token and its output compared
with ``tests/duffel_fixtures.py``, the live path is **UNVERIFIED** and must be
described that way.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date

from ..providers.duffel import (
    DuffelConfigurationError,
    DuffelTransportProvider,
    is_test_token,
    redact,
)
from ..providers.http import ProviderHttpError

EXIT_OK = 0
EXIT_NO_TOKEN = 2
EXIT_PROVIDER_ERROR = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="duffel_probe",
        description="One read-only Duffel Test Mode offer search. Creates nothing.",
    )
    parser.add_argument("--origin", required=True, help="IATA code, e.g. CGN")
    parser.add_argument("--destination", required=True, help="IATA code, e.g. BCN")
    parser.add_argument("--date", required=True, help="departure date, YYYY-MM-DD")
    parser.add_argument("--travelers", type=int, default=1)
    parser.add_argument(
        "--cabin", default="economy",
        choices=["economy", "premium_economy", "business", "first"],
        help="cabin class to request",
    )
    parser.add_argument("--limit", type=int, default=5, help="offers to display")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = os.getenv("DUFFEL_ACCESS_TOKEN")

    if not token:
        print("Duffel Test Mode token not configured.")
        print("Set DUFFEL_ACCESS_TOKEN to a duffel_test_... token and retry.")
        print("No request was sent.")
        return EXIT_NO_TOKEN
    if not is_test_token(token):
        # Deliberately refuses rather than asking. A token that might be live
        # is treated as live, because the cost of guessing wrong is somebody's
        # real money.
        print(f"Configured token is {redact(token)} - not a test-mode token.")
        print("Refusing to send any request. No request was sent.")
        return EXIT_NO_TOKEN

    try:
        departure = date.fromisoformat(args.date)
    except ValueError:
        print(f"--date must be YYYY-MM-DD, got {args.date!r}")
        return EXIT_PROVIDER_ERROR

    provider = DuffelTransportProvider(access_token=token)
    # Says the mode outright. Someone reading this output six months from now
    # must not have to infer from a token prefix whether real money was in
    # play, so the banner states it rather than implying it.
    print("=== DUFFEL TEST MODE ===")
    print(f"Token:    {redact(token)}")
    print(f"Route:    {args.origin} -> {args.destination} on {departure}")
    print(f"Party:    {args.travelers} adult(s), {args.cabin}")
    print()

    try:
        # `fetch_offers`, not `search`. `search` is the TransportDataProvider
        # entry point and now refuses by design, so that beam search cannot
        # reach the network - this probe is the deliberate, single-call path
        # that refusal exists to point at.
        options = provider.fetch_offers(
            args.origin, args.destination, departure,
            travelers=args.travelers, cabin=args.cabin,
        )
    except DuffelConfigurationError as error:
        print(f"Refused: {error}")
        return EXIT_NO_TOKEN
    except ProviderHttpError as error:
        # The message is the adapter's own, which never contains credentials.
        print(f"Provider error: {error}")
        return EXIT_PROVIDER_ERROR

    if not options:
        print("No offers returned for this route and date.")
        print(f"(offers seen: {provider.offers_seen}, unusable: {len(provider.offers_dropped)})")
        return EXIT_OK

    for option in options[: args.limit]:
        reference = option.provider_ref
        baggage = option.baggage
        print(f"  {option.origin} -> {option.destination}")
        print(f"    depart {option.departure:%Y-%m-%d %H:%M}  arrive {option.arrival:%H:%M}"
              f"  ({option.duration_minutes} min)")
        print(f"    carrier {option.operator}"
              f"  stops {max(len(reference.raw_segments) - 1, 0) if reference else '?'}")
        print(f"    price   {option.price_per_person:.2f} per person (normalized)")
        if reference is not None:
            print(f"    expiry  {reference.expires_at or 'not stated'}"
                  f"  ({reference.freshness_at().value})")
            print(f"    hold    {_tri(reference.hold_supported)}")
        if baggage is not None:
            print(f"    cabin   {baggage.cabin_bag.status.value}"
                  f"  checked {baggage.checked_bag.status.value}")
        print()

    print(f"offers seen: {provider.offers_seen}  usable: {len(options)}"
          f"  unusable: {len(provider.offers_dropped)}")
    print("No order was created. No payment was made.")
    return EXIT_OK


def _tri(value: bool | None) -> str:
    """Three states, kept apart - unknown is not 'no'."""
    return "unknown" if value is None else ("supported" if value else "not supported")


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
