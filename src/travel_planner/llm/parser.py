"""An LLM preference parser that cannot produce an invalid search (V4).

The higher-risk seam, and it is second on purpose. A model in front of the
optimizer decides *what gets searched*, so a hallucinated destination or an
invented budget is not a cosmetic error - it is a wrong trip, confidently
delivered.

**The guard is the type.** ``TripRequest`` is a validated pydantic model that
already rejects unknown experience names, contradictory destination lists,
impossible date windows and non-positive budgets. V3 tightened that validation
"precisely because it is the layer that would eventually face model output" -
this is that layer arriving. A model that hallucinates gets a ``ValidationError``,
not a search.

**Three lines of defence, in order.** Structured output constrains the reply to
a schema; validation rejects what the schema cannot express; and on failure the
model is shown its own error and asked once more, after which the keyword
parser answers instead. The pipeline degrades to V3 rather than to nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from pydantic import ValidationError

from ..models.destination import EXPERIENCE_ATTRIBUTES
from ..models.trip import (
    AccommodationPreference,
    TransportType,
    TravelPreferences,
    TravelStyle,
    TripRequest,
)
from ..profiles import ProfileName
from .client import DEFAULT_MAX_TOKENS, LlmClient, LlmError, parse_json_object
from .interfaces import KeywordPreferenceParser

#: Preference weights the model may set, which is exactly the set the domain
#: model accepts. Deriving it rather than listing it means a new attribute
#: cannot be added to the engine and forgotten here.
PREFERENCE_KEYS: tuple[str, ...] = EXPERIENCE_ATTRIBUTES + ("multiple_cities",)


def request_schema() -> dict:
    """The JSON schema the model's reply is constrained to.

    Mirrors ``TripRequest``'s optional fields, but is not a substitute for
    validating against it: a schema can say "a string", only the domain model
    knows whether *this* string is a city the catalog has heard of.
    """
    return {
        "type": "object",
        "properties": {
            "budget": {"type": "number"},
            "travelers": {"type": "integer"},
            "duration_days": {"type": "integer"},
            "origin": {"type": "string"},
            "date_flexible": {"type": "boolean"},
            "transport_preferences": {
                "type": "array",
                "items": {"type": "string", "enum": [t.value for t in TransportType]},
            },
            "preferred_destinations": {"type": "array", "items": {"type": "string"}},
            "avoid_destinations": {"type": "array", "items": {"type": "string"}},
            "must_visit": {"type": "array", "items": {"type": "string"}},
            "previously_visited": {"type": "array", "items": {"type": "string"}},
            "preferred_experiences": {
                "type": "array",
                "items": {"type": "string", "enum": list(EXPERIENCE_ATTRIBUTES)},
            },
            "disliked_experiences": {
                "type": "array",
                "items": {"type": "string", "enum": list(EXPERIENCE_ATTRIBUTES)},
            },
            "preferred_city_count": {"type": ["integer", "null"]},
            "accommodation_preference": {
                "type": "string",
                "enum": [p.value for p in AccommodationPreference],
            },
            "travel_style": {
                "type": "string",
                "enum": [s.value for s in TravelStyle],
            },
            "profile": {
                "type": ["string", "null"],
                "enum": [p.value for p in ProfileName] + [None],
            },
            "preferences": {
                "type": "object",
                "properties": {
                    key: {"type": "number", "minimum": 0, "maximum": 1}
                    for key in PREFERENCE_KEYS
                },
                "additionalProperties": False,
            },
        },
        "required": ["budget", "travelers", "duration_days"],
        "additionalProperties": False,
    }


SYSTEM_PROMPT = f"""You turn a traveler's own words into a structured trip \
request. You are a form-filler, not a travel agent.

