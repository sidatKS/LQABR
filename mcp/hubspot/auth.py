"""STEP 4 — acquire the HubSpot access token. NEW IN V2.

*Machine-to-machine · ahead of every HubSpot call.*

Trigger: the correlation token has been bound and a HubSpot call is about
to be made. End goal: hold a short-lived machine-to-machine token for the
life of the run — **no hard-coded token anywhere in the design**. Produces:
a bearer, cached for the run and refreshed on expiry, sent as a header on
every HubSpot hop.

This module is owned by the lead profile agent's author and shared by every
agent that touches HubSpot; the email agent only consumes it.

Two backends, selected by ``LQABR_HUBSPOT_AUTH_MODE``:

``secret_manager`` (default)
    The credential already provisioned for this project: the HubSpot
    private-app token held in Google Secret Manager as
    ``lqabr-hubspot-access-token`` and fetched through
    ``lqabr_core.secrets``. Nothing is hard-coded and nothing is read from
    a checked-in file — the process authenticates to Secret Manager with
    its own workload identity and is handed the credential at runtime,
    which is the machine-to-machine property the step is after. Cached for
    ``LQABR_HUBSPOT_TOKEN_TTL_SECONDS`` (default 3600) and re-fetched after
    that, so a rotation in Secret Manager is picked up without a redeploy.

``oauth2``
    A true client-credentials grant against a token authority, for when
    HubSpot OAuth replaces the private app. ``expires_in`` from the token
    response drives the refresh. Configured by
    ``LQABR_HUBSPOT_TOKEN_URL`` / ``LQABR_HUBSPOT_CLIENT_ID``, with the
    client secret coming from Secret Manager as
    ``lqabr-hubspot-client-secret``.

Swapping between them is config, never a code edit. The token VALUE is
never logged by either backend — audit_log records the token call and a
fingerprint of the result (see observability.bearer_fingerprint).
"""

from __future__ import annotations

import os
import threading
import time
from abc import ABC, abstractmethod
from typing import Optional

import requests

from lqabr_core.secrets import get_secret

from mcp.hubspot import NullObservability, ObservabilitySink

DEFAULT_TTL_SECONDS = 3600
_SAFETY_WINDOW_SECONDS = 60  # refresh slightly early rather than mid-call


class TokenError(RuntimeError):
    """Raised when a bearer cannot be obtained. Callers must fail the run —
    never fall back to an unauthenticated HubSpot call."""


class TokenProvider(ABC):
    """A source of short-lived HubSpot bearers, cached across a run."""

    @abstractmethod
    def fetch(self) -> tuple[str, int]:
        """Return ``(token, ttl_seconds)``. Implementations must not log the
        token value."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Backend name, for process_log."""


class SecretManagerTokenProvider(TokenProvider):
    """Default backend — the credential already configured in Google Secret
    Manager for this project."""

    secret_name = "lqabr-hubspot-access-token"

    def __init__(self, ttl_seconds: Optional[int] = None) -> None:
        self._ttl = int(ttl_seconds if ttl_seconds is not None
                        else os.environ.get("LQABR_HUBSPOT_TOKEN_TTL_SECONDS", DEFAULT_TTL_SECONDS))

    @property
    def name(self) -> str:
        return "secret_manager"

    def fetch(self) -> tuple[str, int]:
        # lqabr_core.secrets caches per process; clear it so a rotation in
        # Secret Manager is actually picked up when our own TTL expires.
        try:
            get_secret.cache_clear()
        except AttributeError:  # pragma: no cover - only if caching is removed
            pass
        try:
            token = get_secret(self.secret_name)
        except Exception as exc:  # noqa: BLE001 - surfaced as TokenError
            raise TokenError(f"could not read {self.secret_name} from Secret Manager: {exc}") from exc
        if not token:
            raise TokenError(f"{self.secret_name} resolved empty")
        return token, self._ttl


