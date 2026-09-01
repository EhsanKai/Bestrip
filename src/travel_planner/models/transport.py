"""Transportation domain model."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TransportType(str, Enum):
    """The transport modes supported by the MVP."""

    FLIGHT = "flight"
    TRAIN = "train"
    BUS = "bus"


class TransportOption(BaseModel):
    """A single bookable transportation leg between two network nodes.

    ``price_per_person`` is deliberately named: the optimizer must always
    multiply by the number of travelers before comparing against a budget.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    origin: str
    destination: str
    departure: datetime
    arrival: datetime
    price_per_person: float = Field(ge=0.0)
    transport_type: TransportType
    duration_minutes: int = Field(ge=0)
    operator: str = "synthetic"

    @model_validator(mode="before")
    @classmethod
    def _derive_duration(cls, data: object) -> object:
        """Allow ``duration_minutes`` to be omitted and derived from the times."""
        if isinstance(data, dict) and data.get("duration_minutes") is None:
            departure, arrival = data.get("departure"), data.get("arrival")
            if isinstance(departure, datetime) and isinstance(arrival, datetime):
                data = dict(data)
                data["duration_minutes"] = int((arrival - departure).total_seconds() // 60)
        return data

    @model_validator(mode="after")
    def _check_consistency(self) -> "TransportOption":
        if self.arrival < self.departure:
            raise ValueError(f"{self.id}: arrival {self.arrival} precedes departure {self.departure}")
        if self.origin == self.destination:
            raise ValueError(f"{self.id}: origin and destination are identical ({self.origin})")
        elapsed = int((self.arrival - self.departure).total_seconds() // 60)
        if abs(elapsed - self.duration_minutes) > 1:
            raise ValueError(
                f"{self.id}: duration_minutes={self.duration_minutes} disagrees with "
                f"arrival - departure = {elapsed} minutes"
            )
        return self

    def total_price(self, travelers: int) -> float:
        """Total price of this leg for the whole party."""
        if travelers < 1:
            raise ValueError("travelers must be >= 1")
        return round(self.price_per_person * travelers, 2)

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return (
            f"{self.origin}->{self.destination} {self.transport_type.value} "
            f"{self.departure:%Y-%m-%d %H:%M} ({self.duration_minutes}m) "
            f"{self.price_per_person:.2f}/pp"
        )
