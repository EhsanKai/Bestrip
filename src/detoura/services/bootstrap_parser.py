"""The parser contract for one acquisition task (V9 Phase 2.5 §21, §22).

A parser turns one fetched response (or, for a ``FILE_IMPORT``/
``MANUAL_DATASET`` source, a pre-supplied record) into a
:data:`~detoura.services.market_prior_source.RawPriorRecord` — the exact
shape the existing, unmodified Phase 2 import boundary
(:func:`detoura.services.market_prior_import.normalize`) already knows how to
validate and persist. This module does the acquisition-specific half of
validation: "does this response actually describe the task I asked for", not
"is this a well-formed BootstrapMarketPrior" (that is still
``market_prior_import``'s job).

**Never infer.** A parser reports only what the source actually supplied.
Baggage, fare rules, cabin, refundability and availability guarantees are
never invented (§21). A field the source did not give stays absent — the
Phase 2 import boundary already turns "absent" into ``None`` (UNKNOWN), never
``0``.

**Confidence, not just success/failure.** A response that parses but does not
look like the source's normal shape (missing the fields this parser expects
to always see) is a *schema-change* signal, not a best-effort partial import —
persisting a plausible-looking but structurally broken record would poison
the Market Prior with something worse than no data at all (§22).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

PARSER_VERSION = "v1"


class ParserError(Exception):
    """The response could not be turned into a usable record."""


class SchemaChanged(ParserError):
    """The response parsed, but not into the shape this parser version
    expects — the source most likely changed its page/response structure.
    Callers must treat this as a stop condition for the *source*, not just
    this one task (§20, §22)."""


@dataclass(frozen=True, slots=True)
class TaskContext:
    """What a parser is allowed to know about the task it is answering, so
    it can validate the response actually matches what was asked."""

    origin: str
    destination: str
    horizon_days: int


class BootstrapParser(Protocol):
    """Turns a fetched body into zero-or-one raw prior record.

    Returns ``None`` for "the source answered but has no fare for this
    market/horizon" (NO_DATA — a real, honest answer, not a failure).
    Raises :class:`SchemaChanged` when the response cannot be trusted at all.
    """

    version: str

    def parse(self, body: str, *, task: TaskContext) -> dict | None: ...


def validate_record_matches_task(record: dict, *, task: TaskContext) -> None:
    """Cross-check a parsed record against the task it claims to answer.
    Raises :class:`SchemaChanged` on a mismatch — this is exactly the
    "does the market in the response match the market I asked for" check
    from §22, kept separate from a single parser's own internal logic so
    every parser gets it for free."""
    o = str(record.get("origin_airport", "")).strip().upper()
    d = str(record.get("destination_airport", "")).strip().upper()
    if o and o != task.origin.upper():
        raise SchemaChanged(f"response origin {o!r} does not match requested {task.origin!r}")
    if d and d != task.destination.upper():
        raise SchemaChanged(f"response destination {d!r} does not match requested {task.destination!r}")
