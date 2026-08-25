"""Step 2 — Resolve the owning endpoint.

Rev 3, Functional Requirements by Step:

    END GOAL   Deterministically decide which downstream agent owns this
               trigger and resolve a valid endpoint for it.
    INPUTS     the event array from Step 1; agents_registry.yaml (property +
               value -> owning agent); config.yaml (protocols, timeouts).
    PROCESSING Iterate the batched array. Filter on propertyValue and discard
               events that do not match a routing condition. De-duplicate
               using eventId where attemptNumber > 0. Ignore events whose
               changeSource is an agent write-back. Match the remaining events
               to their owning agent, mint the trigger_id, and validate that
               the required endpoint exists and is enabled.
    OUTPUTS    A resolved {trigger_id, target agent, endpoint} reference passed
               forward. If no endpoint matches, a routing error is raised — a
               lead is never silently dropped.

Two words carry the weight here. **Discard** is a normal outcome: the
subscription fires on every value of the watched property, so most events are
simply not the routing condition and are dropped with a logged reason (D-01 —
the free tier has no custom trigger, so value filtering lives here). **Routing
error** is a failure: the event *did* match, but the agent that owns it has no
usable endpoint. That is raised, never swallowed.

No lead-profile data is read from the event beyond ``objectId``, which is a
record identifier, not profile data.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence

#: Namespace for deterministic trigger ids. Fixed constant: a redelivery of
#: the same HubSpot event mints the same trigger_id, so the dedupe store is a
#: performance optimisation rather than the only thing standing between us and
#: a double outreach.
_TRIGGER_NAMESPACE = uuid.UUID("6f0b6f9a-6c3f-5c2e-9a1d-4d7e2b8f1c30")


class DiscardReason(str, Enum):
    """Why an event did not become a trigger. Every discard is logged."""

    NOT_ROUTING_CONDITION = "not_routing_condition"      # value isn't the one we act on
    NO_MATCHING_ROUTE = "no_matching_route"              # property isn't routed at all
    DUPLICATE_EVENT = "duplicate_event"                  # redelivery, already processed
    SELF_TRIGGER_LOOP = "self_trigger_loop"              # agent's own write-back
    AGENT_WRITEBACK = "agent_writeback"                   # changeSource in ignore list
    MALFORMED_EVENT = "malformed_event"                  # unusable envelope


class RoutingError(RuntimeError):
    """A matched event has no usable endpoint.

    Rev 3: *If no endpoint matches, a routing error is raised — a lead is never
    silently dropped.* Carries the routing basis so the audit record and the
    exception say the same thing.
    """

    def __init__(self, message: str, *, event_id: Optional[str] = None,
                 object_id: Optional[str] = None, agent: Optional[str] = None,
                 route_id: Optional[str] = None, property_name: Optional[str] = None,
                 property_value: Optional[str] = None,
                 trigger_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.event_id = event_id
        self.object_id = object_id
        self.agent = agent
        self.route_id = route_id
        self.property_name = property_name
        self.property_value = property_value
        # The trigger id is minted before the endpoint is resolved, so an
        # undeliverable event still has one. Carrying it here is what keeps the
        # failure traceable by the same id as a success — the records that most
        # need tracing are exactly these.
        self.trigger_id = trigger_id

    def as_dict(self) -> Dict[str, Any]:
        return {
            "error": str(self),
            "event_id": self.event_id,
            "object_id": self.object_id,
            "agent": self.agent,
            "route_id": self.route_id,
            "property_name": self.property_name,
            "property_value": self.property_value,
        }


class RegistryError(RuntimeError):
    """agents_registry.yaml is malformed or internally inconsistent."""


# =====================================================================
# Inbound event
# =====================================================================
@dataclass(frozen=True)
class HubSpotEvent:
    """One element of the Step 1 payload.

    Rev 3, "What the trigger emits":

        objectId, propertyName, propertyValue, subscriptionType, portalId,
        eventId, occurredAt, attemptNumber, changeSource
    """

    object_id: Optional[str]
    property_name: Optional[str]
    property_value: Optional[str]
    subscription_type: Optional[str]
    portal_id: Optional[int]
    event_id: Optional[str]
    occurred_at: Optional[int]
    attempt_number: int
    change_source: Optional[str]
    raw_index: int = 0

    @classmethod
    def from_payload(cls, payload: Dict[str, Any], index: int = 0) -> "HubSpotEvent":
        def _text(key: str) -> Optional[str]:
            value = payload.get(key)
            return None if value is None else str(value)

        try:
            attempt = int(payload.get("attemptNumber", 0) or 0)
        except (TypeError, ValueError):
            attempt = 0
        try:
            portal = payload.get("portalId")
            portal_id = int(portal) if portal is not None else None
        except (TypeError, ValueError):
            portal_id = None
        try:
            occurred = payload.get("occurredAt")
            occurred_at = int(occurred) if occurred is not None else None
        except (TypeError, ValueError):
            occurred_at = None

        return cls(
            object_id=_text("objectId"),
            property_name=_text("propertyName"),
            property_value=_text("propertyValue"),
            subscription_type=_text("subscriptionType"),
            portal_id=portal_id,
            event_id=_text("eventId"),
            occurred_at=occurred_at,
            attempt_number=attempt,
            change_source=_text("changeSource"),
            raw_index=index,
        )

    @property
    def is_redelivery(self) -> bool:
        """Rev 3: *attemptNumber > 0 indicates a redelivery.*"""
        return self.attempt_number > 0

    def audit_fields(self) -> Dict[str, Any]:
        """The routing basis, and nothing else. Safe to log."""
        return {
            "event_id": self.event_id,
            "object_id": self.object_id,
            "property_name": self.property_name,
            "property_value": self.property_value,
            "subscription_type": self.subscription_type,
            "occurred_at": self.occurred_at,
            "attempt_number": self.attempt_number,
            "change_source": self.change_source,
        }


# =====================================================================
# Registry
# =====================================================================
@dataclass(frozen=True)
class AgentEntry:
    key: str
    endpoint_env: str
    enabled: bool = True
    description: str = ""
    #: Properties this agent writes back to HubSpot — the loop guard's input.
    writes: frozenset = field(default_factory=frozenset)


@dataclass(frozen=True)
class Route:
    id: str
    agent: str
    property_name: Optional[str] = None
    values: Sequence[str] = ()
    match: str = "exact_ci"
    subscription_type: Optional[str] = None
    description: str = ""

    def matches_property(self, event: HubSpotEvent) -> bool:
        if self.subscription_type and event.subscription_type != self.subscription_type:
            return False
        if self.property_name is None:
            # A creation route: no property to compare.
            return True
        return event.property_name == self.property_name

    def matches_value(self, event: HubSpotEvent) -> bool:
        """Is this event's value the routing condition?"""
        if self.match == "non_empty":
            value = event.property_value
            return value is not None and value.strip() != ""
        if self.match == "any" or not self.values:
            return True
        value = event.property_value
        if value is None:
            return False
        if self.match == "exact":
            return value in self.values
        return value.strip().lower() in {str(v).strip().lower() for v in self.values}


