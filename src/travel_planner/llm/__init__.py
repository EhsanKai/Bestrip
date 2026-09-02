"""LLM integration seams.

Two protocols mark where a model may plug in - free text -> ``TripRequest`` in
front, ``Itinerary`` -> prose behind - together with dependency-free stand-ins
(V3) and Claude-backed implementations (V4).

The optimizer never imports this package, and a test enforces that by walking
the AST of every module in ``algorithms``, ``constraints``, ``services``,
``providers`` and ``data``. Prices, availability, travel time, feasibility and
scores are computed; nothing here can change one.

The Claude-backed classes are imported lazily so that ``anthropic`` stays an
optional extra: ``from travel_planner.llm import LlmItineraryExplainer`` works
without the SDK installed, and only *using* it needs the dependency.
"""

from .client import AnthropicClient, LlmClient, LlmError, ScriptedClient
from .explainer import LlmItineraryExplainer
from .interfaces import (
    ItineraryExplainer,
    KeywordPreferenceParser,
    PreferenceParser,
    TemplateItineraryExplainer,
)
from .parser import LlmPreferenceParser

__all__ = [
    "AnthropicClient",
    "ItineraryExplainer",
    "KeywordPreferenceParser",
    "LlmClient",
    "LlmError",
    "LlmItineraryExplainer",
    "LlmPreferenceParser",
    "PreferenceParser",
    "ScriptedClient",
    "TemplateItineraryExplainer",
]
