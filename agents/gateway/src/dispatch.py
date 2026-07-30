"""Step 4 — Hand off, light.

Rev 3, Step 4:

    END GOAL   Deliver the trigger to the owning agent with the smallest
               possible footprint, keeping profile data out of the gateway.
    INPUTS     trigger_id, the resolved endpoint from Step 2, and the
               agent-protocol (A2A) configuration from config.yaml.
    PROCESSING Send trigger_id only, via the agent protocol, to the resolved
               agent endpoint. No profile payload is attached — this keeps the
               gateway footprint small. HTTPS ingress is converted to agent
               protocol at this boundary.
    OUTPUTS    A dispatch call to the target agent and its acknowledgement.
               The gateway's responsibility ends here.

"The gateway's responsibility ends here" is worth taking literally: this module
does not wait for the agent's work, inspect its result, or write anything back
to HubSpot. It delivers a trigger id and records what happened.

Failures are returned, not raised. Step 4's observability row asks for response
status, latency and retry count — which only exist if a failed hand-off is still
a first-class outcome. ``server.py`` decides what a failure means to HubSpot.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

try:
    from .audit import GatewayAudit
    from .router import RoutingDecision
except ImportError:  # pragma: no cover
    from audit import GatewayAudit  # type: ignore
    from router import RoutingDecision  # type: ignore

from soloai.protocols.a2a import A2AClient, A2AResponse, PayloadGuardError


@dataclass(frozen=True)
class DispatchResult:
    """One hand-off outcome, as reported back to the caller."""

    trigger_id: str
    agent: str
    endpoint: str
    ok: bool
    status_code: Optional[int]
    latency_ms: float
    attempts: int
    payload_size_bytes: int
    error: Optional[str] = None

    @property
    def retries(self) -> int:
        return max(0, self.attempts - 1)

    def as_dict(self) -> Dict[str, Any]:
        """Response body shape. Trigger reference only — no lead data."""
        return {
            "trigger_id": self.trigger_id,
            "agent": self.agent,
            "dispatched": self.ok,
            "status": self.status_code,
            "latency_ms": self.latency_ms,
            "retries": self.retries,
            "error": self.error,
        }


class Dispatcher:
    """Converts a routing decision into an A2A hand-off and audits it."""

    def __init__(self, client: A2AClient, audit: GatewayAudit,
                 gateway_version: str = "0.1.0",
                 include_routing_basis: bool = False) -> None:
        self._client = client
        self._audit = audit
        self._version = gateway_version
        # Off by default: Rev 3 Step 4 says "send trigger_id only", and a
        # property *value* is config-controlled (subscribe to a profile property
        # in the portal and it holds an email). Enable via
        # dispatch.include_routing_basis only if an agent needs the route id to
        # log why it was woken — the audit trail already has it either way.
        self._include_routing_basis = include_routing_basis

    def _metadata(self, decision: RoutingDecision, run_id: str) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {
            "trigger_id": decision.trigger_id,
            "run_id": run_id,
            "source": "agent_gateway",
            "gateway_version": self._version,
        }
        if self._include_routing_basis:
            metadata["route_id"] = decision.route_id
        return metadata

    def dispatch(self, decision: RoutingDecision, run_id: str) -> DispatchResult:
        """Hand off one trigger. Always returns; never raises for a remote fault."""
        try:
            response: A2AResponse = self._client.send_trigger(
                endpoint=decision.endpoint,
                trigger_id=decision.trigger_id,
                metadata=self._metadata(decision, run_id),
            )
        except PayloadGuardError as exc:
            # A programming error, not a transport one: something tried to
            # attach data that must not cross the gateway. Fail the dispatch
            # loudly rather than quietly stripping it.
            self._audit.record_dispatch(
                run_id, decision, ok=False, status_code=None, latency_ms=0.0,
                attempts=0, payload_size_bytes=0, error=f"payload guard: {exc}")
            return DispatchResult(
                trigger_id=decision.trigger_id, agent=decision.agent,
                endpoint=decision.endpoint, ok=False, status_code=None,
                latency_ms=0.0, attempts=0, payload_size_bytes=0,
                error=f"payload guard: {exc}")

        self._audit.record_dispatch(
            run_id, decision, ok=response.ok, status_code=response.status_code,
            latency_ms=response.latency_ms, attempts=response.attempts,
            payload_size_bytes=response.payload_size_bytes, error=response.error)

        return DispatchResult(
            trigger_id=decision.trigger_id, agent=decision.agent,
            endpoint=decision.endpoint, ok=response.ok,
            status_code=response.status_code, latency_ms=response.latency_ms,
            attempts=response.attempts,
            payload_size_bytes=response.payload_size_bytes, error=response.error)

    def dispatch_all(self, decisions: Sequence[RoutingDecision],
                     run_id: str) -> List[DispatchResult]:
        """Hand off every decision in a batch, in order.

        Sequential on purpose at MVP: HubSpot allows 10 concurrent requests and
        a batch is at most 100 events, so fan-out here would multiply load on
        the agents without any deadline requiring it. Revisit if p95 dispatch
        latency approaches HubSpot's request timeout.
        """
        results: List[DispatchResult] = []
        for decision in decisions:
            started = time.perf_counter()
            try:
                results.append(self.dispatch(decision, run_id))
            except Exception as exc:  # noqa: BLE001 - one bad hand-off must not
                # take the rest of the batch with it.
                self._audit.record_exception(run_id, exc, where="dispatch")
                results.append(DispatchResult(
                    trigger_id=decision.trigger_id, agent=decision.agent,
                    endpoint=decision.endpoint, ok=False, status_code=None,
                    latency_ms=round((time.perf_counter() - started) * 1000, 2),
                    attempts=1, payload_size_bytes=0, error=str(exc)))
        return results

    @classmethod
    def from_config(cls, config: Any, audit: GatewayAudit,
                    session: Optional[Any] = None) -> "Dispatcher":
        client = A2AClient(
            session=session,
            timeout_seconds=int(config.get("protocols.a2a.timeout_seconds", 30)),
            max_retries=int(config.get("protocols.a2a.max_retries", 2)),
            backoff_seconds=float(config.get("protocols.a2a.backoff_seconds", 0.5)),
        )
        return cls(client=client, audit=audit,
                   gateway_version=str(config.get("gateway.version", "0.1.0")),
                   include_routing_basis=bool(
                       config.get("dispatch.include_routing_basis", False)))
