"""Decorators that turn provider exceptions into recorded failures (V5.1.1).

A real provider raising mid-search presents the caller with two bad options:
let it propagate and lose eight hundred viable itineraries because one route
lookup timed out, or swallow it and report an outage as an empty result. The
spec forbids the second and the first is not a product.

The third option is to degrade: return no options *for that lookup*, record why,
and let the run finish on the data that did arrive. The result then says
explicitly that it is incomplete. That is what these decorators do, and they
are decorators for the same reason the caching ones are - they compose with a
provider this project did not write.

Deliberately **not** applied by default. The synthetic providers cannot fail,
so wrapping them would add a layer that never fires; and a caller running
against a real provider should opt into degradation consciously, because
"finish on partial data" is a product decision, not a default.
"""

from __future__ import annotations

from datetime import date

from ..models.accommodation import AccommodationOption
from ..models.transfer import GroundTransferOption
from ..models.transport import TransportOption
from .accommodation import AccommodationDataProvider
from .failures import FailureLog, ProviderFailureKind
from .ground_transfer import GroundTransferProvider
from .http import ProviderHttpError, RateLimitExceeded
from .transport import TransportDataProvider


def classify(error: Exception) -> ProviderFailureKind:
    """Map a provider exception onto the failure taxonomy.

    Ordered most specific first. The fallback is ``UNAVAILABLE`` rather than
    ``MALFORMED_RESPONSE`` because an unrecognised exception is more likely to
    be the world misbehaving than the payload, and ``UNAVAILABLE`` is the
    honest "we don't know, try again".
    """
    # Imported lazily: the Amadeus module is one provider among several and
    # this layer must not require it to be importable.
    from .amadeus import AmadeusAuthError

    if isinstance(error, AmadeusAuthError):
        return ProviderFailureKind.AUTHENTICATION_FAILED
    if isinstance(error, RateLimitExceeded):
        return ProviderFailureKind.RATE_LIMITED
    if isinstance(error, TimeoutError):
        return ProviderFailureKind.TIMEOUT
    if isinstance(error, ProviderHttpError):
        text = str(error).lower()
        if "timed out" in text or "timeout" in text:
            return ProviderFailureKind.TIMEOUT
        if "not json" in text or "expected a json" in text:
            return ProviderFailureKind.MALFORMED_RESPONSE
        if "cannot price" in text or "exchange rate" in text:
            return ProviderFailureKind.CURRENCY_UNAVAILABLE
        return ProviderFailureKind.UNAVAILABLE
    return ProviderFailureKind.UNAVAILABLE


class _Resilient:
    """Shared machinery: name the provider, record, degrade."""

    def __init__(self, inner: object, failures: FailureLog, name: str | None = None):
        self.inner = inner
        self.failures = failures
        self.name = name or type(inner).__name__

    def __getattr__(self, attribute: str):
        # Anything not explicitly wrapped passes straight through, so a
        # decorated provider keeps its introspection surface.
        return getattr(self.inner, attribute)

    def _record(self, error: Exception, context: str) -> None:
        self.failures.record(
            classify(error), self.name, detail=str(error), context=context
        )


class ResilientTransportProvider(_Resilient):
    """A transport provider that degrades instead of aborting the search."""

    def __init__(
        self,
        inner: TransportDataProvider,
        failures: FailureLog,
        name: str | None = None,
    ) -> None:
        super().__init__(inner, failures, name)

    def search(
        self, origin: str, destination: str, departure_date: date
    ) -> list[TransportOption]:
        try:
            return self.inner.search(origin, destination, departure_date)
        except Exception as error:  # noqa: BLE001 - every failure is degradable here
            self._record(error, f"{origin}->{destination} {departure_date.isoformat()}")
            return []


class ResilientAccommodationProvider(_Resilient):
    """An accommodation provider that degrades instead of aborting."""

    def __init__(
        self,
        inner: AccommodationDataProvider,
        failures: FailureLog,
        name: str | None = None,
    ) -> None:
        super().__init__(inner, failures, name)

    def search(
        self, city: str, check_in: date, check_out: date, travelers: int
    ) -> list[AccommodationOption]:
        try:
            return self.inner.search(city, check_in, check_out, travelers)
        except Exception as error:  # noqa: BLE001
            self._record(error, f"{city} {check_in.isoformat()}..{check_out.isoformat()}")
            return []

    def min_price_per_night(self, city: str, travelers: int) -> float | None:
        """Degrades to ``None``, which is the admissible answer.

        This one matters more than it looks. ``None`` means *unknown*, and the
        constraint layer treats an unknown bound as "do not prune" - so a
        provider failure here loses pruning efficiency and never correctness.
        Returning ``0.0`` would also be admissible but would waste the bound;
        returning a guess would be the inadmissibility bug V3 already fixed
        once.
        """
        try:
            return self.inner.min_price_per_night(city, travelers)
        except Exception as error:  # noqa: BLE001
            self._record(error, f"min_price_per_night {city}")
            return None


class ResilientGroundTransferProvider(_Resilient):
    """A ground-transfer provider that degrades instead of aborting."""

    def __init__(
        self,
        inner: GroundTransferProvider,
        failures: FailureLog,
        name: str | None = None,
    ) -> None:
        super().__init__(inner, failures, name)

    def search(self, origin: str, airport: str) -> list[GroundTransferOption]:
        try:
            return self.inner.search(origin, airport)
        except Exception as error:  # noqa: BLE001
            self._record(error, f"{origin}->{airport}")
            return []
