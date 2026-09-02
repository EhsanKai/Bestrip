"""Ground-transfer domain model (V2).

V1 treated ``CGN``, ``DUS`` and ``FRA`` as interchangeable starting points. In
reality the journey starts at the user's front door: a cheaper flight out of a
distant airport can lose once the trip to that airport is paid for.

Prices are **per person** (a train or bus ticket), matching how
:class:`~detoura.models.transport.TransportOption` is priced.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class GroundTransferMode(str, Enum):
    TRAIN = "train"
    BUS = "bus"
    TAXI = "taxi"


class GroundTransferOption(BaseModel):
    """One way of getting between the user's origin and a departure airport."""

    model_config = ConfigDict(frozen=True)

    id: str
    origin: str
    """The user's origin as they typed it, e.g. ``"Köln"``."""
    airport: str
    """The departure airport code, e.g. ``"CGN"``."""

    mode: GroundTransferMode = GroundTransferMode.TRAIN
    price_per_person: float = Field(ge=0.0)
    duration_minutes: int = Field(ge=0)

    def total_price(self, travelers: int) -> float:
        if travelers < 1:
            raise ValueError("travelers must be >= 1")
        return round(self.price_per_person * travelers, 2)

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return (
            f"{self.origin}<->{self.airport} {self.mode.value} "
            f"{self.price_per_person:.2f}/pp {self.duration_minutes}m"
        )
