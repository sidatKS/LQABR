"""STEP A — M2M auth utility: ``get_hubspot_token()``.

Contract (FR Step A / context §7.6): provide a valid HubSpot access token to
ANY agent, generated machine-to-machine at runtime. Called BEFORE every HubSpot
call (Steps 5 & 6); the email / voice / scheduler agents call it first too.
The token is attached as ``Authorization: Bearer <token>``.

--------------------------------------------------------------------------
AUTH MODE — read this before changing anything
--------------------------------------------------------------------------
HubSpot offers three grants. Only two of them can touch CRM objects:

  private_app     Static, long-lived token. Works for CRM. This is what the
                  verified 263-lead run used. It does NOT satisfy the M2M
                  requirement Mahi raised on 29 Jul — the token is static.
                  >>> INTERIM MODE, in use today by explicit decision. <<<

  client_credentials  Authenticates as the app, but HubSpot scopes it to
                  webhooks-journal operations only. It CANNOT mint a token for
                  crm.objects.contacts.write / companies.write, and issues no
                  refresh token. Unusable here — ruled out, do not revisit.

  refresh_token   TARGET MODE. Exchanges client_id + client_secret +
                  refresh_token for a fresh ~30 minute access token at runtime.
                  Nothing static is stored, the token is short-lived and
                  auto-refreshed. This is the flow that meets the requirement.
                  The one-time authorization-code handshake that produces the
                  refresh token happens out-of-band, once, outside this agent.

Both usable modes sit behind one ``TokenProvider`` interface, so flipping
HUBSPOT_AUTH_MODE from ``private_app`` to ``refresh_token`` is a config change:
no call site in crm.py or in any other agent changes.

Credentials come from .env locally and Secret Manager in prod. In
``refresh_token`` mode the access token itself is NEVER stored — only the
credentials used to mint it.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

import requests

from lqabr_core.obs import get_obs
from lqabr_core.leadgen.secrets import SecretAccessError, SecretConfigError, get_secret

DEFAULT_TOKEN_URL = "https://api.hubapi.com/oauth/v1/token"
DEFAULT_REFRESH_SKEW_SECONDS = 120


class AuthConfigError(RuntimeError):
    """Auth is misconfigured — a structural failure that halts the run."""


def _secret(name: str) -> str | None:
    """Resolve a credential from Secret Manager, mapping failures to auth errors.

    A credential that cannot be resolved is systemic: it fails identically for
    every lead, so it must halt the run rather than be recorded per record
    (review finding B1).
    """
    try:
        return get_secret(name)
    except (SecretConfigError, SecretAccessError) as exc:
        raise AuthConfigError(f"{type(exc).__name__}: {exc}") from exc


class TokenError(RuntimeError):
    """The token endpoint refused or failed."""


@dataclass
class AccessToken:
    value: str
    expires_at: float | None  # epoch seconds; None == does not expire
    mode: str

    def is_fresh(self, skew_seconds: int) -> bool:
        if self.expires_at is None:
            return True
        return time.time() < (self.expires_at - skew_seconds)

    @property
    def expires_in(self) -> float | None:
        if self.expires_at is None:
            return None
        return round(self.expires_at - time.time(), 1)


class TokenProvider(Protocol):
    mode: str

    def fetch(self) -> AccessToken: ...


class PrivateAppTokenProvider:
    """INTERIM. Static long-lived private-app token from the environment.

    Wrapped in the same interface so that every caller already goes through
    get_hubspot_token(); swapping in the refresh flow later touches this file
    only. Emits a process-log warning on every acquisition so the deviation
    from the M2M requirement is visible in the run log, not just in a doc.
    """

    mode = "private_app"

    def __init__(self, token: str | None = None):
        self._token = token

    def fetch(self) -> AccessToken:
        # Resolved at RUNTIME from Secret Manager, not read from the environment.
        if not self._token:
            self._token = _secret("HUBSPOT_PRIVATE_APP_TOKEN")
        if not self._token:
            raise AuthConfigError(
                "HUBSPOT_AUTH_MODE=private_app but the private-app token could "
                "not be resolved (see LQABR_SECRET_HUBSPOT_PRIVATE_APP_TOKEN)"
            )
        get_obs().process.emit(
            "auth_mode_interim",
            mode=self.mode,
            note=(
                "static private-app token in use; does not satisfy the M2M requirement "
                "(context §8 open item) — switch HUBSPOT_AUTH_MODE=refresh_token when "
                "the refresh token is provisioned"
            ),
        )
        # A private-app token has no expiry, so there is nothing to refresh.
        return AccessToken(value=self._token, expires_at=None, mode=self.mode)


class RefreshTokenProvider:
    """TARGET. Mint a short-lived access token from stored credentials.

    POST {token_url}
        grant_type=refresh_token
        client_id, client_secret, refresh_token
    -> {"access_token": ..., "expires_in": 1800, "refresh_token": ...}

    HubSpot access tokens live ~30 minutes. The refresh token is long-lived and
    is the only long-lived secret stored; it lives in Secret Manager, never in
    the image and never in git.
    """

    mode = "refresh_token"

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        token_url: str | None = None,
        timeout: float | None = None,
        session: Any = None,
    ):
        # Deliberately NOT read from the environment: resolved from Secret
        # Manager on first fetch (context §7.6). Only the non-secret endpoint
        # and timeout come from env.
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._token_url = token_url or os.getenv("HUBSPOT_TOKEN_URL", DEFAULT_TOKEN_URL)
        self._timeout = timeout or float(os.getenv("HUBSPOT_HTTP_TIMEOUT_SECONDS", "30"))
        self._session = session or requests

    def fetch(self) -> AccessToken:
        self._client_id = self._client_id or _secret("HUBSPOT_CLIENT_ID")
        self._client_secret = self._client_secret or _secret("HUBSPOT_CLIENT_SECRET")
        self._refresh_token = self._refresh_token or _secret("HUBSPOT_REFRESH_TOKEN")

        missing = [
            name
            for name, value in (
                ("HUBSPOT_CLIENT_ID", self._client_id),
                ("HUBSPOT_CLIENT_SECRET", self._client_secret),
                ("HUBSPOT_REFRESH_TOKEN", self._refresh_token),
            )
            if not value
        ]
        if missing:
            raise AuthConfigError(
                "HUBSPOT_AUTH_MODE=refresh_token but these could not be resolved "
                "from Secret Manager: " + ", ".join(missing)
            )

        obs = get_obs()
        payload = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": self._refresh_token,
        }

        # audit: every external call — endpoint, status, timing. Never the secret.
        with obs.timed_audit("hubspot_token_request", endpoint=self._token_url, method="POST") as t:
            response = self._session.post(
                self._token_url,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=self._timeout,
            )
            t.extra["status"] = getattr(response, "status_code", None)

        if response.status_code != 200:
            raise TokenError(
                f"HubSpot token endpoint returned {response.status_code}: {_safe_body(response)}"
            )

        body = response.json()
        access_token = body.get("access_token")
        if not access_token:
            raise TokenError("HubSpot token response contained no access_token")

        expires_in = float(body.get("expires_in", 1800))
        return AccessToken(
            value=access_token,
            expires_at=time.time() + expires_in,
            mode=self.mode,
        )


def _safe_body(response: Any) -> str:
    try:
        return str(response.text)[:500]
    except Exception:  # pragma: no cover
        return "<unreadable>"


def build_provider(mode: str | None = None) -> TokenProvider:
    """Resolve the auth mode. NO DEFAULT — review finding B11.

    This used to default to ``private_app``. That meant a prod deploy which
    simply forgot HUBSPOT_AUTH_MODE silently selected the static-token path
    that explicitly does not meet the M2M requirement. Failing closed makes the
    interim mode a decision someone has to type, not an accident.
    """
    mode = (mode or os.getenv("HUBSPOT_AUTH_MODE", "")).strip().lower()
    if not mode:
        raise AuthConfigError(
            "HUBSPOT_AUTH_MODE is not set. Set it explicitly to 'refresh_token' "
            "(the target M2M flow) or 'private_app' (the interim static token, "
            "which does NOT satisfy the M2M requirement — see docs/AUTH.md). "
            "There is deliberately no default."
        )
    if mode == "private_app":
        return PrivateAppTokenProvider()
    if mode == "refresh_token":
        return RefreshTokenProvider()
    if mode == "client_credentials":
        raise AuthConfigError(
            "client_credentials is not usable: HubSpot scopes that grant to "
            "webhooks-journal operations only and it cannot mint CRM-scoped tokens. "
            "Use refresh_token."
        )
    raise AuthConfigError(f"unknown HUBSPOT_AUTH_MODE: {mode!r}")


class TokenCache:
    """In-process cache with auto-refresh on/near expiry.

    One cache per process; the MCP is in-process, so every agent sharing the
    process shares one token rather than minting one per call.
    """

    def __init__(self, provider: TokenProvider | None = None, skew_seconds: int | None = None):
        self._provider = provider
        self._skew = (
            skew_seconds
            if skew_seconds is not None
            else int(os.getenv("HUBSPOT_TOKEN_REFRESH_SKEW_SECONDS", DEFAULT_REFRESH_SKEW_SECONDS))
        )
        self._token: AccessToken | None = None
        self._hits = 0
        self._lock = threading.Lock()

    @property
    def provider(self) -> TokenProvider:
        if self._provider is None:
            self._provider = build_provider()
        return self._provider

    def get(self, force_refresh: bool = False) -> AccessToken:
        obs = get_obs()
        with self._lock:
            cached = self._token
            if not force_refresh and cached is not None and cached.is_fresh(self._skew):
                # B19: deliberately NOT logged. get_auth_header() runs before
                # every HubSpot call, so a per-hit line is ~1,300 lines of noise
                # per run and buries the mints and refreshes that matter.
                self._hits += 1
                return cached

            reason = "forced" if force_refresh else ("expiring" if cached else "cold")
            token = self.provider.fetch()
            self._token = token
            obs.process.emit(
                "hubspot_token_acquired" if reason == "cold" else "hubspot_token_refreshed",
                mode=token.mode,
                reason=reason,
                expires_in=token.expires_in,
            )
            obs.system.emit(
                "hubspot_token_cache_updated",
                mode=token.mode,
                served_from_cache_since_last_mint=self._hits,
            )
            self._hits = 0
            return token

    def clear(self) -> None:
        with self._lock:
            self._token = None


_CACHE = TokenCache()


def get_hubspot_token(force_refresh: bool = False) -> str:
    """Return a valid HubSpot access token. Call before every HubSpot request.

    This is the single entry point every agent uses — lead_profile today,
    email / voice / scheduler tomorrow.
    """
    return _CACHE.get(force_refresh=force_refresh).value


def get_auth_header(force_refresh: bool = False) -> dict[str, str]:
    """The headers for a HubSpot call: fresh Bearer token + JSON content type."""
    return {
        "Authorization": f"Bearer {get_hubspot_token(force_refresh=force_refresh)}",
        "Content-Type": "application/json",
    }


def reset_token_cache(provider: TokenProvider | None = None, skew_seconds: int | None = None) -> TokenCache:
    """Reset the module-level cache. Used by tests and by credential rotation."""
    global _CACHE
    _CACHE = TokenCache(provider=provider, skew_seconds=skew_seconds)
    return _CACHE
