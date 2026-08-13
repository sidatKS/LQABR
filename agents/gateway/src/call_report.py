"""Vapi end-of-call report relay — the gateway's second inbound path.

Why this is a separate module, and separate from the router
-----------------------------------------------------------
Everything else in this gateway is *trigger routing*: HubSpot says a property
changed, the router decides which agent owns it, and a ``trigger_id`` crosses
the wire carrying no lead data. That guarantee is enforced by
``ALLOWED_METADATA_KEYS`` in ``soloai.protocols.a2a``.

A Vapi end-of-call report is not a trigger. It is content — a transcript, a
recording URL, an ended-reason — and the Text/Voice agent needs all of it. So
this path deliberately does NOT go through ``router.py`` or ``dispatch.py``:

  * no routing decision (there is nothing to decide; one source, one target)
  * no A2A envelope (txtv reads Vapi's own shape)
  * no payload guard (it would reject the body by design)

Instead the gateway acts as an authenticating reverse proxy for exactly one
route. Confirmed with the Text/Voice agent owner, 2026-08-04:

    "Expose POST /call-report, write one audit line, forward the body
     UNCHANGED, no A2A envelope, no transcript parsing, no routing —
     confirmed correct."

Why the gateway is in the path at all: ``lqabr-dev-txtv`` is deployed private,
and Vapi cannot present a Google identity token. The gateway is the public
front door, and it is the component that owns provider authenticity — txtv's
handler does no secret verification of its own.

What crosses, and what does not
-------------------------------
The report body is forwarded byte-for-byte and is never inspected beyond
pulling two correlation ids for the audit line. The transcript is never
logged: the audit hooks scan every record for profile fields and would trip
``ProfileFieldLeak`` on a real conversation, which is the correct behaviour.
"""

from __future__ import annotations

import hmac
import threading
import time
from typing import Any, Dict, Optional, Tuple

import requests

#: Cloud Run's metadata server. Minting an ID token here needs no key file —
#: the platform signs it for the service's own runtime service account.
_METADATA_TOKEN_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/identity"
)

#: Refresh a little before the hour is up, so a request never races expiry.
_TOKEN_TTL_SECONDS = 3300


class ReportRelayError(Exception):
    """A relay failure with the status code the caller should return."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class VapiSecretError(ReportRelayError):
    """The shared secret was missing, malformed, or wrong."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=401)


def verify_vapi_secret(expected: str, headers: Dict[str, str]) -> None:
    """Authenticate the caller as Vapi.

    Vapi can be configured to send the shared secret either as a custom header
    or as a bearer token, so both are accepted. Comparison is constant-time —
    a plain ``==`` on a secret leaks its length and prefix through timing.

    Raises ``VapiSecretError`` (401) on any mismatch. Note the gateway owns
    this check: txtv's ``/call-report`` handler performs no verification of its
    own, so if this is skipped the endpoint is genuinely open.
    """
    if not expected:
        raise VapiSecretError(
            "LQABR_VAPI_WEBHOOK_SECRET is not set — refusing to relay an "
            "unauthenticated report. Set the secret, or disable "
            "vapi.report.signature.enabled in config.yaml for local testing."
        )

    provided = (headers.get("x-vapi-secret") or "").strip()
    if not provided:
        authorization = (headers.get("authorization") or "").strip()
        if authorization.lower().startswith("bearer "):
            provided = authorization[7:].strip()

    if not provided:
        raise VapiSecretError("no x-vapi-secret header and no bearer token")
    if not hmac.compare_digest(provided, expected):
        raise VapiSecretError("x-vapi-secret does not match")


def extract_correlation(payload: Any) -> Dict[str, Optional[str]]:
    """Pull only the two ids the audit line needs. Never the transcript.

    Both are best-effort by design. ``hubspot_contact_id`` is not top level —
    it rides in ``assistantOverrides.variableValues``, injected by txtv when
    the call was created, and Vapi echoes it back. It can legitimately be
    absent, in which case txtv falls back to a phone-number lookup. Its
    absence is therefore NOT an error and must never produce a 4xx: rejecting
    the report would lose a completed call's outcome permanently.
    """
    ids: Dict[str, Optional[str]] = {"call_id": None, "hubspot_contact_id": None}
    if not isinstance(payload, dict):
        return ids

    # Vapi sends either {"message": {...}} or the bare message.
    message = payload.get("message")
    message = message if isinstance(message, dict) else payload

    for holder in (message.get("call"), message.get("artifact"), message):
        if isinstance(holder, dict) and holder.get("id"):
            ids["call_id"] = str(holder["id"])
            break

    # Checked in the order txtv checks them, so the audit line agrees with the
    # id txtv actually resolves.
    for holder in (message, message.get("call"), message.get("assistant")):
        if not isinstance(holder, dict):
            continue
        overrides = holder.get("assistantOverrides")
        if isinstance(overrides, dict):
            variables = overrides.get("variableValues")
            if isinstance(variables, dict) and variables.get("hubspot_contact_id"):
                ids["hubspot_contact_id"] = str(variables["hubspot_contact_id"])
                return ids
        metadata = holder.get("metadata")
        if isinstance(metadata, dict) and metadata.get("hubspot_contact_id"):
            ids["hubspot_contact_id"] = str(metadata["hubspot_contact_id"])
            return ids

    return ids


