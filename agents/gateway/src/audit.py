"""Step 3 — Record the run. Also Step 7 (observability), which is this module.

Rev 3, Step 3:

    END GOAL   Produce a durable, auditable record of every HubSpot->agent
               handoff so runs can be traced, and so the basis of every routing
               decision is reviewable after the fact.
    INPUTS     trigger_id, a generated run ID, the resolved agent/endpoint, the
               propertyName and propertyValue that drove the decision, a
               timestamp, and the audit verbosity level from config.yaml.
    PROCESSING Log run ID + trigger_id for every request. Log the routing
               decision together with the property and value it was based on.
               Capture metrics for the HubSpot->agent handoff.
    OUTPUTS    An append-only audit entry plus a metrics record. No lead-profile
               data is logged — only the trigger reference and the decision
               inputs.

Rev 3 Step 7 makes the point that matters: *In an agentic system the decision is
the thing worth auditing; the rest follows from telemetry.* The agentgateway
sidecar already produces the telemetry — access logs, Prometheus counters, OTel
spans. What it cannot produce is **why this agent and not another one**. That is
what this module writes, and why the process stream always carries
``property_name`` and ``property_value`` next to the chosen agent.

``GatewayAudit`` is a façade over ``lib/soloai/audit_hooks``: the business logic
calls intention-named methods, the hooks own the four streams and the
profile-data guard.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from router import DiscardedEvent, RoutingDecision, RoutingError, RoutingResult

from soloai.audit_hooks import AuditHooks, Stream, new_run_id


@dataclass
class HandoffMetrics:
    """The HubSpot->agent handoff, counted. Step 3: *Capture metrics.*

    Deliberately tiny and in-process: the sidecar exposes Prometheus metrics
    for the network view, while these are the decision-level counters that only
    the gateway can produce. Exposed on ``/metrics`` as JSON for the MVP.
    """

    requests: int = 0
    events_received: int = 0
    routed: int = 0
    discarded: int = 0
    routing_errors: int = 0
    dispatched_ok: int = 0
    dispatched_failed: int = 0
    dispatch_latency_ms_total: float = 0.0
    retries: int = 0
    by_agent: Dict[str, int] = field(default_factory=dict)
    by_discard_reason: Dict[str, int] = field(default_factory=dict)
    #: The ingress pipeline runs in a threadpool with up to 10 concurrent
    #: requests, so every mutation and snapshot goes through this lock —
    #: unguarded `+=` across threads loses counts.
    lock: threading.Lock = field(default_factory=threading.Lock,
                                 repr=False, compare=False)

    @property
    def dispatch_latency_ms_mean(self) -> Optional[float]:
        total = self.dispatched_ok + self.dispatched_failed
        if not total:
            return None
        return round(self.dispatch_latency_ms_total / total, 2)

    def as_dict(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "requests": self.requests,
                "events_received": self.events_received,
                "routed": self.routed,
                "discarded": self.discarded,
                "routing_errors": self.routing_errors,
                "dispatched_ok": self.dispatched_ok,
                "dispatched_failed": self.dispatched_failed,
                "dispatch_retries": self.retries,
                "dispatch_latency_ms_mean": self.dispatch_latency_ms_mean,
                "by_agent": dict(self.by_agent),
                "by_discard_reason": dict(self.by_discard_reason),
            }


class GatewayAudit:
    """Step 3's write path, and the gateway's whole observability surface."""

    def __init__(self, hooks: AuditHooks, verbose_discards: Optional[bool] = None) -> None:
        self._hooks = hooks
        self.metrics = HandoffMetrics()
        # At standard level, discards are summarised by reason; at verbose,
        # every discarded event gets its own process record. A subscription
        # that fires on every value produces a lot of legitimate discards, so
        # per-event logging is opt-in rather than the default.
        self._verbose_discards = (
            hooks.level == "verbose" if verbose_discards is None else verbose_discards
        )

    # ------------------------------------------------------------- lifecycle
    @staticmethod
    def new_run_id() -> str:
        return new_run_id()

    def record_startup(self, runtime: Dict[str, Any], registry_health: Dict[str, Any],
                       ingress_path: str, concurrency_limit: int,
                       chunk_size_hint: Optional[int]) -> None:
        """System stream: startup and configuration state.

        Also writes the token/model exclusion, because Rev 3 wants that stream
        recorded as an explicit N/A rather than left absent.
        """
        self._hooks.system(
            "gateway_startup",
            runtime=runtime,
            ingress_path=ingress_path,
            concurrency_limit=concurrency_limit,
            chunk_size_hint=chunk_size_hint,
            agents=registry_health,
            audit_level=self._hooks.level,
            streams=self._hooks.streams,
        )
        self._hooks.token_model_exclusion()

        unready = {k: v for k, v in registry_health.items() if not v.get("ready")}
        if unready:
            # A configuration problem belongs on the system stream loudly at
            # startup, not as a surprise 503 on the first live lead.
            self._hooks.system("agent_endpoints_unresolved", agents=unready)

    def record_config_error(self, message: str, detail: Optional[str] = None) -> None:
        self._hooks.system("configuration_error", error=message, detail=detail)

    # --------------------------------------------------------------- ingress
    def record_ingress(self, run_id: str, *, source_ip: Optional[str], endpoint: str,
                       method: str, event_count: int, payload_bytes: int,
                       signature_verified: bool, received_at: Optional[float] = None,
                       concurrency: Optional[Dict[str, int]] = None) -> None:
        """Audit stream: *where the call came from, when it arrived, which
        endpoint it went to* — Step 2's audit row."""
        with self.metrics.lock:
            self.metrics.requests += 1
            self.metrics.events_received += event_count
        self._hooks.audit(
            "hubspot_ingress_received",
            run_id=run_id,
            direction="inbound",
            source="hubspot",
            source_ip=source_ip,
            endpoint=endpoint,
            method=method,
            event_count=event_count,
            payload_bytes=payload_bytes,
            signature_verified=signature_verified,
            received_at=received_at or time.time(),
        )
        if concurrency:
            # System stream: ingress concurrency against the throttle limit of 10.
            self._hooks.system("ingress_concurrency", run_id=run_id, **concurrency)

    def record_ingress_rejected(self, run_id: str, *, reason: str, status_code: int,
                                source_ip: Optional[str] = None,
                                endpoint: Optional[str] = None) -> None:
        self._hooks.audit(
            "hubspot_ingress_rejected", run_id=run_id, direction="inbound",
            source_ip=source_ip, endpoint=endpoint, status=status_code, reason=reason,
        )

    # -------------------------------------------------------------- decision
    def record_decision(self, run_id: str, decision: RoutingDecision) -> None:
        """Process stream: the routing decision **and the property and value it
        was based on**. This is the record Rev 3 Step 7 is written for."""
        with self.metrics.lock:
            self.metrics.routed += 1
            self.metrics.by_agent[decision.agent] = \
                self.metrics.by_agent.get(decision.agent, 0) + 1
        self._hooks.process(
            "routing_decision",
            run_id=run_id,
            trigger_id=decision.trigger_id,
            decision="route",
            agent=decision.agent,
            endpoint=decision.endpoint,
            route_id=decision.route_id,
            property_name=decision.property_name,
            property_value=decision.property_value,
            event_id=decision.event_id,
            object_id=decision.object_id,
            attempt_number=decision.attempt_number,
            basis="property + value matched a route in agents_registry.yaml",
        )

    def record_discards(self, run_id: str, discarded: Sequence[DiscardedEvent]) -> None:
        """Process stream: *events discarded and why*."""
        if not discarded:
            return
        by_reason: Dict[str, int] = {}
        for item in discarded:
            by_reason[item.reason.value] = by_reason.get(item.reason.value, 0) + 1
            with self.metrics.lock:
                self.metrics.discarded += 1
                self.metrics.by_discard_reason[item.reason.value] = \
                    self.metrics.by_discard_reason.get(item.reason.value, 0) + 1
            if self._verbose_discards:
                self._hooks.process("event_discarded", run_id=run_id,
                                    decision="discard", **item.audit_fields())
        if not self._verbose_discards:
            self._hooks.process("events_discarded", run_id=run_id, decision="discard",
                                discarded=len(discarded), by_reason=by_reason)

    def record_routing_errors(self, run_id: str, errors: Sequence[RoutingError]) -> None:
        """Process + system streams: matched but undeliverable.

        Rev 3: *a lead is never silently dropped.* This is the not-silent part —
        an error record per event, and a system record because an unresolvable
        endpoint is a configuration fault, not a data condition.
        """
        for error in errors:
            with self.metrics.lock:
                self.metrics.routing_errors += 1
            self._hooks.process("routing_error", run_id=run_id,
                                trigger_id=error.trigger_id, decision="error",
                                **error.as_dict())
        if errors:
            self._hooks.system("routing_errors_in_batch", run_id=run_id,
                               count=len(errors),
                               agents=sorted({e.agent for e in errors if e.agent}))

    # -------------------------------------------------------------- dispatch
    def record_dispatch(self, run_id: str, decision: RoutingDecision, *,
                        ok: bool, status_code: Optional[int], latency_ms: float,
                        attempts: int, payload_size_bytes: int,
                        error: Optional[str] = None) -> None:
        """Audit + process streams for Step 4.

        Audit gets the network facts (destination, status, latency, retry
        count); process gets the protocol conversion and the payload size,
        which is how the light-payload claim becomes measurable.
        """
        retries = max(0, attempts - 1)
        with self.metrics.lock:
            self.metrics.retries += retries
            self.metrics.dispatch_latency_ms_total += latency_ms
            if ok:
                self.metrics.dispatched_ok += 1
            else:
                self.metrics.dispatched_failed += 1

        self._hooks.audit(
            "agent_dispatch" if ok else "agent_dispatch_failed",
            run_id=run_id,
            trigger_id=decision.trigger_id,
            direction="outbound",
            agent=decision.agent,
            endpoint=decision.endpoint,
            status=status_code,
            latency_ms=latency_ms,
            retry_count=retries,
            error=error,
        )
        self._hooks.process(
            "protocol_conversion",
            run_id=run_id,
            trigger_id=decision.trigger_id,
            conversion="https_ingress -> a2a_message_send",
            payload_size_bytes=payload_size_bytes,
            payload_contents="trigger_id + correlation ids only",
            agent=decision.agent,
            route_id=decision.route_id,
            outcome="dispatched" if ok else "failed",
        )
        self._hooks.system(
            "dispatch_resources", run_id=run_id, trigger_id=decision.trigger_id,
            agent=decision.agent,
        )

    def record_batch_dispatch(self, run_id: str, head: RoutingDecision, *,
                             batch_id: str, batch_size: int, ok: bool,
                             status_code: Optional[int], latency_ms: float,
                             attempts: int, payload_size_bytes: int,
                             error: Optional[str] = None) -> None:
        """Step 4 for a grouped hand-off: N leads in one call.

        Same three streams as ``record_dispatch``, with the batch id in place
        of a single trigger id and ``batch_size`` on the record — which is what
        makes "20 routed, 2 dispatched" visible at a glance. Still ids only:
        the object ids stay in the payload, never in the log line.
        """
        retries = max(0, attempts - 1)
        with self.metrics.lock:
            self.metrics.retries += retries
            self.metrics.dispatch_latency_ms_total += latency_ms
            if ok:
                self.metrics.dispatched_ok += 1
            else:
                self.metrics.dispatched_failed += 1

        self._hooks.audit(
            "agent_batch_dispatch" if ok else "agent_batch_dispatch_failed",
            run_id=run_id,
            trigger_id=batch_id,
            direction="outbound",
            agent=head.agent,
            endpoint=head.endpoint,
            batch_size=batch_size,
            status=status_code,
            latency_ms=latency_ms,
            retry_count=retries,
            error=error,
        )
        self._hooks.process(
            "protocol_conversion",
            run_id=run_id,
            trigger_id=batch_id,
            conversion="https_ingress -> a2a_message_send",
            payload_size_bytes=payload_size_bytes,
            payload_contents="batch_id + object_ids only",
            agent=head.agent,
            route_id=head.route_id,
            batch_size=batch_size,
            outcome="dispatched" if ok else "failed",
        )
        self._hooks.system(
            "dispatch_resources", run_id=run_id, trigger_id=batch_id,
            agent=head.agent, batch_size=batch_size,
        )

    def record_audience(self, run_id: str, decision: RoutingDecision, *,
                        outcome: Optional[Any] = None,
                        error: Optional[str] = None) -> None:
        """Rev 5 Step 4 — one blog ticket resolved into N leads (ids/counts only)."""
        if error is not None:
            self._hooks.process(
                "audience_resolution_failed", run_id=run_id,
                trigger_id=decision.trigger_id, object_id=decision.object_id,
                error=error)
            return
        self._hooks.process(
            "audience_resolved", run_id=run_id, trigger_id=decision.trigger_id,
            summary_ref_id=outcome.summary_ref_id, industry=outcome.industry,
            company_count=outcome.company_count, lead_count=outcome.lead_count,
            zero_audience=(outcome.lead_count == 0))

    # ---------------------------------------------------------------- wrap-up
    def record_run_summary(self, run_id: str, result: RoutingResult,
                           dispatched_ok: int, dispatched_failed: int,
                           duration_ms: float) -> Dict[str, Any]:
        summary = {
            **result.summary(),
            "dispatched_ok": dispatched_ok,
            "dispatched_failed": dispatched_failed,
            "duration_ms": round(duration_ms, 2),
        }
        self._hooks.process("run_summary", run_id=run_id, **summary)
        return summary

    # ---------------------------------------------------- vapi call report
    def record_call_report(self, run_id: str, *, source_ip: Optional[str],
                           endpoint: str, payload_bytes: int,
                           call_id: Optional[str],
                           hubspot_contact_id: Optional[str],
                           secret_verified: bool) -> None:
        """Audit stream: a Vapi end-of-call report arrived at the gateway.

        Correlation ids only. The transcript is never recorded: the audit
        hooks scan every payload for profile fields and a real conversation
        would trip ``ProfileFieldLeak`` — correctly, because the gateway is
        not a place lead content should ever come to rest.

        ``hubspot_contact_id`` may legitimately be None; txtv falls back to a
        phone lookup. It is logged as absent, never treated as an error.
        """
        self._hooks.audit(
            "vapi_call_report_received",
            run_id=run_id,
            direction="inbound",
            source="vapi",
            source_ip=source_ip,
            endpoint=endpoint,
            payload_bytes=payload_bytes,
            call_id=call_id,
            hubspot_contact_id=hubspot_contact_id,
            contact_id_present=hubspot_contact_id is not None,
            secret_verified=secret_verified,
            received_at=time.time(),
        )

    def record_call_report_relayed(self, run_id: str, *, target: str,
                                   status_code: int, latency_ms: float,
                                   authenticated: bool) -> None:
        """Audit stream: the outbound leg to the text/voice agent.

        Deliberately mirrors ``record_dispatch`` so the two outbound paths —
        A2A trigger and report relay — read the same way in the log.
        """
        self._hooks.audit(
            "vapi_call_report_relayed",
            run_id=run_id,
            direction="outbound",
            agent="text_voice",
            endpoint=target,
            status=status_code,
            latency_ms=round(latency_ms, 2),
            authenticated=authenticated,
        )

    def record_call_report_rejected(self, run_id: str, *, reason: str,
                                    status_code: int,
                                    source_ip: Optional[str] = None,
                                    endpoint: Optional[str] = None) -> None:
        self._hooks.audit(
            "vapi_call_report_rejected", run_id=run_id, direction="inbound",
            source="vapi", source_ip=source_ip, endpoint=endpoint,
            status=status_code, reason=reason,
        )

    def record_exception(self, run_id: str, exc: BaseException,
                         where: str) -> None:
        """System stream: *exceptions*."""
        self._hooks.system("unhandled_exception", run_id=run_id, where=where,
                           exception_type=type(exc).__name__, error=str(exc))

    # ------------------------------------------------------------ passthrough
    @property
    def hooks(self) -> AuditHooks:
        return self._hooks

    def emit(self, stream: Stream, event: str, **fields: Any) -> Optional[Dict[str, Any]]:
        return self._hooks.emit(stream, event, **fields)

    def records(self) -> List[Dict[str, Any]]:  # pragma: no cover - test aid
        return list(self._hooks.records)