class AgentRegistry:
    """The declarative property+value -> agent map.

    Rev 3: *onboarding a new agent means editing this file, not the routing
    code.* Endpoints are read from the environment at resolve time, not at load
    time, so the same registry ships to dev / stage / prod unchanged.
    """

    def __init__(self, agents: Dict[str, AgentEntry], routes: List[Route],
                 environ: Optional[Dict[str, str]] = None) -> None:
        self._agents = agents
        self._routes = routes
        self._environ = environ  # None -> os.environ, read live
        self._validate()

    # ------------------------------------------------------------- loading
    @classmethod
    def from_document(cls, document: Dict[str, Any],
                      environ: Optional[Dict[str, str]] = None) -> "AgentRegistry":
        raw_agents = document.get("agents") or {}
        if not isinstance(raw_agents, dict) or not raw_agents:
            raise RegistryError("agents_registry.yaml defines no agents")

        agents: Dict[str, AgentEntry] = {}
        for key, spec in raw_agents.items():
            spec = spec or {}
            endpoint_env = spec.get("endpoint_env")
            if not endpoint_env:
                raise RegistryError(f"agent '{key}' has no endpoint_env")
            agents[key] = AgentEntry(
                key=key,
                endpoint_env=str(endpoint_env),
                enabled=bool(spec.get("enabled", True)),
                description=str(spec.get("description", "")),
                writes=frozenset(str(w) for w in (spec.get("writes") or [])),
            )

        raw_routes = document.get("routes") or []
        if not isinstance(raw_routes, list) or not raw_routes:
            raise RegistryError("agents_registry.yaml defines no routes")

        routes: List[Route] = []
        for index, spec in enumerate(raw_routes):
            spec = spec or {}
            agent = spec.get("agent")
            if not agent:
                raise RegistryError(f"route at index {index} has no agent")
            routes.append(Route(
                id=str(spec.get("id") or f"route-{index}"),
                agent=str(agent),
                property_name=(str(spec["property"]) if spec.get("property") else None),
                values=tuple(str(v) for v in (spec.get("values") or ())),
                match=str(spec.get("match", "exact_ci")),
                subscription_type=(str(spec["subscription_type"])
                                   if spec.get("subscription_type") else None),
                description=str(spec.get("description", "")).strip(),
            ))
        return cls(agents, routes, environ=environ)

    def _validate(self) -> None:
        for route in self._routes:
            if route.agent not in self._agents:
                raise RegistryError(
                    f"route '{route.id}' points at unknown agent '{route.agent}' — "
                    f"known agents: {sorted(self._agents)}"
                )
            if route.match not in {"exact", "exact_ci", "any", "non_empty"}:
                raise RegistryError(
                    f"route '{route.id}' has unknown match mode '{route.match}'")
            if route.match not in ("any", "non_empty") and not route.values and route.property_name:
                raise RegistryError(
                    f"route '{route.id}' watches '{route.property_name}' but declares no "
                    "values — a subscription fires on every value, so this would route "
                    "everything. Use match: any if that is intended."
                )

    # ------------------------------------------------------------ resolving
    @property
    def routes(self) -> List[Route]:
        return list(self._routes)

    @property
    def agents(self) -> Dict[str, AgentEntry]:
        return dict(self._agents)

    def match(self, event: HubSpotEvent) -> Optional[Route]:
        """First matching route wins, in declaration order."""
        for route in self._routes:
            if route.matches_property(event) and route.matches_value(event):
                return route
        return None

    def matches_property_only(self, event: HubSpotEvent) -> Optional[Route]:
        """A route watches this property, but the value wasn't the condition.

        Separating this from ``match`` is what lets the process log say
        "not_routing_condition" (expected, high volume) instead of
        "no_matching_route" (unexpected, worth an alert).
        """
        for route in self._routes:
            if route.matches_property(event):
                return route
        return None

    def endpoint_for(self, agent_key: str) -> str:
        """Resolve and validate the agent's endpoint. Raises ``RoutingError``."""
        entry = self._agents.get(agent_key)
        if entry is None:
            raise RoutingError(f"unknown agent '{agent_key}' in registry", agent=agent_key)
        if not entry.enabled:
            raise RoutingError(f"agent '{agent_key}' is disabled in the registry",
                               agent=agent_key)
        environ = self._environ if self._environ is not None else os.environ
        url = (environ.get(entry.endpoint_env) or "").strip()
        if not url:
            raise RoutingError(
                f"agent '{agent_key}' endpoint is not configured "
                f"({entry.endpoint_env} is unset)", agent=agent_key)
        if not url.startswith(("http://", "https://")):
            raise RoutingError(
                f"agent '{agent_key}' endpoint '{url}' is not an http(s) URL",
                agent=agent_key)
        return url

    def health(self) -> Dict[str, Any]:
        """Per-agent endpoint resolvability, for ``/readyz``."""
        report: Dict[str, Any] = {}
        for key in self._agents:
            try:
                self.endpoint_for(key)
                report[key] = {"ready": True}
            except RoutingError as exc:
                report[key] = {"ready": False, "reason": str(exc)}
        return report


