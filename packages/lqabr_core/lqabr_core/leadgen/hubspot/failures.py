"""Failure taxonomy.

Review finding B1: the previous build caught every exception in one place and
wrote all of them to ``errors/schema_mismatch.jsonl``. A missing env var then
produced N "bad record" entries and a run that reported itself complete.

Three kinds of failure, three different handlings:

  RECORD   the record is wrong. Bad data, or HubSpot rejected the payload.
           -> errors/schema_mismatch.jsonl, mark the lead failed, CONTINUE.
           (SchemaMismatchError, defined in schema.py)

  TRANSPORT the network or HubSpot misbehaved for this attempt. Timeouts,
           connection resets, 5xx after retries, 429 after retries.
           -> errors/transport_failures.jsonl, mark the lead failed, CONTINUE,
           but count it: N consecutive transport failures means the dependency
           is down, not that N leads are bad.

  SYSTEMIC  the run cannot succeed for ANY lead. Auth misconfigured, token
           endpoint refusing, credentials revoked.
           -> re-raise. HALT the run. Never write per-lead error records.

The circuit breaker turns a run of transport failures into a systemic halt, so
a HubSpot outage stops the run instead of burning through 263 leads.
"""

from __future__ import annotations

import os


class TransportFailure(RuntimeError):
    """A network-level failure for one attempt. Retryable, then per-lead."""

    def __init__(self, message: str, status: int | None = None, endpoint: str | None = None):
        self.status = status
        self.endpoint = endpoint
        super().__init__(message)


class SystemicFailure(RuntimeError):
    """The run cannot succeed for any lead. Halts the run — never per-lead."""

    def __init__(self, message: str, reason: str = "systemic"):
        self.reason = reason
        super().__init__(message)


DEFAULT_CONSECUTIVE_TRANSPORT_LIMIT = 5


class CircuitBreaker:
    """Trip after N consecutive transport failures.

    A single failed lead is data. Five in a row is a dependency outage, and
    continuing just converts one incident into 263 misleading error records.
    """

    def __init__(self, limit: int | None = None):
        self.limit = (
            limit
            if limit is not None
            else int(
                os.getenv(
                    "LQABR_CONSECUTIVE_TRANSPORT_LIMIT",
                    str(DEFAULT_CONSECUTIVE_TRANSPORT_LIMIT),
                )
            )
        )
        self.consecutive = 0

    def record_success(self) -> None:
        self.consecutive = 0

    def record_transport_failure(self) -> bool:
        """Return True if the breaker has tripped."""
        self.consecutive += 1
        return self.limit > 0 and self.consecutive >= self.limit

    @property
    def tripped(self) -> bool:
        return self.limit > 0 and self.consecutive >= self.limit
