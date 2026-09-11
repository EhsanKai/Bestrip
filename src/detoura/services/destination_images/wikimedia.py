"""Wikimedia Commons adapter (V9 Phase 2.6 §B3, §B7).

Commons is the first-choice source in the license-priority order: every file
carries structured, machine-readable license/creator metadata via its public
``action=query`` API, which Wikimedia explicitly publishes for this kind of
programmatic use (see ``https://commons.wikimedia.org/wiki/Commons:API``) —
this is API access to a documented endpoint, not scraping the wiki's HTML,
and nothing here touches an authenticated or undocumented endpoint.

Two domains, allowlisted separately and explicitly — never widened to "any
wikimedia.org host":

* ``commons.wikimedia.org`` — the search/metadata API.
* ``upload.wikimedia.org`` — the actual media CDN a file's ``url`` points at.

Politeness over throughput: a descriptive ``User-Agent`` (Wikimedia's own
robot policy asks for one identifying the tool and a contact point) and a
deliberate pause between requests, reusing the existing V4 HTTP plumbing
(:class:`~detoura.providers.http.RetryingHttpClient`,
:class:`~detoura.providers.http.RateLimiter`) rather than a bespoke client.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ...models.destination_image import ACCEPTED_LICENSE_PREFIXES, license_is_accepted
from ...providers.http import (
    DEFAULT_TIMEOUT_SECONDS,
    HttpClient,
    ProviderHttpError,
    RateLimiter,
    RetryingHttpClient,
    UrllibHttpClient,
    _build_ssl_context,
)

__all__ = [
    "ACCEPTED_LICENSE_PREFIXES",
    "license_is_accepted",
]

API_DOMAIN = "commons.wikimedia.org"
#: Two explicit, distinct media domains - the raw-original CDN and the
#: thumbnail-rendering service. Both allowlisted by name; never widened to a
#: "*.wikimedia.org" wildcard.
MEDIA_DOMAINS = ("upload.wikimedia.org", "thumb.wikimedia.org")
USER_AGENT = "DetouraDestinationImageBot/1.0 (product image acquisition; see docs/V9_PHASE2_6_DESTINATION_IMAGES.md)"

# NOTE: ACCEPTED_LICENSE_PREFIXES / license_is_accepted are re-exported from
# models.destination_image (the domain layer) rather than defined here, so
# the same allowlist is enforced both at acquisition time (this module) and
# after the fact (DestinationImage.production_eligible,
# quality_checks.audit_manifest) — found missing by independent adversarial
# QA, which showed a fabricated license string on a hand-edited manifest
# record was previously accepted as "understood" with no re-validation.

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(value: str | None) -> str | None:
    if not value:
        return None
    text = _TAG_RE.sub("", value).strip()
    text = re.sub(r"\s+", " ", text)
    return text or None


class DomainNotAllowed(RuntimeError):
    pass


def _check_domain(url: str, allowed: str | tuple[str, ...]) -> None:
    allowed_domains = (allowed,) if isinstance(allowed, str) else allowed
    host = (urlparse(url).hostname or "").lower()
    if not any(host == a or host.endswith("." + a) for a in allowed_domains):
        raise DomainNotAllowed(f"{host} is not an allowed domain {allowed_domains!r}")
    if urlparse(url).scheme != "https":
        raise DomainNotAllowed("non-https URL")


@dataclass(frozen=True, slots=True)
class Candidate:
    title: str
    page_id: int
    source_page_url: str
    original_url: str
    thumb_url: str | None
    """The size-bounded thumbnail Commons rendered for us
    (``iiurlwidth``) — this, not ``original_url``, is what
    :meth:`WikimediaCommonsClient.download` fetches. Commons' own media CDN
    rate-limits (HTTP 429) repeated full-resolution ``original_url``
    fetches and its error message explicitly asks automated tools to use
    thumbnails instead — this project follows that guidance rather than
    finding a way around it (§B3 "do not bypass rate limits")."""
    mime: str | None
    width: int | None
    height: int | None
    license_short_name: str | None
    license_url: str | None
    attribution_required: bool | None
    creator: str | None
    credit: str | None

    @property
    def download_url(self) -> str:
        return self.thumb_url or self.original_url


@dataclass
class WikimediaCommonsClient:
    """Thin, allowlisted wrapper over the Commons ``action=query`` API."""

    http_client: HttpClient = field(default_factory=UrllibHttpClient)
    min_request_interval_seconds: float = 1.0
    requests_made: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._rate_limiter = RateLimiter(min_interval_seconds=self.min_request_interval_seconds)
        self._client = RetryingHttpClient(self.http_client, rate_limiter=self._rate_limiter)

    def search_candidates(self, query: str, *, limit: int = 8, thumb_width: int = 1600) -> list[Candidate]:
        url = f"https://{API_DOMAIN}/w/api.php"
        _check_domain(url, API_DOMAIN)
        params = {
            "action": "query", "format": "json",
            "generator": "search", "gsrsearch": query, "gsrnamespace": "6",
            "gsrlimit": str(limit),
            "prop": "imageinfo",
            "iiprop": "url|size|extmetadata|mime",
            "iiurlwidth": str(thumb_width),
        }
        try:
            response = self._client.request(
                "GET", url, params=params, headers={"User-Agent": USER_AGENT},
                timeout=DEFAULT_TIMEOUT_SECONDS,
            )
        except ProviderHttpError:
            return []
        self.requests_made += 1
        if not response.ok:
            return []
        try:
            doc = json.loads(response.body)
        except (ValueError, TypeError):
            return []
        pages = (doc.get("query") or {}).get("pages") or {}
        out: list[Candidate] = []
        for page in pages.values():
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            info = infos[0]
            meta = info.get("extmetadata") or {}

            def _m(key: str) -> str | None:
                return (meta.get(key) or {}).get("value")

            attribution_required = _m("AttributionRequired")
            out.append(Candidate(
                title=page.get("title", ""), page_id=page.get("pageid", 0),
                source_page_url=info.get("descriptionurl", ""),
                original_url=info.get("url", ""),
                thumb_url=info.get("thumburl"),
                mime=info.get("mime"),
                width=info.get("width"), height=info.get("height"),
                license_short_name=_m("LicenseShortName"),
                license_url=_m("LicenseUrl"),
                attribution_required=(
                    None if attribution_required is None
                    else str(attribution_required).strip().lower() == "true"
                ),
                creator=strip_html(_m("Artist")),
                credit=strip_html(_m("Credit")),
            ))
        return out

    def download(self, url: str, *, max_bytes: int) -> bytes:
        """Raises :class:`DomainNotAllowed` for anything off the media CDN,
        and :class:`ProviderHttpError`-style failures propagate for the
        caller to treat as a rejected candidate — never a silent empty
        result (§B10 "corrupt download", §B7).

        Binary, not text: :class:`~detoura.providers.http.HttpClient` decodes
        every response as UTF-8 text (correct for the JSON API calls above,
        which is all V4's HTTP plumbing was ever asked to carry) — an image
        download needs the raw bytes, so this goes around that abstraction
        deliberately rather than corrupting a binary body through a text
        decode."""
        _check_domain(url, MEDIA_DOMAINS)
        self._rate_limiter.acquire()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(
                request, timeout=DEFAULT_TIMEOUT_SECONDS, context=_build_ssl_context(),
            ) as response:
                self.requests_made += 1
                if response.status != 200:
                    raise ProviderHttpError(f"download failed with status {response.status}",
                                            status=response.status)
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise ProviderHttpError(f"response body exceeds {max_bytes} bytes")
                return body
        except urllib.error.HTTPError as exc:
            self.requests_made += 1
            raise ProviderHttpError(f"download failed with status {exc.code}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise ProviderHttpError(f"could not reach {url}: {exc.reason}") from exc
