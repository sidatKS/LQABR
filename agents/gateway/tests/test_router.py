"""Step 2 — routing. The gateway's only real decision, so the bulk of the tests.

Covers the five things Rev 3 Step 2 asks router.py to do, plus the two failure
modes the design explicitly names: never route on the wrong value, and never
silently drop a lead.
"""

from __future__ import annotations

import pytest

from conftest import gw_router, make_event

DiscardReason = gw_router.DiscardReason
RoutingError = gw_router.RoutingError


# ===================================================================== events
class TestHubSpotEvent:
    def test_parses_the_documented_payload(self):
        event = gw_router.HubSpotEvent.from_payload(make_event())
        assert event.object_id == "701"
        assert event.property_name == "lqabr_email_status"
        assert event.property_value == "OPENED"
        assert event.portal_id == 246777241
        assert event.attempt_number == 0
        assert event.change_source == "CRM"
        assert not event.is_redelivery

    def test_attempt_number_above_zero_is_a_redelivery(self):
        event = gw_router.HubSpotEvent.from_payload(make_event(attempt_number=2))
        assert event.is_redelivery

    def test_survives_junk_in_numeric_fields(self):
        """HubSpot is the source, but a proxy or replay tool may not be.
        A bad attemptNumber must not take down the batch."""
        raw = make_event()
        raw.update({"attemptNumber": "not-a-number", "portalId": None, "occurredAt": "x"})
        event = gw_router.HubSpotEvent.from_payload(raw)
        assert event.attempt_number == 0
        assert event.portal_id is None
        assert event.occurred_at is None

    def test_audit_fields_carry_the_basis_and_nothing_else(self):
        fields = gw_router.HubSpotEvent.from_payload(make_event()).audit_fields()
        assert fields["property_name"] == "lqabr_email_status"
        assert fields["property_value"] == "OPENED"
        assert "email" not in fields and "phone" not in fields


# =================================================================== registry
class TestAgentRegistry:
    def test_the_shipped_registry_loads_and_validates(self, registry):
        """The real agents_registry.yaml is a deliverable — read it here first."""
        assert set(registry.agents) == {"email", "voice", "scheduling"}
        assert [r.id for r in registry.routes] == [
            "R1-contact-created", "R2-decision-maker",
            "R3-email-opened", "R4-voice-completed",
        ]

    def test_the_four_documented_mappings_resolve(self, registry):
        """Rev 3 page 1: decision-maker -> email, opened -> voice,
        completed -> scheduling, plus Created -> email."""
        cases = [
            (make_event("decision_maker", "true"), "email", "R2-decision-maker"),
            (make_event("lqabr_email_status", "OPENED"), "voice", "R3-email-opened"),
            (make_event("lqabr_voice_status", "COMPLETED"), "scheduling", "R4-voice-completed"),
            (make_event(None, None, subscription_type="contact.creation"),
             "email", "R1-contact-created"),
        ]
        for payload, expected_agent, expected_route in cases:
            route = registry.match(gw_router.HubSpotEvent.from_payload(payload))
            assert route is not None, payload
            assert (route.agent, route.id) == (expected_agent, expected_route)

    def test_values_match_case_insensitively(self, registry):
        for value in ("opened", "OPENED", "Opened"):
            route = registry.match(gw_router.HubSpotEvent.from_payload(
                make_event("lqabr_email_status", value)))
            assert route is not None and route.agent == "voice"

    def test_unknown_agent_in_a_route_is_rejected_at_load(self, agent_env):
        with pytest.raises(gw_router.RegistryError, match="unknown agent"):
            gw_router.AgentRegistry.from_document({
                "agents": {"email": {"endpoint_env": "X"}},
                "routes": [{"id": "r", "agent": "nope", "property": "p", "values": ["v"]}],
            }, environ=agent_env)

    def test_a_route_with_no_values_is_rejected_at_load(self, agent_env):
        """A subscription fires on every value; a route with no values would
        route all of them. That is a config bug, not a feature."""
        with pytest.raises(gw_router.RegistryError, match="declares no values"):
            gw_router.AgentRegistry.from_document({
                "agents": {"email": {"endpoint_env": "LQABR_EMAIL_AGENT_URL"}},
                "routes": [{"id": "r", "agent": "email", "property": "decision_maker"}],
            }, environ=agent_env)

    def test_missing_endpoint_raises_routing_error(self, registry_document):
        registry = gw_router.AgentRegistry.from_document(registry_document, environ={})
        with pytest.raises(RoutingError, match="not configured"):
            registry.endpoint_for("email")

    def test_disabled_agent_raises_routing_error(self, registry_document, agent_env):
        registry_document["agents"]["voice"]["enabled"] = False
        registry = gw_router.AgentRegistry.from_document(registry_document, environ=agent_env)
        with pytest.raises(RoutingError, match="disabled"):
            registry.endpoint_for("voice")

    def test_non_http_endpoint_is_rejected(self, registry_document):
        registry = gw_router.AgentRegistry.from_document(
            registry_document, environ={"LQABR_EMAIL_AGENT_URL": "email-agent.internal"})
        with pytest.raises(RoutingError, match="not an http"):
            registry.endpoint_for("email")

    def test_health_reports_per_agent_readiness(self, registry_document, agent_env):
        partial = dict(agent_env)
        partial.pop("LQABR_SCHEDULING_AGENT_URL")
        registry = gw_router.AgentRegistry.from_document(registry_document, environ=partial)
        health = registry.health()
        assert health["email"]["ready"] is True
        assert health["scheduling"]["ready"] is False


