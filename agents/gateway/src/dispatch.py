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

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

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

    #: The eventIds this hand-off covers. One for a per-lead dispatch, N for a
    #: grouped one. server.py releases exactly these from the dedupe store when
    #: the hand-off fails, so the two modes share one bookkeeping path.
    event_ids: Tuple[Optional[str], ...] = ()

    #: How many leads travelled in this call. 1 unless grouped.
    batch_size: int = 1

    def as_dict(self) -> Dict[str, Any]:
        """Response body shape. Trigger reference only — no lead data."""
        body: Dict[str, Any] = {
            "trigger_id": self.trigger_id,
            "agent": self.agent,
            "dispatched": self.ok,
            "status": self.status_code,
            "latency_ms": self.latency_ms,
            "retries": self.retries,
            "error": self.error,
        }
        if self.batch_size > 1:
            body["batch_size"] = self.batch_size
        return body


class Dispatcher:
    """Converts a routing decision into an A2A hand-off and audits it."""

    #: Values accepted by ``dispatch.mode`` / ``LQABR_DISPATCH_MODE``.
    MODES = ("per_lead", "grouped")

    def __init__(self, client: A2AClient, audit: GatewayAudit,
                 gateway_version: str = "0.1.0",
                 include_routing_basis: bool = False,
                 mode: str = "per_lead",
                 batch_size: int = 20) -> None:
        self._client = client
        self._audit = audit
        self._version = gateway_version
        # Deployment variables, not code paths: the same image behaves
        # differently depending on how it was deployed. An unknown mode falls
        # back to per_lead rather than failing the boot — a typo in an env var
        # must not take the ingress down, and the startup record says which
        # mode is actually live.
        self._mode = mode if mode in self.MODES else "per_lead"
        self._batch_size = max(1, int(batch_size))
        # Off by default: Rev 3 Step 4 says "send trigger_id only", and a
        # property *value* is config-controlled (subscribe to a profile property
        # in the portal and it holds an email). Enable via
        # dispatch.include_routing_basis only if an agent needs the route id to
        # log why it was woken — the audit trail already has it either way.
        self._include_routing_basis = include_routing_basis

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def batch_size(self) -> int:
        return self._batch_size

    def _metadata(self, decision: RoutingDecision, run_id: str) -> Dict[str, Any]:
        # Research hand-off (blog-ticket route, audience resolution disabled):
        # forward ONLY the ticket's own details, under HubSpot's original field
        # names. The agent receives the ticket exactly as HubSpot named it and
        # nothing else. The trigger_id is supplied by the A2A envelope itself
        # (the message body), not here, so it is not "extra" wire metadata.
        if decision.agent == "research":
            return {
                "objectId": decision.object_id,
                "propertyName": decision.property_name,
                "subscriptionType": "ticket.propertyChange",
                "eventId": decision.event_id,
                "triggerId": decision.trigger_id,
            }
        metadata: Dict[str, Any] = {
            "trigger_id": decision.trigger_id,
            # The HubSpot contact record id — what the agent actually calls the
            # CRM with (D-05). The trigger_id travels alongside it purely as the
            # correlation handle that ties the agent's logs back to the routing
            # decision; it is not resolvable in HubSpot and never was.
            "object_id": decision.object_id,
            "run_id": run_id,
            "source": "agent_gateway",
            "gateway_version": self._version,
        }
        if self._include_routing_basis:
            metadata["route_id"] = decision.route_id
        if decision.summary_ref_id is not None:
            metadata["summary_ref_id"] = decision.summary_ref_id
        if getattr(decision, "blog_published_at", None) is not None:
            metadata["blog_published_at"] = decision.blog_published_at
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
                error=f"payload guard: {exc}",
                event_ids=(decision.event_id,))

        self._audit.record_dispatch(
            run_id, decision, ok=response.ok, status_code=response.status_code,
            latency_ms=response.latency_ms, attempts=response.attempts,
            payload_size_bytes=response.payload_size_bytes, error=response.error)

        return DispatchResult(
            trigger_id=decision.trigger_id, agent=decision.agent,
            endpoint=decision.endpoint, ok=response.ok,
            status_code=response.status_code, latency_ms=response.latency_ms,
            attempts=response.attempts,
            payload_size_bytes=response.payload_size_bytes, error=response.error,
            event_ids=(decision.event_id,))

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
                    attempts=1, payload_size_bytes=0, error=str(exc),
                    event_ids=(decision.event_id,)))
        return results

    # ------------------------------------------------------------- grouped
    #: Namespace for batch ids. Distinct from the router's trigger namespace so
    #: a batch id can never collide with a trigger id.
    _BATCH_NAMESPACE = uuid.UUID("2f8c1d40-9a6b-5f21-b7e3-0c4a9d5e6f82")

    def _batch_id(self, run_id: str, agent: str, summary_ref_id: Optional[str],
                  index: int) -> str:
        """Deterministic from (run_id, agent, summary_ref_id, chunk) so a
        redelivery of the same batch mints the same id and the audit trail stays
        joinable. summary_ref_id keeps two blog tickets' research batches apart."""
        seed = f"lqabr-batch:{run_id}:{agent}:{summary_ref_id or '-'}:{index}"
        return f"bat-{uuid.uuid5(self._BATCH_NAMESPACE, seed).hex[:24]}"

    def _batch_metadata(self, decisions: Sequence[RoutingDecision], run_id: str,
                        batch_id: str) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {
            "batch_id": batch_id,
            # The resolvable handles. The agent fetches these from HubSpot in
            # one batch/read call (max 100 ids, strongly consistent).
            "object_ids": [d.object_id for d in decisions],
            "batch_size": len(decisions),
            # Per-lead correlation, kept so a single lead is still greppable in
            # the audit trail after batching.
            "trigger_ids": [d.trigger_id for d in decisions],
            "run_id": run_id,
            "source": "agent_gateway",
            "gateway_version": self._version,
        }
        if self._include_routing_basis:
            metadata["route_id"] = decisions[0].route_id
        if decisions[0].summary_ref_id is not None:
            metadata["summary_ref_id"] = decisions[0].summary_ref_id
        return metadata

    def dispatch_grouped(self, decisions: Sequence[RoutingDecision],
                         run_id: str) -> List[DispatchResult]:
        """One hand-off per agent per chunk, instead of one per lead.

        Groups the decisions of a single ingress by target agent and chunks
        each group at ``batch_size``. Nothing is held back waiting for more:
        the leads already arrived together, so this batches what is in hand and
        sends. That is why it is "up to batch_size", never a guaranteed size.

        Failure is all-or-nothing per chunk — a failed call releases every
        eventId in that chunk, so HubSpot retries the whole group. Agents must
        therefore be idempotent per object_id; see the design note.
        """
        # Group by (agent, summary_ref_id): the summary_ref_id keeps the leads
        # of two different blog tickets in separate calls even though both are
        # bound for research. For every non-blog agent it is None, so this
        # collapses to grouping by agent — unchanged behaviour.
        by_key: Dict[Tuple[str, Optional[str]], List[RoutingDecision]] = {}
        for decision in decisions:
            by_key.setdefault((decision.agent, decision.summary_ref_id), []).append(decision)

        results: List[DispatchResult] = []
        for (agent, summary_ref_id), group in by_key.items():
            for index in range(0, len(group), self._batch_size):
                chunk = group[index:index + self._batch_size]
                results.append(self._dispatch_chunk(
                    chunk, run_id,
                    self._batch_id(run_id, agent, summary_ref_id, index // self._batch_size)))
        return results

    def _dispatch_chunk(self, chunk: Sequence[RoutingDecision], run_id: str,
                        batch_id: str) -> DispatchResult:
        head = chunk[0]
        event_ids = tuple(d.event_id for d in chunk)
        started = time.perf_counter()
        try:
            response: A2AResponse = self._client.send_trigger(
                endpoint=head.endpoint,
                trigger_id=batch_id,
                metadata=self._batch_metadata(chunk, run_id, batch_id),
            )
        except PayloadGuardError as exc:
            self._audit.record_batch_dispatch(
                run_id, head, batch_id=batch_id, batch_size=len(chunk), ok=False,
                status_code=None, latency_ms=0.0, attempts=0,
                payload_size_bytes=0, error=f"payload guard: {exc}")
            return DispatchResult(
                trigger_id=batch_id, agent=head.agent, endpoint=head.endpoint,
                ok=False, status_code=None, latency_ms=0.0, attempts=0,
                payload_size_bytes=0, error=f"payload guard: {exc}",
                event_ids=event_ids, batch_size=len(chunk))
        except Exception as exc:  # noqa: BLE001 - one bad chunk must not take
            # the rest of the batch with it.
            self._audit.record_exception(run_id, exc, where="dispatch_grouped")
            return DispatchResult(
                trigger_id=batch_id, agent=head.agent, endpoint=head.endpoint,
                ok=False, status_code=None,
                latency_ms=round((time.perf_counter() - started) * 1000, 2),
                attempts=1, payload_size_bytes=0, error=str(exc),
                event_ids=event_ids, batch_size=len(chunk))

        self._audit.record_batch_dispatch(
            run_id, head, batch_id=batch_id, batch_size=len(chunk),
            ok=response.ok, status_code=response.status_code,
            latency_ms=response.latency_ms, attempts=response.attempts,
            payload_size_bytes=response.payload_size_bytes, error=response.error)

        return DispatchResult(
            trigger_id=batch_id, agent=head.agent, endpoint=head.endpoint,
            ok=response.ok, status_code=response.status_code,
            latency_ms=response.latency_ms, attempts=response.attempts,
            payload_size_bytes=response.payload_size_bytes, error=response.error,
            event_ids=event_ids, batch_size=len(chunk))

    @classmethod
    def from_config(cls, config: Any, audit: GatewayAudit,
                    session: Optional[Any] = None) -> "Dispatcher":
        client = A2AClient(
            session=session,
            timeout_seconds=int(config.get("protocols.a2a.timeout_seconds", 30)),
            max_retries=int(config.get("protocols.a2a.max_retries", 2)),
            backoff_seconds=float(config.get("protocols.a2a.backoff_seconds", 0.5)),
        )
        # Env wins over config.yaml: mode and size are deployment variables,
        # set with `gcloud run services update --set-env-vars`.
        mode = (os.environ.get("LQABR_DISPATCH_MODE", "").strip()
                or str(config.get("dispatch.mode", "per_lead")))
        raw_size = os.environ.get("LQABR_BATCH_SIZE", "").strip()
        try:
            batch_size = int(raw_size) if raw_size else int(
                config.get("dispatch.batch_size", 20))
        except ValueError:
            batch_size = int(config.get("dispatch.batch_size", 20))

        return cls(client=client, audit=audit,
                   gateway_version=str(config.get("gateway.version", "0.1.0")),
                   include_routing_basis=bool(
                       config.get("dispatch.include_routing_basis", False)),
                   mode=mode, batch_size=batch_size)
