"""The credential the MCP demands, minted by the workload itself.

`lqabr-dev-mcp` is deployed with **Require authentication (IAM)**. Every caller
must present a Google-signed OIDC ID token whose `aud` is the MCP service URL;
an anonymous request is refused by Cloud Run's front door with **HTTP 403** in
~100ms, before the MCP container sees it. That failure reads like a broken URL
and is not one — it is a missing header.

On Cloud Run the token costs nothing to obtain: the instance metadata server
issues one for the runtime service account, for any audience we ask for. So the
image carries no secret, `.env` gains nothing, and a rotation is somebody else's
problem. Off Cloud Run (a laptop, the test suite) there is no metadata server;
`token()` returns "" and the client sends no header, which is exactly right for
a local MCP that wants none.

NEVER log the value. `expires_in` and the audience are the useful facts and are
the only ones emitted.
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Optional
from urllib.parse import urlsplit

import requests

#: The instance metadata server. `default` is the runtime service account.
IDENTITY_URL = ("http://metadata.google.internal/computeMetadata/v1/"
                "instance/service-accounts/default/identity")
_METADATA_HEADERS = {"Metadata-Flavor": "Google"}

#: Refresh this long before `exp`, so a token never expires mid-flight.
_SKEW_SECONDS = 300
#: Used only when `exp` cannot be read out of the JWT.
_FALLBACK_TTL_SECONDS = 2700
#: After a failed mint, wait this long before trying again. Off Cloud Run the
#: metadata host does not resolve, and every MCP call must not pay that wait.
_RETRY_AFTER_SECONDS = 60


def audience_for(base_url: str) -> str:
    """The Cloud Run audience for an MCP base URL: scheme://host, no path.

    Cloud Run issues and validates against the SERVICE url. `mcp_base_url`
    carries the `/mcp` path, and an audience that includes the path does not
    match — so the path is dropped here rather than at each call site.
    """
    parts = urlsplit((base_url or "").strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def _expiry_from(token: str) -> float:
    """`exp` out of the JWT payload. 0 when it cannot be read.

    The signature is Google's business, not ours — we are not validating the
    token, only deciding when to ask for the next one.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return float(claims.get("exp") or 0)
    except Exception:  # noqa: BLE001 - a token we cannot read still works
        return 0.0


class IdentityTokenSource:
    """One cached ID token for one audience.

    Not thread-safe by design: a duplicate mint is cheap and harmless, while a
    lock around a network call on every MCP request is not.
    """

    def __init__(self, audience: str, *,
                 session: Optional[requests.Session] = None,
                 timeout_seconds: float = 5.0,
                 obs: Any = None) -> None:
        self._audience = audience or ""
        self._session = session or requests
        self._timeout = timeout_seconds
        self._obs = obs
        self._token: str = ""
        self._expires_at: float = 0.0
        self._retry_at: float = 0.0

    @property
    def audience(self) -> str:
        return self._audience

    def _emit(self, event: str, **fields: Any) -> None:
        if self._obs is None:
            return
        try:
            self._obs.system.emit(event, audience=self._audience, **fields)
        except Exception:  # noqa: BLE001 - logging never breaks a request
            pass

    def token(self) -> str:
        """A valid ID token, or "" when this workload cannot mint one."""
        if not self._audience:
            return ""
        now = time.time()
        if self._token and now < self._expires_at:
            return self._token
        if now < self._retry_at:
            return ""
        try:
            response = self._session.get(
                IDENTITY_URL, params={"audience": self._audience},
                headers=_METADATA_HEADERS, timeout=self._timeout,
            )
            status = getattr(response, "status_code", 0)
            token = (getattr(response, "text", "") or "").strip()
            if status != 200 or not token:
                raise RuntimeError(f"metadata server answered HTTP {status}")
        except Exception as exc:  # noqa: BLE001 - absence is a normal state
            self._retry_at = now + _RETRY_AFTER_SECONDS
            self._emit("mcp_identity_unavailable", reason=str(exc)[:200],
                       retry_in_s=_RETRY_AFTER_SECONDS)
            return ""
        expiry = _expiry_from(token)
        self._expires_at = (expiry - _SKEW_SECONDS) if expiry else (now + _FALLBACK_TTL_SECONDS)
        self._token = token
        self._retry_at = 0.0
        self._emit("mcp_identity_minted",
                   expires_in_s=int(max(0.0, self._expires_at - now)),
                   exp_known=bool(expiry))
        return self._token