# ================================================================== trigger id
class TestTriggerId:
    def test_minted_by_the_gateway_not_taken_from_hubspot(self, router):
        """Rev 3: the payload has no trigger id — the gateway mints it."""
        payload = make_event()
        assert "trigger_id" not in payload
        result = router.route_batch([payload])
        assert result.decisions[0].trigger_id.startswith("trg-")

    def test_stable_for_the_same_event(self):
        """A redelivery of the same event must mint the same trigger id, so the
        audit trail joins and downstream idempotency is possible."""
        event = gw_router.HubSpotEvent.from_payload(make_event(event_id="evt-42"))
        again = gw_router.HubSpotEvent.from_payload(
            make_event(event_id="evt-42", attempt_number=3))
        assert gw_router.Router.mint_trigger_id(event) == \
            gw_router.Router.mint_trigger_id(again)

    def test_distinct_per_event(self):
        a = gw_router.HubSpotEvent.from_payload(make_event(event_id="evt-a"))
        b = gw_router.HubSpotEvent.from_payload(make_event(event_id="evt-b"))
        assert gw_router.Router.mint_trigger_id(a) != gw_router.Router.mint_trigger_id(b)

    def test_still_minted_when_hubspot_sends_no_event_id(self):
        raw = make_event()
        raw.pop("eventId")
        event = gw_router.HubSpotEvent.from_payload(raw)
        assert gw_router.Router.mint_trigger_id(event).startswith("trg-")

    def test_stable_without_an_event_id_too(self):
        """Regression: a uuid4 fallback gave the same event a different trigger
        id on every redelivery, defeating downstream idempotency for exactly
        the events we can least identify."""
        raw = make_event()
        raw.pop("eventId")
        first = gw_router.Router.mint_trigger_id(
            gw_router.HubSpotEvent.from_payload(dict(raw)))
        raw["attemptNumber"] = 2
        second = gw_router.Router.mint_trigger_id(
            gw_router.HubSpotEvent.from_payload(dict(raw)))
        assert first == second


