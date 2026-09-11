"""Google-signed ID tokens for calling private Cloud Run services.

A Cloud Run service deployed with ``--no-allow-unauthenticated`` rejects any
request without an ``Authorization: Bearer <id-token>`` whose ``audience``
matches the callee's URL. Every gateway->agent and agent->MCP hop therefore
needs one; without it the call is 403 before it ever reaches the container.

Deliberately stdlib-only. The metadata server is a plain HTTP endpoint, so
this needs no ``google-auth`` dependency -- which matters because the gateway
ships five packages and the shared core ships none of Google's auth libraries.

Two behaviours make this safe to call unconditionally:

* Only ``https://*.run.app`` targets get a token. A loopback URL returns ``{}``,
  so local development is unchanged.
* A metadata-server failure returns ``{}`` rather than raising. Off-platform
  (a laptop, a test runner) there is no metadata server, and a hard failure
  there would break every local run to fix a cloud-only concern.

Verified against the real path on 2026-08-26: a Cloud Run job on the LQABR VPC
minted a token here (821 chars) and reached an ``ingress=internal`` service --
the request was accepted, not 403'd or 404'd.
"""

from __future__ import annotations

import time
import urllib.request
from typing import Dict, Tuple
from urllib.parse import quote, urlsplit

_METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/identity?audience="
)

#: Tokens are valid for ~1h. Re-minting per request would add a metadata
#: round-trip to every hop, so they are cached per audience and retired early.
_TOKEN_TTL_SECONDS = 2700
_REQUEST_TIMEOUT_SECONDS = 5

#: audience -> (token, expires_at_monotonic_wallclock)
_cache: Dict[str, Tuple[str, float]] = {}


def _audience(url: str) -> str:
    """The audience Cloud Run expects: scheme + host, no path, no query.

    Returns "" when the URL is not a Cloud Run target, which is the signal to
    attach nothing at all.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return ""
    if not parts.hostname.endswith(".run.app"):
        return ""
    return f"{parts.scheme}://{parts.hostname}"


def id_token(url: str) -> str:
    """A Google-signed ID token for ``url``, or "" if one does not apply."""
    audience = _audience(url)
    if not audience:
        return ""

    cached, expires_at = _cache.get(audience, ("", 0.0))
    if cached and time.time() < expires_at:
        return cached

    request = urllib.request.Request(
        _METADATA_IDENTITY_URL + quote(audience, safe=""),
        headers={"Metadata-Flavor": "Google"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            token = response.read().decode("utf-8").strip()
    except Exception:
        # No metadata server (local dev / CI), or it is briefly unavailable.
        # Returning "" lets the caller proceed unauthenticated: correct against
        # a loopback callee, and a clean 403 from a private one.
        return ""

    if not token:
        return ""
    _cache[audience] = (token, time.time() + _TOKEN_TTL_SECONDS)
    return token


def auth_header(url: str) -> Dict[str, str]:
    """``{"Authorization": "Bearer <id-token>"}`` for a private Cloud Run
    target, or ``{}`` for anything else.

    Safe to splat into a header dict unconditionally::

        headers = {"Content-Type": "application/json", **auth_header(url)}
    """
    token = id_token(url)
    return {"Authorization": f"Bearer {token}"} if token else {}


def clear_cache() -> None:
    """Drop cached tokens. Used by tests and after an identity change."""
    _cache.clear()
