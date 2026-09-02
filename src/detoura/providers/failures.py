"""Typed provider failures (V5.1.1).

The rule this module exists to enforce, stated as plainly as the spec does it:

    Never silently convert infrastructure failure into "No trips found."

Those two sentences mean opposite things to a traveler. *"No flights under
€450"* is an answer - the search worked and the budget is the problem, so the
useful next move is to relax something. *"The flight provider is unavailable"*
is not an answer at all; nothing about the request is wrong and the useful next
move is to try again. A UI that renders both as an empty list has told the user
something false in one of the two cases.

V4 already got the first half right: the Amadeus provider returns ``[]`` for an
empty page and raises for auth failures and exhausted retries. What it lacked
was a way for that distinction to *survive* to the caller - a raised exception
aborts the whole plan, which is right for a broken integration and much too
blunt when one of four route lookups timed out and the other three worked.

So a failure is now a **value**. Providers record what went wrong, the search
carries on with whatever data it does have, and the result says explicitly that
it is incomplete and why. Partial data honestly labelled beats either a lie or
a crash.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class ProviderFailureKind(str, Enum):
    """Every distinct way a provider can fail to answer.

    Distinct because they need distinct *responses*, not for the taxonomy's own
    sake: a rate limit means wait, a timeout means retry, a malformed response
    means the integration is broken, and no results means change the question.
    """

    NO_RESULTS = "NO_RESULTS"
    """The provider answered, and the answer was "nothing". Not a failure."""

    SOLD_OUT = "SOLD_OUT"
    """Options existed but none had inventory for this party."""

    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    CURRENCY_UNAVAILABLE = "CURRENCY_UNAVAILABLE"
    """A quote arrived in a currency nothing can convert. A misconfiguration."""

    STALE_OFFER = "STALE_OFFER"
    AUTHENTICATION_FAILED = "AUTHENTICATION_FAILED"
    RATE_LIMITED = "RATE_LIMITED"

    @property
    def is_infrastructure(self) -> bool:
        """True when the *system* failed rather than the search.

        This is the predicate the whole module exists for. Only these may
        never be reported to a user as "no trips found".
        """
        return self not in (
            ProviderFailureKind.NO_RESULTS,
            ProviderFailureKind.SOLD_OUT,
        )

    @property
    def is_retryable(self) -> bool:
        """Whether "try again" is honest advice rather than a shrug."""
        return self in (
            ProviderFailureKind.TIMEOUT,
            ProviderFailureKind.UNAVAILABLE,
            ProviderFailureKind.RATE_LIMITED,
        )


#: What a person should be told. Deliberately here rather than in the UI: the
#: backend knows which of these is true, and a frontend that has to infer it
#: from a status code will eventually infer wrong.
FAILURE_MESSAGES: dict[ProviderFailureKind, str] = {
    ProviderFailureKind.NO_RESULTS: "No options on this route.",
    ProviderFailureKind.SOLD_OUT: "Sold out for your party size.",
    ProviderFailureKind.TIMEOUT: "The provider took too long to respond.",
    ProviderFailureKind.UNAVAILABLE: "The provider is temporarily unavailable.",
    ProviderFailureKind.MALFORMED_RESPONSE: (
        "The provider returned data we could not read."
    ),
    ProviderFailureKind.CURRENCY_UNAVAILABLE: (
        "Prices arrived in a currency we cannot convert."
    ),
    ProviderFailureKind.STALE_OFFER: "This price is older than we would like.",
    ProviderFailureKind.AUTHENTICATION_FAILED: (
        "We could not authenticate with the provider."
    ),
    ProviderFailureKind.RATE_LIMITED: "We are being rate limited by the provider.",
}


@dataclass(frozen=True, slots=True)
class ProviderFailure:
    """One thing that went wrong, with enough context to act on it."""

    kind: ProviderFailureKind
    provider: str
    detail: str = ""
    """The technical detail, for logs. Never shown to a traveler as-is."""

    context: str = ""
    """What was being fetched, e.g. ``CGN->VIE 2026-09-10``."""

    occurred_at: datetime | None = None

    @property
    def message(self) -> str:
        """The sentence a person should read."""
        return FAILURE_MESSAGES[self.kind]

    @property
    def is_infrastructure(self) -> bool:
        return self.kind.is_infrastructure

    @property
    def is_retryable(self) -> bool:
        return self.kind.is_retryable

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "provider": self.provider,
            "message": self.message,
            "detail": self.detail,
            "context": self.context,
            "retryable": self.is_retryable,
        }


@dataclass
class FailureLog:
    """Failures collected during one planning run.

    A mutable collector deliberately: providers are called from deep inside the
    search and threading a return value back out through every call site would
    be a redesign for no gain. It is per-run, passed in explicitly, and never
    global.
    """

    failures: list[ProviderFailure] = field(default_factory=list)

    def record(
        self,
        kind: ProviderFailureKind,
        provider: str,
        *,
        detail: str = "",
        context: str = "",
    ) -> None:
        self.failures.append(
            ProviderFailure(
                kind=kind,
                provider=provider,
                detail=detail,
                context=context,
                occurred_at=datetime.now(),
            )
        )

    @property
    def infrastructure_failures(self) -> list[ProviderFailure]:
        """The ones that must never be reported as "no trips found"."""
        return [f for f in self.failures if f.is_infrastructure]

    @property
    def degraded(self) -> bool:
        """True when the search ran on incomplete data.

        The single question the result assembler asks. If this is true and the
        recommendation list is empty, the honest answer is "we could not
        complete the search", not "nothing matched".
        """
        return bool(self.infrastructure_failures)

    def summary(self) -> list[dict[str, object]]:
        """Deduplicated by (kind, provider), because one outage is one message.

        A timeout on forty route lookups is one fact about the world, and
        listing it forty times would be a worse bug report than listing it once
        with a count.
        """
        seen: dict[tuple[str, str], dict[str, object]] = {}
        for failure in self.infrastructure_failures:
            key = (failure.kind.value, failure.provider)
            if key in seen:
                seen[key]["occurrences"] = int(seen[key]["occurrences"]) + 1  # type: ignore[arg-type]
                continue
            entry = failure.as_dict()
            entry["occurrences"] = 1
            seen[key] = entry
        return list(seen.values())

    def clear(self) -> None:
        self.failures.clear()