# ================================================================= filtering
class TestValueFiltering:
    """D-01: the free tier cannot filter by value, so the gateway must."""

    def test_routes_only_the_routing_condition(self, router):
        result = router.route_batch([make_event("lqabr_email_status", "OPENED")])
        assert len(result.decisions) == 1
        assert result.decisions[0].agent == "voice"

    @pytest.mark.parametrize("value", ["SENT", "DELIVERED", "BOUNCED", "CLICKED", ""])
    def test_discards_every_other_value_of_a_watched_property(self, router, value):
        result = router.route_batch([make_event("lqabr_email_status", value,
                                                event_id=f"evt-{value}")])
        assert result.decisions == []
        assert result.discarded[0].reason is DiscardReason.NOT_ROUTING_CONDITION
        # The discard names the route it failed, so the log explains itself.
        assert result.discarded[0].route_id == "R3-email-opened"

    def test_decision_maker_false_is_not_a_trigger(self, router):
        result = router.route_batch([make_event("decision_maker", "false")])
        assert result.decisions == []
        assert result.discarded[0].reason is DiscardReason.NOT_ROUTING_CONDITION

    def test_unwatched_property_is_a_different_reason(self, router):
        """Distinguishing these two matters: one is expected volume, the other
        means someone added a subscription nobody routed."""
        result = router.route_batch([make_event("lifecyclestage", "customer")])
        assert result.discarded[0].reason is DiscardReason.NO_MATCHING_ROUTE

    def test_routing_never_reads_profile_fields(self, router):
        """A payload carrying profile data must not change the outcome —
        the gateway routes on property + value only."""
        clean = router.route_batch([make_event(event_id="evt-clean")])
        polluted_payload = make_event(event_id="evt-polluted")
        polluted_payload.update({"email": "a@b.test", "phone": "+15550100",
                                 "annual_revenue": 1_000_000})
        polluted = router.route_batch([polluted_payload])
        assert polluted.decisions[0].agent == clean.decisions[0].agent
        # and none of it is carried forward
        assert "email" not in polluted.decisions[0].audit_fields()


