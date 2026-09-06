"""Typed trip edits (V7 Phase 2).

An edit is a *typed operation*, never a free-form dictionary. The difference
matters more than it looks: a dictionary lets an unknown key be silently
dropped, and an edit that is silently dropped is an instruction the traveler
gave and the system ignored. Every operation here is a discriminated model, so
an unrecognised one is a validation error at the boundary rather than a trip
that quietly did not change.

The operations divide cleanly by what they are allowed to do:

**Hard** - expressed as fields on a derived :class:`TripRequest` and enforced
by :class:`~detoura.constraints.validator.ConstraintValidator`. A state that
violates one is never valid, so there is no weight to tune and no weight to get
wrong. Locks, exclusions, budget and duration are all hard.

**Soft** - expressed as similarity and applied when choosing between candidates
that already satisfy every hard constraint. Route order, hotels and specific
departures are soft, because insisting on them exactly would make almost every
edit infeasible.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .trip import AccommodationPreference, TransportType


class PatchOp(str, Enum):
    """Every edit the vocabulary can express.

    Declared in full even though Phase 2 implements a subset: a client that
    sends a not-yet-supported operation gets an explicit refusal naming it,
    which is a better answer than a 422 about an unknown field and a much
    better one than silence.
    """

    LOCK_CITY = "lock_city"
    UNLOCK_CITY = "unlock_city"
    REMOVE_CITY = "remove_city"
    REPLACE_CITY = "replace_city"
    ADD_CITY = "add_city"
    EXCLUDE_CITY = "exclude_city"
    CHANGE_BUDGET = "change_budget"
    CHANGE_TRIP_DURATION = "change_trip_duration"

    # --- declared, refused explicitly until a later phase ---------------
    CHANGE_STAY_DURATION = "change_stay_duration"
    LOCK_STAY_DURATION = "lock_stay_duration"
    CHANGE_TRANSPORT_PREFERENCE = "change_transport_preference"
    CHANGE_ACCOMMODATION_PREFERENCE = "change_accommodation_preference"
    CHANGE_BAGGAGE_REQUIREMENT = "change_baggage_requirement"
    CHANGE_DEPARTURE_LOCATION = "change_departure_location"
    CHANGE_RETURN_LOCATION = "change_return_location"


#: Operations Phase 2 actually carries out.
SUPPORTED_OPS: frozenset[PatchOp] = frozenset(
    {
        PatchOp.LOCK_CITY,
        PatchOp.UNLOCK_CITY,
        PatchOp.REMOVE_CITY,
        PatchOp.REPLACE_CITY,
        PatchOp.ADD_CITY,
        PatchOp.EXCLUDE_CITY,
        PatchOp.CHANGE_BUDGET,
        PatchOp.CHANGE_TRIP_DURATION,
    }
)


class _Operation(BaseModel):
    model_config = ConfigDict(frozen=True)


class LockCity(_Operation):
    """Keep this city. It appears in the result, or there is no result.

    A lock is about the *city*, not the flight, the hotel or the day. Reading
    it as "everything about this stay is frozen" would make almost every edit
    infeasible; those are preferences, and they live in similarity.
    """

    op: Literal["lock_city"] = "lock_city"
    city: str = Field(min_length=1, max_length=120)


class UnlockCity(_Operation):
    """Stop protecting this city. **Not** a removal.

    Unlocking means "you may change this", not "take this out". Collapsing the
    two would delete cities the traveler merely stopped insisting on.
    """

    op: Literal["unlock_city"] = "unlock_city"
    city: str = Field(min_length=1, max_length=120)


class RemoveCity(_Operation):
    """Take this city out of the trip.

    Implemented as an exclusion for the duration of the edit. Re-proposing
    Budapest because it scores well, immediately after being told to remove
    Budapest, reads as the system overruling the traveler.
    """

    op: Literal["remove_city"] = "remove_city"
    city: str = Field(min_length=1, max_length=120)


class ExcludeCity(_Operation):
    """Never offer this city for this trip."""

    op: Literal["exclude_city"] = "exclude_city"
    city: str = Field(min_length=1, max_length=120)


class ReplaceCity(_Operation):
    """Swap a city out, optionally naming what for.

    With no ``replacement`` the optimizer chooses, and the city count is held
    where it was so that "replace" cannot quietly become "remove".
    """

    op: Literal["replace_city"] = "replace_city"
    city: str = Field(min_length=1, max_length=120)
    replacement: str | None = Field(default=None, max_length=120)


class AddCity(_Operation):
    op: Literal["add_city"] = "add_city"
    city: str = Field(min_length=1, max_length=120)


class ChangeBudget(_Operation):
    op: Literal["change_budget"] = "change_budget"
    budget: float = Field(gt=0.0)


class ChangeTripDuration(_Operation):
    op: Literal["change_trip_duration"] = "change_trip_duration"
    duration_days: int = Field(ge=1, le=30)


class ChangeStayDuration(_Operation):
    op: Literal["change_stay_duration"] = "change_stay_duration"
    city: str = Field(min_length=1, max_length=120)
    nights: int = Field(ge=1, le=30)


class LockStayDuration(_Operation):
    op: Literal["lock_stay_duration"] = "lock_stay_duration"
    city: str = Field(min_length=1, max_length=120)


class ChangeTransportPreference(_Operation):
    op: Literal["change_transport_preference"] = "change_transport_preference"
    transport: list[TransportType] = Field(min_length=1, max_length=8)


class ChangeAccommodationPreference(_Operation):
    op: Literal["change_accommodation_preference"] = (
        "change_accommodation_preference"
    )
    accommodation_preference: AccommodationPreference


class ChangeBaggageRequirement(_Operation):
    """Accepted and recorded now; priced in Phase 3.

    Refusing it outright would force a client change the day baggage lands.
    Accepting it silently would be worse - so it is echoed back in the
    unsupported list until there is a baggage model to honour it.
    """

    op: Literal["change_baggage_requirement"] = "change_baggage_requirement"
    baggage: str = Field(min_length=1, max_length=40)


class ChangeDepartureLocation(_Operation):
    op: Literal["change_departure_location"] = "change_departure_location"
    location: str = Field(min_length=1, max_length=120)


class ChangeReturnLocation(_Operation):
    op: Literal["change_return_location"] = "change_return_location"
    location: str = Field(min_length=1, max_length=120)


PatchOperation = Annotated[
    Union[
        LockCity,
        UnlockCity,
        RemoveCity,
        ExcludeCity,
        ReplaceCity,
        AddCity,
        ChangeBudget,
        ChangeTripDuration,
        ChangeStayDuration,
        LockStayDuration,
        ChangeTransportPreference,
        ChangeAccommodationPreference,
        ChangeBaggageRequirement,
        ChangeDepartureLocation,
        ChangeReturnLocation,
    ],
    Field(discriminator="op"),
]

#: Ceiling on operations in one patch. Far above any real edit and far below
#: what would make one request expensive - the same reasoning as the V6.5
#: request bounds, applied before this endpoint is reachable rather than after
#: somebody demonstrates it.
MAX_OPERATIONS = 32


class TripPatch(BaseModel):
    """An ordered set of edits to one selected itinerary.

    Order matters and is preserved: ``remove Berlin`` then ``add Berlin`` is a
    different instruction from the reverse, and resolving them as an unordered
    set would pick one arbitrarily.
    """

    model_config = ConfigDict(frozen=True)

    operations: list[PatchOperation] = Field(
        default_factory=list, max_length=MAX_OPERATIONS
    )

    @model_validator(mode="after")
    def _no_empty_patch(self) -> "TripPatch":
        if not self.operations:
            raise ValueError("a patch must contain at least one operation")
        return self

    @property
    def unsupported(self) -> list[str]:
        """Operations this phase declares but does not yet carry out."""
        return sorted(
            {
                op.op
                for op in self.operations
                if PatchOp(op.op) not in SUPPORTED_OPS
            }
        )
