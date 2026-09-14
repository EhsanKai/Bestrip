"""Trusted client-IP extraction for abuse controls (V9 Phase 6 slice 1).

``X-Forwarded-For`` is attacker-controlled input on any deployment that
does not sit behind a proxy the operator controls - trusting it blindly
turns a per-IP rate limiter into one an attacker bypasses just by sending a
different header value on every request. This module makes that trust
boundary explicit and fails safe: unless a specific number of trusted
proxy hops is configured, the header is ignored entirely and the actual
TCP peer address (``request.client.host``, set by the ASGI server from the
real socket, never attacker-suppliable) is used instead.

Production note (Render, this project's target host - see ``render.yaml``):
Render's edge terminates TLS and proxies to this container as the single
hop in front of it, appending the real client address as the last entry of
``X-Forwarded-For``. Set ``AUTH_TRUSTED_PROXY_HOPS=1`` there (or on any
single-reverse-proxy deployment) to recover the real per-client address for
rate limiting. Leave it at the default (``0``) on any deployment without a
reverse proxy Detoura controls - including local dev - since a wrong,
too-high value would trust an attacker-supplied entry instead of a proxy's
own.
"""

from __future__ import annotations

import ipaddress


def _parse_ip(raw: str) -> str | None:
    """A canonical address string, or ``None`` if ``raw`` is not a
    syntactically valid IPv4/IPv6 address. Never raises: every caller is on
    the unauthenticated login path and must fail safe, not fail loud, on
    attacker-controlled input."""
    candidate = raw.strip()
    if not candidate:
        return None
    # A literal IPv6 address in a header is conventionally bracketed
    # ("[::1]:8080", or bare "[::1]") - strip the brackets before parsing.
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


#: Returned when neither a trusted forwarded address nor a parseable direct
#: peer is available (e.g. a test client that sets no real socket peer).
#: Stable and non-``None`` on purpose - every caller uses this as a dict
#: key, and a real string groups "callers we genuinely couldn't identify"
#: into one bounded bucket rather than propagating ``None``.
UNKNOWN = "unknown"


def resolve_client_ip(
    *, direct_peer: str | None, forwarded_for: str | None, trusted_proxy_hops: int,
) -> str:
    """The address to key IP-based abuse controls on.

    ``direct_peer`` is the ASGI server's own view of the TCP peer
    (``request.client.host``) - never attacker-suppliable. ``forwarded_for``
    is the raw ``X-Forwarded-For`` header value, attacker-suppliable unless
    a trusted proxy overwrites/appends to it.

    With ``trusted_proxy_hops <= 0`` (the default), ``forwarded_for`` is
    ignored entirely and ``direct_peer`` is used - safe on any deployment,
    including one with no reverse proxy at all, at the cost of every
    request behind a shared proxy collapsing onto that proxy's one address
    (which only makes IP-based limiting coarser, never spoofable).

    With ``trusted_proxy_hops = N``, exactly the ``N``-th entry from the
    *right* of a well-formed ``X-Forwarded-For`` list is trusted - the
    entry appended by the outermost of the ``N`` proxies actually in front
    of this service, based on the socket *it* saw, which an attacker's own
    forged prefix entries cannot reach (a client that sends its own fake
    ``X-Forwarded-For`` only pushes its forgery further left; the trusted
    proxy still appends the real address on the right). Anything short of
    that - fewer entries than hops, or an unparseable entry - falls back to
    ``direct_peer`` rather than trusting a guess.
    """
    if trusted_proxy_hops > 0 and forwarded_for:
        parts = forwarded_for.split(",")
        if len(parts) >= trusted_proxy_hops:
            candidate = parts[-trusted_proxy_hops]
            parsed = _parse_ip(candidate)
            if parsed is not None:
                return parsed
    return _parse_ip(direct_peer or "") or UNKNOWN