# =====================================================================
# De-duplication
# =====================================================================
class DedupeStore:
    """TTL + LRU set of processed eventIds.

    Rev 3 Step 2: *De-duplicate using eventId where attemptNumber > 0.* So a
    first delivery is never checked against the store — only a redelivery is.

    An id is reserved **before** the hand-off and released again if the
    hand-off fails (see ``forget``). Reserving after success instead left a
    window as wide as the dispatch: HubSpot gives up at 5s and redelivers,
    and every redelivery arriving before the first hand-off returned found an
    empty store and dispatched again. Observed live 05-Aug-2026 —
    ``peak_in_flight: 4`` on a 30s dispatch.

    Nothing but a dispatched event goes in. Two consequences, both deliberate:

    * Failures (routing error, dispatch failure) are not remembered, so
      HubSpot's next redelivery gets a real second attempt instead of being
      deduped into oblivion. That asymmetry is the whole reason this is a store
      the caller writes to rather than something the router does implicitly.
    * Discards are not remembered either. Re-evaluating a discard on redelivery
      costs a dictionary lookup, whereas storing them would let the
      high-volume "not the routing condition" stream evict real dispatch
      records from a bounded LRU — and an evicted dispatch record means a
      second outreach to a lead already contacted.

    In-memory and therefore per-instance: two Cloud Run instances do not share
    it. Acceptable at MVP because trigger ids are deterministic per event and
    agents are idempotent on trigger id; see README "Known limits" for the
    shared-store follow-up.
    """

    def __init__(self, ttl_seconds: int = 900, max_entries: int = 10000,
                 clock: Any = time.time) -> None:
        self._ttl = max(1, int(ttl_seconds))
        self._max = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.Lock()
        self._seen: "OrderedDict[str, float]" = OrderedDict()

    def _purge(self, now: float) -> None:
        cutoff = now - self._ttl
        while self._seen:
            key, stamped = next(iter(self._seen.items()))
            if stamped >= cutoff:
                break
            self._seen.popitem(last=False)
        while len(self._seen) > self._max:
            self._seen.popitem(last=False)

    def seen(self, event_id: Optional[str]) -> bool:
        if not event_id:
            return False
        with self._lock:
            now = self._clock()
            self._purge(now)
            return event_id in self._seen

    def remember(self, event_id: Optional[str]) -> None:
        if not event_id:
            return
        with self._lock:
            now = self._clock()
            self._seen[event_id] = now
            self._seen.move_to_end(event_id)
            self._purge(now)

    def forget(self, event_id: Optional[str]) -> None:
        """Release a reservation made before a hand-off that then failed.

        The caller reserves an eventId *before* dispatching so that a HubSpot
        redelivery arriving while the first hand-off is still in flight is
        deduped rather than dispatched a second time. If the hand-off then
        fails, the reservation must come back out, or HubSpot's retry would be
        deduped into oblivion and the lead never contacted.
        """
        if not event_id:
            return
        with self._lock:
            self._seen.pop(event_id, None)

    def __len__(self) -> int:  # pragma: no cover - introspection
        with self._lock:
            return len(self._seen)