Rules:
- Extract only what the traveler actually said or clearly implied. Leave \
anything else out; the planner has sensible defaults.
- Never invent a budget, a party size, a duration, or a destination. If the \
traveler did not say, omit the field.
- Preference weights run 0 to 1 and mean "how much does this person care", not \
"how good is it". Only include an interest they expressed.
- Valid experience names are exactly: {", ".join(EXPERIENCE_ATTRIBUTES)}.
- "preferred_destinations" are places they want to go; "avoid_destinations" are \
places they do not. A place must never appear in both.
- Budgets are for the whole party unless the traveler says per person."""


@dataclass
class ParseAttempt:
    """One round trip, kept so a caller can see what actually happened."""

    reply: str
    error: str | None = None


@dataclass
class LlmPreferenceParser:
    """A :class:`~travel_planner.llm.interfaces.PreferenceParser` using Claude.

    Everything the traveler did not say is supplied by the caller's defaults,
    not by the model: the origin, the date window and the fallbacks are the
    application's business, and a model asked to guess them will.
    """

    client: LlmClient
    default_origin: str = "Köln"
    default_date_from: date = date(2026, 9, 10)
    default_duration_days: int = 5
    default_budget: float = 500.0
    default_travelers: int = 2
    max_tokens: int = DEFAULT_MAX_TOKENS
    strict: bool = False
    """Raise instead of falling back to the keyword parser."""
    attempts: list[ParseAttempt] = field(default_factory=list)

    def _defaults(self) -> dict:
        return {
            "origin": self.default_origin,
            "budget": self.default_budget,
            "travelers": self.default_travelers,
            "duration_days": self.default_duration_days,
            "date_from": self.default_date_from,
            "date_to": self.default_date_from + timedelta(days=self.default_duration_days),
        }

    def _build(self, extracted: dict) -> TripRequest:
        """Merge the model's extraction onto the caller's defaults.

        The date window is derived here rather than taken from the model: it
        depends on ``duration_days``, and a model that returns an inconsistent
        pair would produce a request that is valid and wrong.
        """
        payload = self._defaults()
        preferences = extracted.pop("preferences", None)
        # Unknown keys are dropped rather than passed through: a schema-free
        # fallback reply can carry anything, and TripRequest forbids extras.
        payload.update(
            {
                key: value
                for key, value in extracted.items()
                if key in TripRequest.model_fields and value is not None
            }
        )
        duration = int(payload.get("duration_days") or self.default_duration_days)
        payload["duration_days"] = duration
        payload["date_to"] = payload["date_from"] + timedelta(days=duration)
        if isinstance(preferences, dict):
            payload["preferences"] = TravelPreferences(
                **{
                    key: float(value)
                    for key, value in preferences.items()
                    if key in PREFERENCE_KEYS and value is not None
                }
            )
        return TripRequest(**payload)

    def parse(self, text: str) -> TripRequest:
        """Free text in, a validated :class:`TripRequest` out.

        Retries once with the validation error fed back, then falls back to the
        keyword parser. The retry is worth its cost because the common failure -
        a plausible-looking city the catalog has never heard of - is one the
        model can fix when it is told.
        """
        self.attempts = []
        message = f"Traveler's request:\n{text}"

        for attempt in range(2):
            try:
                reply = self.client.complete(
                    system=SYSTEM_PROMPT,
                    user=message,
                    schema=request_schema(),
                    max_tokens=self.max_tokens,
                )
            except LlmError:
                if self.strict:
                    raise
                return self._fallback(text)

            try:
                request = self._build(parse_json_object(reply))
            except (ValidationError, LlmError, ValueError, TypeError) as exc:
                detail = str(exc)
                self.attempts.append(ParseAttempt(reply=reply, error=detail))
                if attempt == 0:
                    message = (
                        f"Traveler's request:\n{text}\n\n"
                        f"Your previous answer was rejected by the planner:\n{detail}\n"
                        "Correct it. Omit anything you are unsure about."
                    )
                    continue
                if self.strict:
                    raise LlmError(f"the model could not produce a valid request: {detail}")
                return self._fallback(text)
            self.attempts.append(ParseAttempt(reply=reply))
            return request

        return self._fallback(text)  # pragma: no cover - loop always returns

    def _fallback(self, text: str) -> TripRequest:
        """V3's keyword parser: duller, and it always produces something valid."""
        return KeywordPreferenceParser(
            default_origin=self.default_origin,
            default_date_from=self.default_date_from,
        ).parse(text)