# ================================================================ de-duplication
class TestDeduplication:
    def test_first_delivery_is_never_deduped(self, router):
        """Rev 3: de-duplicate *where attemptNumber > 0*. A first delivery is
        not checked at all — two distinct events can share nothing else."""
        router.dedupe.remember("evt-1")
        result = router.route_batch([make_event(attempt_number=0, event_id="evt-1")])
        assert len(result.decisions) == 1

    def test_redelivery_of_a_processed_event_is_discarded(self, router):
        first = router.route_batch([make_event(event_id="evt-9")])
        router.dedupe.remember(first.decisions[0].event_id)
        again = router.route_batch([make_event(event_id="evt-9", attempt_number=1)])
        assert again.decisions == []
        assert again.discarded[0].reason is DiscardReason.DUPLICATE_EVENT

    def test_redelivery_after_a_failure_is_allowed_through(self, registry_document, agent_env):
        """The store is only written on a terminal outcome. An event that
        errored is not remembered, so HubSpot's retry gets a real second
        attempt instead of being deduped into oblivion."""
        registry = gw_router.AgentRegistry.from_document(registry_document, environ={})
        router = gw_router.Router(registry=registry)
        first = router.route_batch([make_event(event_id="evt-fail")])
        assert len(first.errors) == 1          # matched, no endpoint
        registry_ok = gw_router.AgentRegistry.from_document(registry_document,
                                                           environ=agent_env)
        router._registry = registry_ok         # endpoint configured in the meantime
        retry = router.route_batch([make_event(event_id="evt-fail", attempt_number=1)])
        assert len(retry.decisions) == 1

    def test_discards_are_NOT_remembered(self, router):
        """Regression. Remembering discards looks like a cheap optimisation and
        is in fact a duplicate-outreach bug: the subscription fires on every
        value, so discards are the high-volume path and would evict real
        dispatch records from the bounded LRU. Re-evaluating a discard costs a
        dict lookup; losing a dispatch record costs a second call to a lead."""
        router.route_batch([make_event("lqabr_email_status", "SENT", event_id="evt-s")])
        assert not router.dedupe.seen("evt-s")
        again = router.route_batch([make_event("lqabr_email_status", "SENT",
                                               event_id="evt-s", attempt_number=1)])
        assert again.discarded[0].reason is DiscardReason.NOT_ROUTING_CONDITION

    def test_a_flood_of_discards_cannot_evict_a_real_dispatch(self, registry):
        """The failure this ordering prevents, end to end: one dispatched lead,
        then thousands of non-routing events, then a redelivery of the first."""
        store = gw_router.DedupeStore(ttl_seconds=900, max_entries=50)
        router = gw_router.Router(registry=registry, dedupe=store)
        dispatched = router.route_batch([make_event(event_id="evt-REAL")]).decisions[0]
        store.remember(dispatched.event_id)          # what server.py does on success

        for i in range(500):
            router.route_batch([make_event("lqabr_email_status", "CLICKED",
                                           event_id=f"noise-{i}")])

        assert store.seen("evt-REAL"), "a real dispatch was evicted by discard noise"
        redelivery = router.route_batch([make_event(event_id="evt-REAL", attempt_number=1)])
        assert redelivery.decisions == []
        assert redelivery.discarded[0].reason is DiscardReason.DUPLICATE_EVENT

    def test_the_injected_store_is_actually_used(self, registry):
        """Regression: `dedupe or DedupeStore()` silently drops the caller's
        store, because DedupeStore defines __len__ and an empty one is falsy —
        which made dedupe.ttl_seconds / max_entries in config.yaml dead."""
        store = gw_router.DedupeStore(ttl_seconds=1, max_entries=3)
        router = gw_router.Router(registry=registry, dedupe=store)
        assert router.dedupe is store

    def test_the_same_event_id_twice_in_one_batch_dispatches_once(self, router):
        """The store is only written after a hand-off succeeds, so an in-batch
        repeat cannot be caught by it."""
        result = router.route_batch([
            make_event(event_id="evt-dup", attempt_number=1),
            make_event(event_id="evt-dup", attempt_number=1),
        ])
        assert len(result.decisions) == 1
        assert result.discarded[0].reason is DiscardReason.DUPLICATE_EVENT

    def test_store_expires_entries(self):
        clock = [1000.0]
        store = gw_router.DedupeStore(ttl_seconds=60, clock=lambda: clock[0])
        store.remember("evt-old")
        assert store.seen("evt-old")
        clock[0] += 61
        assert not store.seen("evt-old")

    def test_store_is_capped(self):
        store = gw_router.DedupeStore(ttl_seconds=900, max_entries=5)
        for index in range(20):
            store.remember(f"evt-{index}")
        assert len(store) <= 5
        assert store.seen("evt-19") and not store.seen("evt-0")


# =================================================================== loop guard
class TestLoopGuard:
    def test_an_agent_is_not_woken_by_its_own_write_back(self, registry_document, agent_env):
        """"so the system does not trigger itself" — the voice agent writes
        lqabr_voice_status, so it must never be triggered by it."""
        # Point the winning route at the agent that writes the property, which
        # is the misconfiguration the guard exists to catch.
        registry_document["routes"] = [{
            "id": "R-loop", "subscription_type": "contact.propertyChange",
            "property": "lqabr_voice_status", "values": ["COMPLETED"], "agent": "voice",
        }]
        registry = gw_router.AgentRegistry.from_document(registry_document, environ=agent_env)
        router = gw_router.Router(registry=registry, loop_guard_mode="self_trigger")
        result = router.route_batch([make_event("lqabr_voice_status", "COMPLETED")])
        assert result.decisions == []
        assert result.discarded[0].reason is DiscardReason.SELF_TRIGGER_LOOP

    def test_default_mode_keeps_agent_written_triggers_alive(self, router):
        """D-03. lqabr_email_status is written by the Email agent via the API,
        and routes to the Voice agent. A blanket changeSource=API drop would
        kill this path — the very path Rev 3 requires."""
        result = router.route_batch([
            make_event("lqabr_email_status", "OPENED", change_source="API")])
        assert len(result.decisions) == 1
        assert result.decisions[0].agent == "voice"

    def test_all_api_mode_is_available_and_does_drop_them(self, registry):
        """The literal Rev 3 reading, kept behind config for review."""
        router = gw_router.Router(registry=registry, loop_guard_mode="all_api",
                                  ignored_change_sources=["API"])
        result = router.route_batch([
            make_event("lqabr_email_status", "OPENED", change_source="API")])
        assert result.decisions == []
        assert result.discarded[0].reason is DiscardReason.AGENT_WRITEBACK

    def test_human_edits_are_always_routed(self, registry):
        router = gw_router.Router(registry=registry, loop_guard_mode="all_api")
        result = router.route_batch([make_event("decision_maker", "true",
                                                change_source="CRM")])
        assert len(result.decisions) == 1