# =====================================================================
# Router output
# =====================================================================
@dataclass(frozen=True)
class RoutingDecision:
    """A resolved {trigger_id, target agent, endpoint} reference (Step 2 output)."""

    trigger_id: str
    agent: str
    endpoint: str
    route_id: str
    event_id: Optional[str]
    object_id: Optional[str]
    property_name: Optional[str]
    property_value: Optional[str]
    occurred_at: Optional[int]
    attempt_number: int
    #: Forwarded verbatim on the research hand-off, which carries the HubSpot
    #: event under HubSpot's own field names. None for every other route only
    #: because nothing else reads them.
    subscription_type: Optional[str] = None
    portal_id: Optional[int] = None
    change_source: Optional[str] = None
    #: Set only on audience-expanded research hand-offs (Rev 5): the blog Ticket
    #: id the lead was selected for. None for every other route.
    summary_ref_id: Optional[str] = None
    #: Set alongside summary_ref_id on research hand-offs: the blog post's
    #: publication timestamp, which is the key the central MCP reads a blog
    #: summary by. Without it the agent has the ticket id but no way to fetch
    #: the summary through the MCP. None for every other route.
    blog_published_at: Optional[str] = None

    def audit_fields(self) -> Dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "agent": self.agent,
            "endpoint": self.endpoint,
            "route_id": self.route_id,
            "event_id": self.event_id,
            "object_id": self.object_id,
            "property_name": self.property_name,
            "property_value": self.property_value,
            "attempt_number": self.attempt_number,
        }


@dataclass(frozen=True)
class DiscardedEvent:
    reason: DiscardReason
    event_id: Optional[str]
    object_id: Optional[str]
    property_name: Optional[str]
    property_value: Optional[str]
    route_id: Optional[str] = None
    detail: Optional[str] = None

    def audit_fields(self) -> Dict[str, Any]:
        return {
            "reason": self.reason.value,
            "event_id": self.event_id,
            "object_id": self.object_id,
            "property_name": self.property_name,
            "property_value": self.property_value,
            "route_id": self.route_id,
            "detail": self.detail,
        }


@dataclass
class RoutingResult:
    decisions: List[RoutingDecision] = field(default_factory=list)
    discarded: List[DiscardedEvent] = field(default_factory=list)
    errors: List[RoutingError] = field(default_factory=list)

    @property
    def event_count(self) -> int:
        return len(self.decisions) + len(self.discarded) + len(self.errors)

    def summary(self) -> Dict[str, Any]:
        by_reason: Dict[str, int] = {}
        for item in self.discarded:
            by_reason[item.reason.value] = by_reason.get(item.reason.value, 0) + 1
        return {
            "events_received": self.event_count,
            "routed": len(self.decisions),
            "discarded": len(self.discarded),
            "routing_errors": len(self.errors),
            "discards_by_reason": by_reason,
        }


