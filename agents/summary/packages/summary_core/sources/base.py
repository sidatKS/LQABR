"""The adapter contract, the fetch guards, and the retry policy.

Every adapter is a callable `(spec, settings) -> NormalizedDocument`
registered under its kind. The summariser depends on the CONTRACT, never on
a particular adapter, which is what makes a fifth input kind a new file
instead of a change to the agent.

Two things live here because every fetching adapter needs them and no
adapter should reimplement them:

* `guard_url` — an agent that fetches a URL a caller chose is an SSRF hole
  unless something checks first. Scheme allowlist, then DNS resolution, then
  a refusal of loopback / private / link-local / reserved addresses. The
  link-local check is the one that matters on GCP: 169.254.169.254 is the
  metadata server, and it hands out service-account tokens.
* `request` — the house retry contract: 3 tries, exponential backoff on
  429 and 5xx, every attempt on the audit stream.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

import requests

from ..obs import Observability, get_obs
from ..settings import Settings, get_settings
from ..types import NormalizedDocument, SourceError, SourceSpec

#: kind -> adapter. Populated by each adapter module at import time.
_REGISTRY: Dict[str, Callable[..., NormalizedDocument]] = {}

_RETRYABLE_STATUS = (429, 500, 502, 503, 504)

#: `a.b[0].c` -> [("a",""), ("b",""), ("","0"), ("c","")]
_SELECT_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def register(kind: str, adapter: Callable[..., NormalizedDocument]) -> None:
    _REGISTRY[kind] = adapter


def available_kinds() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def get_adapter(kind: str) -> Callable[..., NormalizedDocument]:
    try:
        return _REGISTRY[kind]
    except KeyError:
        raise SourceError(
            f"no adapter registered for source kind {kind!r} "
            f"(have: {', '.join(available_kinds()) or 'none'})"
        ) from None


def fetch(spec: SourceSpec, settings: Settings | None = None,
          *, session: Optional[requests.Session] = None,
          obs: Observability | None = None) -> NormalizedDocument:
    """Normalise any SourceSpec. The one entry point the agent's tools call."""
    settings = settings or get_settings()
    obs = obs or get_obs()
    adapter = get_adapter(spec.kind)
    obs.process.emit("source_fetch_start", kind=spec.kind, source_ref=spec.reference)
    document = adapter(spec, settings, session=session, obs=obs)
    if document.is_empty:
        raise SourceError(
            f"{spec.reference}: the source produced no readable text — "
            "nothing was summarised rather than summarising an empty page"
        )
    obs.process.emit(
        "source_fetch_complete", kind=spec.kind, source_ref=spec.reference,
        chars=document.char_count, truncated=document.truncated, title=document.title,
    )
    return document


# ---------------------------------------------------------------- guards
def guard_url(url: str, settings: Settings) -> str:
    """Refuse anything we should not be fetching. Returns the URL unchanged."""
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise SourceError(
            f"refused {url!r}: only http and https are fetchable "
            f"(got scheme {parsed.scheme or 'none'!r})"
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise SourceError(f"refused {url!r}: no host in the URL")

    if settings.allowed_hosts:
        allowed = any(host == entry or host.endswith("." + entry)
                      for entry in settings.allowed_hosts)
        if not allowed:
            raise SourceError(
                f"refused {host!r}: not in LQABR_SUMMARY_ALLOWED_HOSTS"
            )
        return url

    if settings.allow_private_hosts:
        return url

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise SourceError(f"refused {host!r}: DNS lookup failed ({exc})") from exc

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (address.is_private or address.is_loopback or address.is_link_local
                or address.is_reserved or address.is_multicast or address.is_unspecified):
            raise SourceError(
                f"refused {host!r}: resolves to the non-public address {address}. "
                "Set LQABR_SUMMARY_ALLOWED_HOSTS (or ALLOW_PRIVATE_HOSTS=1 for local dev) "
                "if this is deliberate."
            )
    return url


# ---------------------------------------------------------------- transport
def request(method: str, url: str, *, settings: Settings,
            session: Optional[requests.Session] = None,
            obs: Observability | None = None,
            headers: Optional[Dict[str, str]] = None,
            json_body: Any = None) -> requests.Response:
    """One guarded, audited, retrying HTTP call."""
    obs = obs or get_obs()
    guard_url(url, settings)
    session = session or requests.Session()
    sent = {"User-Agent": settings.user_agent, "Accept": "*/*"}
    sent.update(headers or {})

    last_error = ""
    for attempt in range(1, max(1, settings.max_retries) + 1):
        started = time.monotonic()
        try:
            response = session.request(
                method.upper(), url, headers=sent, json=json_body,
                timeout=settings.http_timeout_seconds,
            )
        except requests.RequestException as exc:
            last_error = str(exc)
            obs.hop(service="source", endpoint=url, method=method.upper(),
                    attempt=attempt, error=last_error,
                    duration_ms=round((time.monotonic() - started) * 1000, 1))
            if attempt >= settings.max_retries:
                raise SourceError(f"unable to fetch {url}: {exc}") from exc
            time.sleep(min(2 ** (attempt - 1), 8))
            continue

        duration = round((time.monotonic() - started) * 1000, 1)
        obs.hop(service="source", endpoint=url, method=method.upper(),
                status=response.status_code, attempt=attempt, duration_ms=duration)

        if response.status_code in _RETRYABLE_STATUS and attempt < settings.max_retries:
            time.sleep(min(2 ** (attempt - 1), 8))
            continue
        if response.status_code >= 400:
            raise SourceError(
                f"unable to fetch {url}: HTTP {response.status_code}"
            )
        return response

    raise SourceError(f"unable to fetch {url}: {last_error or 'retries exhausted'}")


# ---------------------------------------------------------------- helpers
def truncate(text: str, settings: Settings) -> tuple[str, bool]:
    """Protect the model from a very large page. Reports whether it cut."""
    limit = max(1, settings.max_chars)
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def select_path(data: Any, expression: Optional[str]) -> Any:
    """A deliberately small selector: `$.a.b[0].c`, or `a.b` without the `$.`.

    Enough to pull the field that actually holds the content out of an API
    envelope, and small enough that it needs no dependency and cannot execute
    anything. A path that does not exist is a NAMED error, not a silent None —
    a caller who selected the wrong field should hear about it.
    """
    if not expression:
        return data
    cursor = data
    trail: list[str] = []
    for key, index in _SELECT_TOKEN.findall(expression.lstrip("$")):
        if key:
            trail.append(key)
            if not isinstance(cursor, dict) or key not in cursor:
                raise SourceError(
                    f"select {expression!r}: no key {'.'.join(trail)!r} in the payload"
                )
            cursor = cursor[key]
        else:
            trail.append(f"[{index}]")
            try:
                cursor = cursor[int(index)]
            except (TypeError, KeyError, IndexError) as exc:
                raise SourceError(
                    f"select {expression!r}: {''.join(trail)} is not reachable ({exc})"
                ) from exc
    return cursor
