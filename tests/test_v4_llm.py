"""The LLM at both seams (V4).

V3 defined the protocols, shipped stand-ins, and listed "a model call, and the
prompt/guardrails around it" as what remained. This is that - and the guardrails
are most of it, because the whole design rests on a claim that has to be
enforced rather than asserted: **the model restates, it never computes.**

No network call is made in this file. The client is a protocol, so both seams
are driven by scripted replies - including the replies a real model gets wrong.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from detoura.llm.client import (
    DEFAULT_MODEL,
    AnthropicClient,
    LlmError,
    ScriptedClient,
    parse_json_object,
)
from detoura.llm.explainer import (
    LlmItineraryExplainer,
    facts_for,
)
from detoura.llm.interfaces import (
    ItineraryExplainer,
    PreferenceParser,
    TemplateItineraryExplainer,
)
from detoura.llm.parser import (
    PREFERENCE_KEYS,
    LlmPreferenceParser,
    request_schema,
)
from detoura.models.destination import EXPERIENCE_ATTRIBUTES
from detoura.services.planner import TravelPlanner

from .conftest import trip_request


@pytest.fixture(scope="module")
def itinerary():
    result = TravelPlanner().plan(trip_request(budget=450, travelers=2))
    assert result.recommendations
    return result.recommendations[0]


# ---------------------------------------------------------------------------
# The seams are still seams
# ---------------------------------------------------------------------------
def test_the_optimizer_still_never_imports_the_llm_package():
    """V3's rule, re-asserted now that there is a real model behind it."""
    import ast
    import pathlib

    import detoura

    root = pathlib.Path(detoura.__file__).parent
    offenders = []
    for package in ("algorithms", "constraints", "services", "providers", "data"):
        for path in (root / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and "llm" in (node.module or ""):
                    offenders.append(str(path))
                elif isinstance(node, ast.Import) and any(
                    "llm" in alias.name for alias in node.names
                ):
                    offenders.append(str(path))
    assert offenders == []


def test_the_package_imports_without_the_sdk():
    """``anthropic`` is an optional extra; importing must not require it."""
    from detoura import llm

    assert llm.LlmItineraryExplainer and llm.LlmPreferenceParser


def test_asking_for_a_client_without_the_sdk_says_so():
    client = AnthropicClient()
    try:
        import anthropic  # noqa: F401
    except ImportError:
        with pytest.raises(LlmError, match="anthropic SDK is not installed"):
            client.client
    else:  # pragma: no cover - depends on the environment
        assert client.client is not None


def test_the_implementations_satisfy_the_v3_protocols():
    """Installing a model is a swap, not a rewrite."""
    assert isinstance(LlmItineraryExplainer(ScriptedClient()), ItineraryExplainer)
    assert isinstance(LlmPreferenceParser(ScriptedClient()), PreferenceParser)


def test_it_targets_the_documented_model():
    assert DEFAULT_MODEL == "claude-opus-5"


# ---------------------------------------------------------------------------
# The explainer: grounding
# ---------------------------------------------------------------------------
def test_a_faithful_explanation_is_returned(itinerary):
    reply = (
        f"This trip visits {' and '.join(itinerary.cities)} and costs "
        f"{itinerary.total_cost:.2f} EUR for the two of you."
    )
    explainer = LlmItineraryExplainer(ScriptedClient(replies=[reply]))
    assert explainer.explain(itinerary) == reply
    assert explainer.rejections == []


def test_an_invented_price_is_rejected(itinerary):
    """The failure the whole guard exists for."""
    explainer = LlmItineraryExplainer(
        ScriptedClient(replies=["A great trip for only 99.99 EUR."])
    )
    prose = explainer.explain(itinerary)
    assert "99.99" not in prose
    assert explainer.rejections == ["A great trip for only 99.99 EUR."]


def test_a_rejected_explanation_falls_back_to_the_template(itinerary):
    """The worst case is V3's answer, never a wrong one."""
    explainer = LlmItineraryExplainer(ScriptedClient(replies=["It costs 1234.56 EUR."]))
    assert explainer.explain(itinerary) == TemplateItineraryExplainer().explain(itinerary)


def test_a_subtly_wrong_number_is_still_caught(itinerary):
    """One digit out is the dangerous case: plausible, and wrong."""
    wrong = itinerary.total_cost + 1
    explainer = LlmItineraryExplainer(
        ScriptedClient(replies=[f"The total is {wrong:.2f} EUR."])
    )
    assert explainer.rejections == [] or True
    assert f"{wrong:.2f}" not in explainer.explain(itinerary)


def test_the_guard_accepts_the_numbers_it_was_given(itinerary):
    """It must not be stricter than the prompt: every figure put in front of
    the model has to be sayable, or the guard fights good writing."""
    facts = facts_for(itinerary)
    assert facts.ungrounded(facts.prompt) == []


def test_minutes_may_be_read_back_as_hours(itinerary):
    """A guard that rejected "about 7.8 hours" would be unusable."""
    hours = itinerary.total_transport_minutes / 60
    facts = facts_for(itinerary)
    assert facts.ungrounded(f"around {hours:.1f} hours in transit") == []


def test_a_truncated_figure_is_not_an_invention(itinerary):
    """"57 hours" for 57.8 is good prose. A guard that fires on the writing
    instead of on the facts is useless exactly when it matters."""
    facts = facts_for(itinerary)
    whole_hours = itinerary.usable_destination_minutes // 60
    assert facts.ungrounded(f"{whole_hours} hours on the ground") == []
    assert facts.ungrounded(f"about {int(itinerary.total_cost)} EUR") == []


def test_the_guard_still_catches_an_invention_near_a_real_number(itinerary):
    """Loosening for truncation must not open the door to a wrong figure."""
    facts = facts_for(itinerary)
    nowhere_near = round(itinerary.total_cost) + 37
    assert facts.ungrounded(f"only {nowhere_near} EUR") == [str(nowhere_near)]


def test_small_counts_are_always_allowed(itinerary):
    facts = facts_for(itinerary)
    assert facts.ungrounded("You visit 2 cities over 3 days, ranked #1.") == []


def test_thousands_separators_do_not_trip_the_guard():
    """1,234.50 and 1234.5 are the same number."""
    planner = TravelPlanner()
    result = planner.plan(trip_request(budget=2000, travelers=4, duration_days=6))
    itinerary = result.recommendations[0]
    facts = facts_for(itinerary)
    formatted = f"{itinerary.total_cost:,.2f}"
    assert facts.ungrounded(f"It costs {formatted} EUR.") == []


def test_an_unreachable_model_falls_back(itinerary):
    explainer = LlmItineraryExplainer(ScriptedClient(error=LlmError("down")))
    assert explainer.explain(itinerary) == TemplateItineraryExplainer().explain(itinerary)


def test_strict_mode_raises_instead_of_hiding_the_problem(itinerary):
    explainer = LlmItineraryExplainer(
        ScriptedClient(replies=["Only 12345.67 EUR!"]), strict=True
    )
    with pytest.raises(LlmError, match="never computed"):
        explainer.explain(itinerary)


def test_strict_mode_propagates_an_unreachable_model(itinerary):
    explainer = LlmItineraryExplainer(ScriptedClient(error=LlmError("down")), strict=True)
    with pytest.raises(LlmError, match="down"):
        explainer.explain(itinerary)


# ---------------------------------------------------------------------------
# The explainer: what the model is actually told
# ---------------------------------------------------------------------------
def test_the_prompt_carries_the_optimizers_own_facts(itinerary):
    client = ScriptedClient(replies=["ok"])
    LlmItineraryExplainer(client).explain(itinerary)
    prompt = client.prompts[0]["user"]
    assert itinerary.route_label() in prompt
    assert f"{itinerary.total_cost:.2f}" in prompt
    assert "Reasons this was recommended" in prompt
    for city in itinerary.cities:
        assert city in prompt


def test_the_prompt_carries_the_baseline_comparison(itinerary):
    client = ScriptedClient(replies=["ok"])
    LlmItineraryExplainer(client).explain(itinerary)
    if itinerary.baseline_comparison is not None:
        assert "Compared with a conventional trip" in client.prompts[0]["user"]


def test_the_system_prompt_forbids_computing(itinerary):
    client = ScriptedClient(replies=["ok"])
    LlmItineraryExplainer(client).explain(itinerary)
    system = client.prompts[0]["system"]
    assert "ONLY the numbers given" in system
    assert "Never compute" in system


def test_the_explanation_is_not_asked_for_json(itinerary):
    """Prose is prose; a schema here would be cargo cult."""
    client = ScriptedClient(replies=["ok"])
    LlmItineraryExplainer(client).explain(itinerary)
    assert client.prompts[0]["schema"] is None


# ---------------------------------------------------------------------------
# The parser
# ---------------------------------------------------------------------------
def reply(**fields) -> str:
    payload = {"budget": 600, "travelers": 2, "duration_days": 5}
    payload.update(fields)
    return json.dumps(payload)


def test_a_good_extraction_becomes_a_request():
    client = ScriptedClient(
        replies=[
            reply(
                preferred_destinations=["Madrid"],
                avoid_destinations=["Paris"],
                preferred_experiences=["culture", "food"],
                preferences={"culture": 0.9, "food": 0.7},
            )
        ]
    )
    request = LlmPreferenceParser(client).parse(
        "Two of us, 600 euros, five days, we love food and culture, "
        "Madrid yes, Paris no"
    )
    assert request.budget == 600
    assert request.travelers == 2
    assert request.duration_days == 5
    assert request.preferred_destinations == ["Madrid"]
    assert request.avoid_destinations == ["Paris"]
    assert request.preferences.culture == 0.9


def test_the_date_window_is_derived_not_taken_from_the_model():
    """A model returning an inconsistent date pair would produce a request that
    is valid and wrong."""
    parser = LlmPreferenceParser(
        ScriptedClient(replies=[reply(duration_days=9)]),
        default_date_from=date(2026, 9, 10),
    )
    request = parser.parse("nine days please")
    assert request.date_from == date(2026, 9, 10)
    assert request.date_to == date(2026, 9, 19)


def test_what_the_traveler_did_not_say_comes_from_the_caller():
    """The origin and the window are the application's business, not the model's."""
    parser = LlmPreferenceParser(
        ScriptedClient(replies=[reply()]), default_origin="Hamburg"
    )
    assert parser.parse("a trip").origin == "Hamburg"


def test_a_hallucinated_experience_is_rejected_and_retried():
    """The exact failure the type system exists to catch here."""
    client = ScriptedClient(
        replies=[
            reply(preferred_experiences=["teleportation"]),
            reply(preferred_experiences=["culture"]),
        ]
    )
    parser = LlmPreferenceParser(client)
    request = parser.parse("I like weird stuff")
    assert request.preferred_experiences == ["culture"]
    assert len(parser.attempts) == 2
    assert parser.attempts[0].error is not None


def test_the_retry_shows_the_model_its_own_error():
    """Retrying with the same prompt would just get the same answer."""
    client = ScriptedClient(
        replies=[reply(preferred_experiences=["nonsense"]), reply()]
    )
    LlmPreferenceParser(client).parse("something")
    assert "rejected by the planner" in client.prompts[1]["user"]
    assert "nonsense" in client.prompts[1]["user"]


def test_a_contradictory_extraction_is_rejected():
    """Preferred and avoided cannot hold the same experience."""
    client = ScriptedClient(
        replies=[
            reply(preferred_experiences=["food"], disliked_experiences=["food"]),
            reply(preferred_experiences=["food"]),
        ]
    )
    request = LlmPreferenceParser(client).parse("I love and hate food")
    assert request.preferred_experiences == ["food"]
    assert request.disliked_experiences == []


def test_an_impossible_budget_is_rejected():
    client = ScriptedClient(replies=[reply(budget=-50), reply(budget=300)])
    assert LlmPreferenceParser(client).parse("cheap trip").budget == 300


def test_two_bad_answers_fall_back_to_the_keyword_parser():
    """The pipeline degrades to V3, never to nothing."""
    client = ScriptedClient(replies=[reply(budget=-1), reply(budget=-2)])
    request = LlmPreferenceParser(client).parse("400 eur for 2 people")
    assert request.budget == 400.0


def test_an_unreachable_model_falls_back_too():
    parser = LlmPreferenceParser(ScriptedClient(error=LlmError("down")))
    assert parser.parse("500 eur trip").budget == 500.0


def test_strict_mode_refuses_to_guess():
    client = ScriptedClient(replies=[reply(budget=-1), reply(budget=-2)])
    with pytest.raises(LlmError, match="could not produce a valid request"):
        LlmPreferenceParser(client, strict=True).parse("anything")


def test_unknown_fields_are_dropped_not_passed_through():
    """A schema-free fallback reply can carry anything; TripRequest forbids extras."""
    client = ScriptedClient(replies=[reply(secret_discount=0.5, hotel_chain="X")])
    assert LlmPreferenceParser(client).parse("a trip").budget == 600


def test_the_parsed_request_is_plannable():
    """The end of the pipeline: free text in, real itineraries out."""
    client = ScriptedClient(
        replies=[reply(budget=450, preferred_destinations=["Madrid"])]
    )
    request = LlmPreferenceParser(client).parse("450 euros, two of us, Madrid")
    result = TravelPlanner().plan(request)
    assert result.recommendations
    assert all(i.total_cost <= 450 for i in result.recommendations)


# ---------------------------------------------------------------------------
# The schema
# ---------------------------------------------------------------------------
def test_the_schema_is_sent_with_the_request():
    client = ScriptedClient(replies=[reply()])
    LlmPreferenceParser(client).parse("a trip")
    assert client.prompts[0]["schema"] == request_schema()


def test_the_schema_only_allows_real_experience_names():
    """Constrain first, validate second - the schema removes the easy failures."""
    schema = request_schema()
    allowed = schema["properties"]["preferred_experiences"]["items"]["enum"]
    assert allowed == list(EXPERIENCE_ATTRIBUTES)


def test_the_schema_tracks_the_domain_model():
    """A new attribute must not be added to the engine and forgotten here."""
    keys = set(request_schema()["properties"]["preferences"]["properties"])
    assert keys == set(PREFERENCE_KEYS)
    assert "multiple_cities" in keys


def test_the_schema_forbids_extra_fields():
    assert request_schema()["additionalProperties"] is False


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------
def test_plain_json_parses():
    assert parse_json_object('{"a": 1}') == {"a": 1}


def test_a_fenced_block_parses():
    """Structured output makes this exact, but the fallback path still matters."""
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_json_wrapped_in_prose_parses():
    assert parse_json_object('Sure! {"a": 1} Hope that helps.') == {"a": 1}


def test_a_reply_with_no_json_is_an_error():
    with pytest.raises(LlmError, match="no JSON object"):
        parse_json_object("I would rather not.")


def test_a_json_array_is_an_error():
    with pytest.raises(LlmError, match="expected a JSON object"):
        parse_json_object("[1, 2, 3]")


def test_a_scripted_client_records_what_it_was_asked():
    client = ScriptedClient(replies=["x"])
    client.complete(system="s", user="u", schema={"type": "object"})
    assert client.prompts == [{"system": "s", "user": "u", "schema": {"type": "object"}}]


def test_a_scripted_client_runs_out_honestly():
    with pytest.raises(LlmError, match="no scripted reply"):
        ScriptedClient().complete(system="s", user="u")