class OAuth2ClientCredentialsProvider(TokenProvider):
    """True machine-to-machine grant against a token authority."""

    def __init__(self, token_url: Optional[str] = None, client_id: Optional[str] = None,
                 session: Optional[requests.Session] = None, scope: Optional[str] = None) -> None:
        self._token_url = token_url or os.environ.get("LQABR_HUBSPOT_TOKEN_URL", "")
        self._client_id = client_id or os.environ.get("LQABR_HUBSPOT_CLIENT_ID", "")
        self._scope = scope if scope is not None else os.environ.get("LQABR_HUBSPOT_TOKEN_SCOPE", "")
        self._session = session or requests.Session()
        if not self._token_url or not self._client_id:
            raise TokenError(
                "oauth2 auth mode needs LQABR_HUBSPOT_TOKEN_URL and LQABR_HUBSPOT_CLIENT_ID")

    @property
    def name(self) -> str:
        return "oauth2_client_credentials"

    @property
    def token_url(self) -> str:
        return self._token_url

    def fetch(self) -> tuple[str, int]:
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": get_secret("lqabr-hubspot-client-secret"),
        }
        if self._scope:
            payload["scope"] = self._scope
        try:
            resp = self._session.post(self._token_url, data=payload, timeout=30)
        # `requests.RequestException` is a SUBCLASS of OSError, not a
        # superset: a plain OSError raised below the requests layer
        # (a missing/unreadable TLS CA bundle, socket exhaustion, a
        # DNS failure surfacing from the OS) is NOT caught by
        # `except RequestException` and would escape this retry loop
        # as an unhandled 500. Catch both.
        except (requests.RequestException, OSError) as exc:
            raise TokenError(f"token authority unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise TokenError(f"token authority returned HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise TokenError("token authority response carried no access_token")
        return token, int(body.get("expires_in") or DEFAULT_TTL_SECONDS)


class EnvTokenProvider(TokenProvider):
    """Direct backend — the HubSpot private-app token read straight from the
    environment variable ``LQABR_HUBSPOT_ACCESS_TOKEN`` (which a local ``.env``
    populates). No Secret Manager, no ADC: if the variable is unset this fails
    with a clear message instead of attempting a Secret Manager lookup.

    Also valid on Cloud Run when the token is injected as that same env var via
    ``--set-secrets`` — the value still originates in Secret Manager, it is just
    read from the environment rather than through the Secret Manager API."""

    env_var = "LQABR_HUBSPOT_ACCESS_TOKEN"

    def __init__(self, ttl_seconds: Optional[int] = None) -> None:
        self._ttl = int(ttl_seconds if ttl_seconds is not None
                        else os.environ.get("LQABR_HUBSPOT_TOKEN_TTL_SECONDS", DEFAULT_TTL_SECONDS))

    @property
    def name(self) -> str:
        return "env"

    def fetch(self) -> tuple[str, int]:
        token = os.environ.get(self.env_var, "").strip()
        if not token:
            raise TokenError(
                f"{self.env_var} is not set — put the HubSpot token in the "
                "environment (e.g. agents/email/.env) or set "
                "LQABR_HUBSPOT_AUTH_MODE=secret_manager to use Secret Manager")
        return token, self._ttl


def build_provider(mode: Optional[str] = None, **kwargs) -> TokenProvider:
    """Config-driven backend selection. Never hard-code a provider."""
    selected = (mode or os.environ.get("LQABR_HUBSPOT_AUTH_MODE", "secret_manager")).strip().lower()
    if selected == "secret_manager":
        return SecretManagerTokenProvider(**kwargs)
    if selected in ("env", "environment", "direct"):
        return EnvTokenProvider(**kwargs)
    if selected in ("oauth2", "oauth2_client_credentials"):
        return OAuth2ClientCredentialsProvider(**kwargs)
    raise TokenError(f"unknown LQABR_HUBSPOT_AUTH_MODE {selected!r}")


class RunTokenCache:
    """Holds one bearer for the life of a run and refreshes it on expiry.

    Every HubSpot hop asks this for its header. One acquisition per run in
    the normal case; a long run that outlives the TTL transparently
    re-acquires."""

    def __init__(self, provider: Optional[TokenProvider] = None,
                 obs: Optional[ObservabilitySink] = None) -> None:
        self._provider = provider or build_provider()
        self._obs = obs or NullObservability()
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def provider_name(self) -> str:
        return self._provider.name

    def _endpoint(self) -> str:
        token_url = getattr(self._provider, "token_url", None)
        return token_url or f"secretmanager://{getattr(self._provider, 'secret_name', 'unknown')}"

    def get(self) -> str:
        """The bearer to send on the next HubSpot hop. Acquires or refreshes
        as needed; raises TokenError rather than returning nothing."""
        with self._lock:
            now = time.time()
            if self._token and now < self._expires_at:
                return self._token

            refreshing = self._token is not None
            endpoint = self._endpoint()
            try:
                token, ttl = self._provider.fetch()
            except TokenError as exc:
                self._obs.audit(step=4, direction="outbound", endpoint=endpoint,
                                method="POST", status_code=None, error=str(exc))
                self._obs.process(step=4, event="token_acquire_failed",
                                  backend=self._provider.name, error=str(exc))
                raise

            self._token = token
            self._expires_at = now + max(int(ttl) - _SAFETY_WINDOW_SECONDS, 1)

            self._obs.audit(step=4, direction="outbound", endpoint=endpoint,
                            method="POST", status_code=200, bearer=token)
            self._obs.process(
                step=4,
                event="token_refreshed" if refreshing else "token_acquired",
                backend=self._provider.name,
                ttl_seconds=int(ttl),
                detail="cached for the run, refreshed on expiry; value never logged",
            )
            return token

    def invalidate(self) -> None:
        """Drop the cached bearer — call this on a 401 so the next hop
        re-acquires instead of retrying a dead token."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0