# ================================================================ routing errors
class TestRoutingErrors:
    def test_matched_but_unconfigured_endpoint_is_an_error_not_a_discard(
            self, registry_document):
        """Rev 3: *If no endpoint matches, a routing error is raised — a lead is
        never silently dropped.*"""
        registry = gw_router.AgentRegistry.from_document(registry_document, environ={})
        router = gw_router.Router(registry=registry)
        result = router.route_batch([make_event()])
        assert result.decisions == [] and result.discarded == []
        assert len(result.errors) == 1
        error = result.errors[0]
        assert error.trigger_id and error.trigger_id.startswith("trg-")  # traceable
        assert error.agent == "voice"
        assert error.property_name == "lqabr_email_status"   # basis is carried
        assert error.event_id == "evt-1"

    def test_one_broken_agent_does_not_take_the_batch_down(
            self, registry_document, agent_env):
        partial = dict(agent_env)
        partial.pop("LQABR_SCHEDULING_AGENT_URL")
        registry = gw_router.AgentRegistry.from_document(registry_document, environ=partial)
        router = gw_router.Router(registry=registry)
        result = router.route_batch([
            make_event("lqabr_voice_status", "COMPLETED", event_id="evt-sched"),
            make_event("lqabr_email_status", "OPENED", event_id="evt-voice"),
            make_event("decision_maker", "true", event_id="evt-email"),
        ])
        assert len(result.errors) == 1
        assert {d.agent for d in result.decisions} == {"voice", "email"}


# ======================================================================= batches
class TestBatches:
    def test_a_full_hundred_event_batch(self, router):
        """Rev 3: up to 100 events per request."""
        events = [make_event("lqabr_email_status", "OPENED", event_id=f"evt-{i}",
                             object_id=str(700 + i)) for i in range(100)]
        result = router.route_batch(events)
        assert len(result.decisions) == 100
        assert len({d.trigger_id for d in result.decisions}) == 100

    def test_mixed_batch_is_sorted_into_outcomes(self, router):
        result = router.route_batch([
            make_event("lqabr_email_status", "OPENED", event_id="e1"),
            make_event("lqabr_email_status", "BOUNCED", event_id="e2"),
            make_event("decision_maker", "true", event_id="e3"),
            make_event("lifecyclestage", "lead", event_id="e4"),
            make_event(None, None, subscription_type="contact.creation", event_id="e5"),
        ])
        assert len(result.decisions) == 3
        assert len(result.discarded) == 2
        summary = result.summary()
        assert summary["events_received"] == 5
        assert summary["routed"] == 3
        assert summary["discards_by_reason"] == {
            "not_routing_condition": 1, "no_matching_route": 1}

    def test_event_without_object_id_is_a_routing_error_not_a_discard(self, router):
        """D-05: the agent resolves the lead by record id, so an event without
        one is undeliverable. It must be a routing error (503, HubSpot
        redelivers), never a discard (200), which would lose the lead silently.
        """
        raw = make_event()
        raw.pop("objectId")
        result = router.route_batch([raw])
        assert result.decisions == [] and result.discarded == []
        assert len(result.errors) == 1
        error = result.errors[0]
        assert "objectId" in str(error)
        assert error.agent == "voice"                      # it did match a route
        assert error.trigger_id.startswith("trg-")         # and is still traceable

    def test_empty_batch_is_simply_empty(self, router):
        assert router.route_batch([]).event_count == 0
