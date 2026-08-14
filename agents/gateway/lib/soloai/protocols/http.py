"""HTTP ingress protocol — the gateway's single entry point.

Rev 3 Step 2: *The gateway HTTP ingress (server.py) receives the Step 1
webhook. This is the only entry point for HubSpot ingress; no agent-to-agent
traffic passes here.*

This module owns the transport-level concerns of that ingress, so ``server.py``
stays a thin wiring layer over router -> audit -> dispatch:

    * HubSpot v3 request-signature verification
    * batch envelope validation (JSON array, <= 100 events)
    * a concurrency bound matching HubSpot's throttle of 10

Nothing here knows what a route is.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


class IngressError(ValueError):
    """The request is not a well-formed HubSpot webhook delivery."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class SignatureError(IngressError):
    """Signature missing, stale, or wrong. Always a 401 — never a hint why."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=401)


#: How far ahead of us a caller's clock may be. HubSpot's timestamp is inside
#: the HMAC, so this only bounds the replay window's forward half.
MAX_CLOCK_SKEW_SECONDS = 60


# --------------------------------------------------------------- signatures
def compute_v3_signature(
    secret: str,
    method: str,
    uri: str,
    body: str,
    timestamp: str,
) -> str:
    """HubSpot signature v3: base64(HMAC-SHA256(method + uri + body + ts)).

    ``uri`` must be the full request URI as HubSpot built it (scheme, host,
    path, query) — a proxy that rewrites the host will break verification,
    which is why ``LQABR_GATEWAY_PUBLIC_URL`` exists as an override.
    """
    message = f"{method.upper()}{uri}{body}{timestamp}"
    digest = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256)
    return base64.b64encode(digest.digest()).decode("ascii")


def verify_v3_signature(
    secret: str,
    method: str,
    uri: str,
    body: str,
    timestamp: str,
    signature: str,
    max_age_seconds: int = 300,
    now: Optional[float] = None,
) -> None:
    """Raise ``SignatureError`` unless the delivery is authentic and fresh.

    Not specified in Rev 3. Added because the ingress is a public HTTPS
    endpoint and the design gives it authority to start outreach: without
    verification, anyone who learns the URL can make the gateway call an
    agent. Disable only for local dev, via ``ingress.signature.enabled``.
    """
    if not secret:
        raise SignatureError("signature verification is on but HUBSPOT_APP_SECRET is unset")
    if not signature:
        raise SignatureError("missing X-HubSpot-Signature-v3 header")
    if not timestamp:
        raise SignatureError("missing X-HubSpot-Request-Timestamp header")

    try:
        sent_at = int(timestamp) / 1000.0
    except (TypeError, ValueError):
        raise SignatureError("X-HubSpot-Request-Timestamp is not epoch milliseconds") from None

    current = time.time() if now is None else now
    age = current - sent_at
    # Replay guard: an old-but-correctly-signed body is still an attack. The
    # future-skew allowance is deliberately much tighter than the past one —
    # using abs() here would silently double the replay window.
    if age > max_age_seconds:
        raise SignatureError("request timestamp is older than the accepted window")
    if age < -MAX_CLOCK_SKEW_SECONDS:
        raise SignatureError("request timestamp is too far in the future")

    expected = compute_v3_signature(secret, method, uri, body, timestamp)
    # Compare as bytes: hmac.compare_digest raises TypeError on a non-ASCII
    # str, and headers arrive latin-1 decoded, so a single high byte in the
    # signature header would turn an auth failure into a 500.
    if not hmac.compare_digest(expected.encode("utf-8"),
                               signature.encode("utf-8", errors="replace")):
        raise SignatureError("signature mismatch")


# -------------------------------------------------------------- batch shape
@dataclass(frozen=True)
class IngressBatch:
    """A validated inbound delivery, before any routing has happened."""

    events: List[Dict[str, Any]]
    raw_size_bytes: int

    @property
    def size(self) -> int:
        return len(self.events)


def parse_batch(payload: Any, raw_body: bytes, max_batch_size: int = 100) -> IngressBatch:
    """Validate the webhook envelope.

    Rev 3: *Body is a JSON array — up to 100 events batched per request.*
    A single object is accepted too: HubSpot's own docs show an array, but
    being strict about a shape we can trivially normalise would drop real
    triggers for no benefit.
    """
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise IngressError("body must be a JSON array of events")
    if not payload:
        raise IngressError("body contains no events")
    if len(payload) > max_batch_size:
        raise IngressError(
            f"batch of {len(payload)} exceeds the {max_batch_size}-event limit",
            status_code=413,
        )
    for index, event in enumerate(payload):
        if not isinstance(event, dict):
            raise IngressError(f"event at index {index} is not a JSON object")
    return IngressBatch(events=list(payload), raw_size_bytes=len(raw_body))


# -------------------------------------------------------------- concurrency
class ConcurrencyGuard:
    """Bounded in-flight counter for the ingress.

    Rev 3 Step 2 System log: *ingress concurrency against the throttle limit
    of 10*, and page 3: *10 concurrent requests (HubSpot default). This is the
    concurrency the gateway must absorb.*

    A counter rather than a semaphore because the requirement is to *observe*
    the bound — the number belongs on the system stream — while still absorbing
    the load. ``peak`` survives for the life of the process so a container can
    be asked after the fact whether it ever came close to the limit.
    """

    def __init__(self, limit: int = 10) -> None:
        self.limit = max(1, int(limit))
        self._lock = threading.Lock()
        self._in_flight = 0
        self.peak = 0

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def acquire(self) -> Tuple[int, bool]:
        """Returns ``(in_flight_after, at_or_over_limit)``."""
        with self._lock:
            self._in_flight += 1
            self.peak = max(self.peak, self._in_flight)
            return self._in_flight, self._in_flight > self.limit

    def release(self) -> int:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            return self._in_flight

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return {
                "in_flight": self._in_flight,
                "concurrency_limit": self.limit,
                "peak_in_flight": self.peak,
                "headroom": max(0, self.limit - self._in_flight),
            }