# =====================================================================
# The router
# =====================================================================
class Router:
    """Step 2, in order: filter -> dedupe -> loop guard -> match -> mint -> validate."""

    def __init__(self, registry: AgentRegistry, dedupe: Optional[DedupeStore] = None,
                 loop_guard_mode: str = "self_trigger",
                 ignored_change_sources: Sequence[str] = ("API",),
                 dedupe_enabled: bool = True) -> None:
        self._registry = registry
        # `dedupe or DedupeStore()` would be a bug: DedupeStore defines
        # __len__, so an empty store is falsy and the caller's store — with the
        # TTL and cap from config.yaml — would be silently replaced by a
        # default one.
        self._dedupe = dedupe if dedupe is not None else DedupeStore()
        self._loop_guard_mode = loop_guard_mode
        self._ignored_change_sources = {str(s).upper() for s in ignored_change_sources}
        self._dedupe_enabled = dedupe_enabled

    @property
    def registry(self) -> AgentRegistry:
        return self._registry

    @property
    def dedupe(self) -> DedupeStore:
        return self._dedupe

    # ------------------------------------------------------------ minting
    @staticmethod
    def mint_trigger_id(event: HubSpotEvent) -> str:
        """Mint the trigger_id. Rev 3: minted by the gateway, not HubSpot.

        Derived from (portalId, eventId) so it is stable across redeliveries —
        the same HubSpot event always yields the same trigger id, which makes
        the audit trail joinable and downstream idempotency possible. Falls
        back to a random id when HubSpot sent no eventId, because an
        unidentifiable event must still be traceable.
        """
        if event.event_id:
            seed = f"lqabr:{event.portal_id or 0}:{event.event_id}"
        else:
            # No eventId to key on. Derive from the event's own content rather
            # than uuid4(), so a redelivery still mints the same id — a random
            # id here would defeat downstream idempotency precisely for the
            # events we can least identify.
            seed = (f"lqabr:{event.portal_id or 0}:noeid:{event.object_id}:"
                    f"{event.property_name}:{event.property_value}:{event.occurred_at}")
        return f"trg-{uuid.uuid5(_TRIGGER_NAMESPACE, seed).hex[:24]}"

    # ------------------------------------------------------------- routing
    def route_batch(self, payload: Sequence[Dict[str, Any]]) -> RoutingResult:
        """Route one inbound batch. Never raises for a single bad event.

        Routing errors are collected rather than thrown so that one
        unconfigured agent cannot discard the other 99 events in the batch.
        The caller decides the HTTP response; ``result.errors`` being non-empty
        is what makes it a failure.
        """
        result = RoutingResult()
        #: eventIds routed earlier in THIS batch. The dedupe store is written
        #: only after a hand-off succeeds, so without this a duplicate inside
        #: one array would be dispatched twice.
        batch_seen: set = set()

        for index, raw in enumerate(payload):
            try:
                event = HubSpotEvent.from_payload(raw, index=index)
            except (AttributeError, TypeError) as exc:
                result.discarded.append(DiscardedEvent(
                    reason=DiscardReason.MALFORMED_EVENT, event_id=None, object_id=None,
                    property_name=None, property_value=None, detail=str(exc)))
                continue

            # objectId is not a routing input — the gateway still routes on
            # property + value alone — but since D-05 it IS what the agent
            # resolves the lead with, so an event without one is undeliverable.
            # That is checked at step 4, after the cheap rejections, so a
            # malformed event that was never going to route is discarded rather
            # than raised.

            # --- 1. filter on propertyValue ------------------------------
            # First, per Rev 3's ordering, and load-bearing: the subscription
            # fires on every value, so this is the high-volume path. Doing it
            # before de-duplication keeps the discard stream out of the dedupe
            # store entirely.
            route = self._registry.match(event)
            if route is None:
                watched = self._registry.matches_property_only(event)
                if watched is not None:
                    # The subscription fires on every value; this isn't the one.
                    reason, detail = DiscardReason.NOT_ROUTING_CONDITION, (
                        f"'{event.property_value}' is not a routing condition for "
                        f"'{event.property_name}' (route {watched.id} acts on "
                        f"{list(watched.values)})")
                    route_id: Optional[str] = watched.id
                else:
                    reason, detail = DiscardReason.NO_MATCHING_ROUTE, (
                        f"no route watches property '{event.property_name}' / "
                        f"subscription '{event.subscription_type}'")
                    route_id = None
                result.discarded.append(DiscardedEvent(
                    reason=reason, event_id=event.event_id, object_id=event.object_id,
                    property_name=event.property_name, property_value=event.property_value,
                    route_id=route_id, detail=detail))
                continue

            # --- 2. de-duplicate redeliveries ----------------------------
            # Only redeliveries are checked (Rev 3: "where attemptNumber > 0").
            # `batch_seen` additionally catches the same eventId twice inside a
            # single array, which the store cannot: it is only written after a
            # hand-off succeeds.
            if self._dedupe_enabled and event.event_id:
                already = (event.is_redelivery and self._dedupe.seen(event.event_id)) \
                    or event.event_id in batch_seen
                if already:
                    result.discarded.append(DiscardedEvent(
                        reason=DiscardReason.DUPLICATE_EVENT, event_id=event.event_id,
                        object_id=event.object_id, property_name=event.property_name,
                        property_value=event.property_value, route_id=route.id,
                        detail=f"redelivery (attemptNumber={event.attempt_number}) or "
                               "repeat within the batch of an eventId already processed"))
                    continue
                batch_seen.add(event.event_id)

            # --- 3. loop guard -------------------------------------------
            if self._loop_guard_mode == "all_api" and \
                    (event.change_source or "").upper() in self._ignored_change_sources:
                result.discarded.append(DiscardedEvent(
                    reason=DiscardReason.AGENT_WRITEBACK, event_id=event.event_id,
                    object_id=event.object_id, property_name=event.property_name,
                    property_value=event.property_value, route_id=route.id,
                    detail=f"changeSource={event.change_source} is an agent write-back"))
                continue

            # "so the system does not trigger itself": an agent is never woken
            # by the property it writes itself. Scoped per route rather than
            # globally on changeSource=API, because three of the four routes
            # exist precisely to catch agent write-backs (D-03).
            if self._loop_guard_mode == "self_trigger" and event.property_name:
                owner = self._registry.agents.get(route.agent)
                if owner is not None and event.property_name in owner.writes:
                    result.discarded.append(DiscardedEvent(
                        reason=DiscardReason.SELF_TRIGGER_LOOP, event_id=event.event_id,
                        object_id=event.object_id, property_name=event.property_name,
                        property_value=event.property_value, route_id=route.id,
                        detail=f"agent '{route.agent}' writes '{event.property_name}' "
                               "itself — refusing to trigger it with its own write-back"))
                    continue

            # --- 4. mint + validate the endpoint -------------------------
            trigger_id = self.mint_trigger_id(event)

            # The agent looks the lead up by record id (D-05). Without one it
            # would receive a trigger it cannot act on, so this is a routing
            # error — 503, HubSpot redelivers — not a discard, which would
            # return 200 and lose the lead silently.
            if event.object_id is None:
                error = RoutingError(
                    "event matched a route but carries no objectId — the agent "
                    "cannot resolve the lead without it",
                    event_id=event.event_id, object_id=None, agent=route.agent,
                    route_id=route.id, property_name=event.property_name,
                    property_value=event.property_value, trigger_id=trigger_id)
                if event.event_id:
                    batch_seen.discard(event.event_id)
                result.errors.append(error)
                continue

            try:
                endpoint = self._registry.endpoint_for(route.agent)
            except RoutingError as exc:
                # Matched, but undeliverable. Never remembered in the dedupe
                # store: a redelivery after the endpoint is configured must be
                # allowed to succeed. The trigger id travels with the error so
                # the failure is traceable by the same id a success would use.
                exc.event_id = event.event_id
                exc.object_id = event.object_id
                exc.route_id = route.id
                exc.property_name = event.property_name
                exc.property_value = event.property_value
                exc.trigger_id = trigger_id
                if event.event_id:
                    batch_seen.discard(event.event_id)
                result.errors.append(exc)
                continue

            result.decisions.append(RoutingDecision(
                trigger_id=trigger_id, agent=route.agent, endpoint=endpoint,
                route_id=route.id, event_id=event.event_id, object_id=event.object_id,
                property_name=event.property_name, property_value=event.property_value,
                occurred_at=event.occurred_at, attempt_number=event.attempt_number,
                subscription_type=event.subscription_type, portal_id=event.portal_id,
                change_source=event.change_source,
            ))

        return result