class IdTokenProvider:
    """Mints and caches a Google-signed OIDC token for one audience.

    ``lqabr-dev-txtv`` is deployed with "Require authentication", so the
    forward needs ``Authorization: Bearer <id_token>`` where the audience is
    txtv's base URL. The gateway's runtime service account also needs
    ``roles/run.invoker`` on that service — the token alone is not enough.

    Off-platform (a developer laptop) the metadata server is unreachable. That
    is not fatal: ``token()`` returns None and the relay forwards without an
    Authorization header, which is exactly right when the target is a local
    stub. On Cloud Run a missing token would surface as a 403 from txtv, which
    is visible rather than silent.
    """

    def __init__(self, audience: str, timeout_seconds: int = 5,
                 session: Optional[requests.Session] = None) -> None:
        self._audience = audience
        self._timeout = timeout_seconds
        self._session = session or requests.Session()
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    def token(self) -> Optional[str]:
        if not self._audience:
            return None
        with self._lock:
            if self._token and time.time() < self._expires_at:
                return self._token
            try:
                response = self._session.get(
                    _METADATA_TOKEN_URL,
                    params={"audience": self._audience, "format": "full"},
                    headers={"Metadata-Flavor": "Google"},
                    timeout=self._timeout,
                )
            except requests.RequestException:
                return None  # not on GCP — see the class docstring
            if response.status_code != 200 or not response.text.strip():
                return None
            self._token = response.text.strip()
            self._expires_at = time.time() + _TOKEN_TTL_SECONDS
            return self._token


class CallReportRelay:
    """Forwards one Vapi report to the Text/Voice agent, unchanged."""

    def __init__(self, target_url: str, secret: str = "",
                 verify_secret: bool = True, timeout_seconds: int = 30,
                 session: Optional[requests.Session] = None,
                 token_provider: Optional[IdTokenProvider] = None) -> None:
        self._target_url = target_url.rstrip("/")
        self._secret = secret
        self._verify_secret = verify_secret
        self._timeout = timeout_seconds
        self._session = session or requests.Session()
        # Audience is the service root, not the path — Cloud Run validates the
        # token against the service URL.
        audience = self._target_url.split("/call-report")[0] if self._target_url else ""
        self._tokens = token_provider or IdTokenProvider(audience, session=self._session)

    @property
    def target_url(self) -> str:
        return self._target_url

    def forward(self, raw_body: bytes, headers: Dict[str, str]) -> Tuple[int, bytes, str]:
        """Relay the report. Returns (status_code, body, content_type).

        The body is passed through byte-for-byte: txtv accepts either the
        wrapped ``{"message": {...}}`` envelope or the bare message and keys
        off ``message.type == "end-of-call-report"``. Re-serialising it here
        would risk changing a shape we do not own.
        """
        if self._verify_secret:
            verify_vapi_secret(self._secret, headers)

        if not self._target_url:
            raise ReportRelayError(
                "LQABR_VOICE_REPORT_URL is not set — nowhere to relay the report",
                status_code=503,
            )

        outbound: Dict[str, str] = {
            "Content-Type": headers.get("content-type", "application/json"),
        }
        token = self._tokens.token()
        if token:
            outbound["Authorization"] = f"Bearer {token}"

        try:
            response = self._session.post(
                self._target_url, data=raw_body, headers=outbound, timeout=self._timeout)
        except requests.RequestException as exc:
            # 502 rather than 200: Vapi retries (backoffPlan.maxRetries = 2),
            # and a swallowed failure here loses the call outcome for good.
            raise ReportRelayError(f"could not reach the text/voice agent: {exc}") from exc

        return (response.status_code, response.content,
                response.headers.get("content-type", "application/json"))
