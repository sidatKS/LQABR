"""A2A protocol — the gateway -> agent hand-off (Rev 3 Step 4).

*Send trigger_id only, via the agent protocol, to the resolved agent endpoint.
No profile payload is attached — this keeps the gateway footprint small. HTTPS
ingress is converted to agent protocol at this boundary.*

Wire format is A2A ``message/send`` over JSON-RPC 2.0, matching what the
orchestrator already speaks (``agents/orchestrator/src/a2a_dispatch.py``) so
the agents need no second dialect.

This client posts to whatever endpoint the router resolved — it holds no opinion
about the sidecar. Whether traffic goes through the agentgateway sidecar (gaining
its access log and OTel span for the hop) or straight at the agent is decided by
what each agent's ``endpoint_env`` points at in ``agents_registry.yaml``:
``http://127.0.0.1:3000/a2a/<agent>`` for the sidecar, the agent's own URL to
bypass it. One address per agent, in one place.

The payload guard in ``send_trigger`` is the load-bearing part of this module:
it is what makes "no lead-profile data crosses the gateway" a property of the
code rather than a promise in a document.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import requests


class A2AError(RuntimeError):
    """Transport or protocol failure on the hand-off."""


class PayloadGuardError(RuntimeError):
    """Something tried to attach non-trigger data to a dispatch."""


#: Everything the gateway is permitted to put on the wire. Correlation ids plus
#: the one identifier the agent can actually resolve — and nothing else.
#:
#: ``object_id`` is the HubSpot **contact record id**, and it is here on purpose
#: (D-05). Neither the ``trigger_id`` nor HubSpot's ``eventId`` can be looked up
#: in the CRM — no property holds either — so an agent receiving only those has
#: no way to reach the lead that fired the trigger; Rev 3's answer was a
#: criteria query returning a *chunk* of ~5 profiles that may not even contain
#: it. Sending the record id makes the hand-off exact and the lookup a direct,
#: strongly-consistent read instead of an eventually-consistent search.
#:
#: ``property_name`` / ``property_value`` are still NOT here, even though FR-7
#: requires them in the *log*. A property value is config-controlled: put a
#: subscription on a profile property in the portal and the "value" arrives
#: holding an email address. It belongs in the audit trail (where it is redacted
#: if it looks like a lead field), not on the wire.
ALLOWED_METADATA_KEYS = frozenset({
    "trigger_id", "object_id", "run_id", "route_id", "source", "gateway_version",
    # Grouped hand-off (dispatch.mode = grouped). One call carries N leads, so
    # the singular ids become lists and the batch gets its own correlation id.
    #
    # These are ids and counts ONLY. Every lead-profile field stays excluded,
    # which is the whole point of this guard: a list of object ids is not lead
    # data, and the agent still resolves each profile from HubSpot itself.
    "batch_id", "object_ids", "batch_size", "trigger_ids",
    # Audience-resolved research hand-off (Rev 5): the blog Ticket id, so the
    # agent can read the summary. Still an id, never lead-profile data.
    "summary_ref_id",
    # Blog-ticket hand-off (audience disabled): the ticket's own fields under
    # HubSpot's ORIGINAL names, so the agent receives the ticket exactly as
    # HubSpot sent it. The correlation id rides along as ``triggerId`` (camelCase)
    # instead of the snake_case ``trigger_id`` the other agents get.
    "objectId", "propertyName", "subscriptionType", "eventId", "triggerId",
})


@dataclass
class A2AResponse:
    """Outcome of one hand-off, shaped for the audit stream."""

    ok: bool
    status_code: Optional[int]
    latency_ms: float
    attempts: int
    endpoint: str
    payload_size_bytes: int
    result: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class A2AClient:
    """Minimal A2A ``message/send`` client with bounded retries.

    Inject a ``requests.Session`` to test without a network, matching the
    house convention in ``a2a_dispatch.py``.
    """

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        timeout_seconds: int = 30,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        sleep: Any = time.sleep,
    ) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout_seconds
        self._max_retries = max(0, int(max_retries))
        self._backoff = float(backoff_seconds)
        self._sleep = sleep

    # ------------------------------------------------------------ guardrail
    @staticmethod
    def _guard_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
        extra = set(metadata) - ALLOWED_METADATA_KEYS
        if extra:
            raise PayloadGuardError(
                f"refusing to dispatch non-trigger metadata {sorted(extra)}: only "
                f"{sorted(ALLOWED_METADATA_KEYS)} may cross the gateway. The agent "
                "resolves the profile itself from HubSpot over MCP (Rev 3 Step 6)."
            )
        return metadata

    @staticmethod
    def build_message(trigger_id: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The JSON-RPC envelope. ``trigger_id`` is the entire message body."""
        metadata = A2AClient._guard_metadata(dict(metadata or {}))
        if not trigger_id:
            raise PayloadGuardError("a dispatch must carry a trigger_id")
        # The correlation id is injected into params.metadata. The blog-ticket
        # hand-off carries it as camelCase ``triggerId`` already, so skip the
        # snake_case ``trigger_id`` for that path; every other agent still gets
        # ``trigger_id`` exactly as before.
        _correlation = {} if "triggerId" in metadata else {"trigger_id": trigger_id}
        envelope = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": trigger_id}],
                    "messageId": str(uuid.uuid4()),
                },
                "metadata": {**metadata, **_correlation},
            },
        }

        # Compat shim (temporary). The email and voice agents are plain REST,
        # not A2A, and read the ids from the top level of the body rather than
        # from params.metadata -- they reject with 400 "payload carries no
        # objectId/object_id" otherwise. Mirror the same ids at the top level.
        #
        # No new data crosses the gateway: these are the values already inside
        # metadata, which _guard_metadata has just checked against
        # ALLOWED_METADATA_KEYS. The trigger-only guarantee is untouched.
        #
        # Remove this block once the agents read params.metadata.object_id.
        envelope["trigger_id"] = trigger_id
        object_id = metadata.get("object_id") or metadata.get("objectId")
        if object_id is not None:
            envelope["object_id"] = object_id
            envelope["objectId"] = object_id
        # Grouped hand-off: the same mirroring for the plural form.
        object_ids = metadata.get("object_ids")
        if object_ids is not None:
            envelope["object_ids"] = list(object_ids)
            envelope["objectIds"] = list(object_ids)
        batch_id = metadata.get("batch_id")
        if batch_id is not None:
            envelope["batch_id"] = batch_id
            envelope["batchId"] = batch_id
        summary_ref_id = metadata.get("summary_ref_id")
        if summary_ref_id is not None:
            envelope["summary_ref_id"] = summary_ref_id
            envelope["summaryRefId"] = summary_ref_id

        return envelope

    # -------------------------------------------------------------- sending
    def send_trigger(
        self,
        endpoint: str,
        trigger_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> A2AResponse:
        """Hand off one trigger. Never raises for a remote failure.

        Returns an ``A2AResponse`` either way, because Step 4's observability
        row wants response status, latency and retry count recorded even —
        especially — when the hand-off failed. The caller decides what a
        failure means for the HTTP response to HubSpot.
        """
        body = self.build_message(trigger_id, metadata)
        # Measure the JSON that actually goes on the wire, not Python's repr —
        # otherwise the "light payload" number is off by whatever repr adds.
        payload_size = len(json.dumps(body).encode("utf-8"))
        # Correlation headers, so the agentgateway sidecar's access log can be
        # joined to the gateway's own records (config/agentgateway.yaml reads
        # these). Ids only; nothing about the lead.
        headers = {
            "x-lqabr-trigger-id": trigger_id,
            "x-lqabr-run-id": str((metadata or {}).get("run_id", "")),
        }
        started = time.perf_counter()
        attempts = 0
        last_error: Optional[str] = None
        status_code: Optional[int] = None

        while attempts <= self._max_retries:
            attempts += 1
            try:
                response = self._session.post(endpoint, json=body, timeout=self._timeout,
                                              headers=headers)
                status_code = response.status_code
                if 200 <= status_code < 300:
                    try:
                        parsed = response.json()
                    except ValueError:
                        parsed = {}
                    if isinstance(parsed, dict) and parsed.get("error"):
                        # A protocol-level error is terminal; retrying a
                        # rejected message just rejects it again.
                        return A2AResponse(
                            ok=False, status_code=status_code,
                            latency_ms=_elapsed_ms(started), attempts=attempts,
                            endpoint=endpoint, payload_size_bytes=payload_size,
                            error=f"agent returned A2A error: {parsed['error']}",
                        )
                    return A2AResponse(
                        ok=True, status_code=status_code,
                        latency_ms=_elapsed_ms(started), attempts=attempts,
                        endpoint=endpoint, payload_size_bytes=payload_size,
                        result=parsed.get("result", {}) if isinstance(parsed, dict) else {},
                    )
                last_error = f"HTTP {status_code}: {response.text[:200]}"
                # 4xx is our fault and will not fix itself on retry.
                if 400 <= status_code < 500:
                    break
            except requests.RequestException as exc:
                last_error = f"transport error: {exc}"

            if attempts <= self._max_retries:
                self._sleep(self._backoff * (2 ** (attempts - 1)))

        return A2AResponse(
            ok=False, status_code=status_code, latency_ms=_elapsed_ms(started),
            attempts=attempts, endpoint=endpoint, payload_size_bytes=payload_size,
            error=last_error or "dispatch failed",
        )


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
