"""The Claude client behind both LLM seams (V4).

V3 defined ``PreferenceParser`` and ``ItineraryExplainer`` and shipped
dependency-free stand-ins, so the pipeline ran end to end without an API. This
adds the real implementations behind a protocol thin enough that the stand-ins
remain the fallback rather than the design.

**The optimizer still never sees this.** A test walks the AST of
``algorithms/``, ``constraints/``, ``services/``, ``providers/`` and ``data/``
and fails if any of them imports ``detoura.llm``. Prices, availability,
travel time, feasibility and scores are computed; nothing here can change one.

**The SDK is imported lazily.** ``anthropic`` is an optional extra, so the
package imports and the whole test suite runs without it. Asking for a Claude
client without the SDK installed raises with the install command, rather than
failing somewhere less obvious later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

#: The model these seams are written against.
DEFAULT_MODEL = "claude-opus-5"

#: Explaining an itinerary and parsing one sentence are both small, bounded
#: jobs. Low effort is the right default: it is faster and cheaper, and the
#: hard thinking already happened in the optimizer.
DEFAULT_EFFORT = "low"

DEFAULT_MAX_TOKENS = 2048


class LlmError(RuntimeError):
    """The model could not be reached, or answered unusably."""


@runtime_checkable
class LlmClient(Protocol):
    """One completion, optionally constrained to a JSON schema.

    Deliberately smaller than the Messages API: these seams need one turn with
    a system prompt and a user message. A narrow protocol is what lets the
    tests drive both seams with a scripted client and no network.
    """

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str: ...


class AnthropicClient:
    """:class:`LlmClient` backed by the Anthropic SDK.

    Credentials come from the environment the way the SDK already resolves
    them (``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN``, or a logged-in
    profile), so nothing here handles a secret directly.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
        client: Any | None = None,
        max_retries: int = 2,
        timeout: float = 30.0,
    ) -> None:
        self.model = model
        self.effort = effort
        self.max_retries = max_retries
        self.timeout = timeout
        self._client = client

    @property
    def client(self) -> Any:
        """The SDK client, constructed on first use."""
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on env
                raise LlmError(
                    "the anthropic SDK is not installed; "
                    'run `pip install "travel-planner[llm]"` or pass a client'
                ) from exc
            self._client = anthropic.Anthropic(
                max_retries=self.max_retries, timeout=self.timeout
            )
        return self._client

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": self.effort},
        }
        if schema is not None:
            # Structured output: the response is guaranteed to parse, which
            # removes an entire class of "the model added a preamble" bugs.
            request["output_config"]["format"] = {
                "type": "json_schema",
                "schema": schema,
            }
        try:
            response = self.client.messages.create(**request)
        except Exception as exc:  # noqa: BLE001 - any SDK failure is one failure here
            raise LlmError(f"the model could not be reached: {exc}") from exc

        if getattr(response, "stop_reason", None) == "refusal":
            raise LlmError("the model declined to answer")
        text = "".join(
            block.text
            for block in getattr(response, "content", [])
            if getattr(block, "type", None) == "text"
        )
        if not text.strip():
            raise LlmError("the model returned no text")
        return text


@dataclass
class ScriptedClient:
    """A deterministic :class:`LlmClient` for tests and offline demos.

    Returns queued replies in order and records every prompt, so a test can
    assert what the model was actually asked - which is where the guardrails
    on these seams live.
    """

    replies: list[str] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    error: Exception | None = None

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: dict[str, Any] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> str:
        self.prompts.append({"system": system, "user": user, "schema": schema})
        if self.error is not None:
            raise self.error
        if not self.replies:
            raise LlmError("no scripted reply left")
        return self.replies.pop(0)


def parse_json_object(text: str) -> dict:
    """Parse a JSON object out of a model reply.

    Structured output makes this exact, but the fallback path matters: a model
    asked for JSON without a schema will sometimes wrap it in prose or a fenced
    block, and failing on that would be a self-inflicted outage.
    """
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("```")[1]
        if candidate.startswith("json"):
            candidate = candidate[4:]
        candidate = candidate.strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise LlmError(f"no JSON object in the reply: {text[:200]!r}") from None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LlmError(f"malformed JSON in the reply: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LlmError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed
