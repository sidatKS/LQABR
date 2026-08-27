"""Step 4 — the hand-off. Trigger id only, and provably so.

The central assertion of this file is negative: whatever else the dispatch does,
the bytes on the wire contain a trigger id and correlation metadata, and nothing
that could identify or describe a lead.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
import requests

from conftest import gw_dispatch, make_event
from soloai.protocols.a2a import (
    ALLOWED_METADATA_KEYS,
    A2AClient,
    PayloadGuardError,
)


@pytest.fixture()
def decision(router):
    return router.route_batch([make_event("email_status", "OPENED")]).decisions[0]


@pytest.fixture()
def generic_decision(decision):
    """A decision for a hypothetical agent NOT in {research, email, voice}.

    All three of the registry's real agents are on the HubSpot-shaped
    metadata branch as of 25-Aug-2026 (research/email/voice, in that order --
    see dispatch.py::_metadata). The plain generic branch below it -- the
    original Rev-3 {trigger_id, object_id, run_id, source, gateway_version}
    shape, plus the include_routing_basis opt-in -- is therefore unreachable
    through the real registry today, but it is still real code (it is what a
    5th agent onboarded without HubSpot-shaped treatment would get), so it
    still needs a decision that actually reaches it to stay covered."""
    return replace(decision, agent="future-agent",
                   endpoint="https://future-agent.example.test/a2a")


def _client(session, **kw):
    return A2AClient(session=session, max_retries=kw.pop("max_retries", 2),
                     backoff_seconds=0, sleep=lambda _s: None, **kw)


# ============================================================== the wire format
class TestA2AMessage:
    def test_the_message_body_is_the_trigger_id(self):
        body = A2AClient.build_message("trg-abc123")
        assert body["jsonrpc"] == "2.0"
        assert body["method"] == "message/send"
        parts = body["params"]["message"]["parts"]
        assert parts == [{"kind": "text", "text": "trg-abc123"}]

    def test_a_dispatch_without_a_trigger_id_is_refused(self):
        with pytest.raises(PayloadGuardError, match="must carry a trigger_id"):
            A2AClient.build_message("")

    @pytest.mark.parametrize("field,value", [
        ("email", "lead@example.test"),
        ("phone", "+15550100"),
        ("company", "Example Corp"),
        ("lead_profile", {"full_name": "A Person"}),
        ("annual_revenue", 1000),
    ])
    def test_attaching_profile_data_is_refused(self, field, value):
        """Rev 3: *No profile payload is attached.* Enforced at the boundary,
        so the guarantee survives a future maintainer's convenience."""
        with pytest.raises(PayloadGuardError, match="non-trigger metadata"):
            A2AClient.build_message("trg-1", {field: value})

    def test_only_correlation_ids_and_the_record_id_are_permitted(self):
        """Correlation ids plus object_id (D-05), and nothing else.

        propertyName/propertyValue are required in the LOG by FR-7 but must not
        be on the wire: a property value is config-controlled, so subscribing to
        a profile property in the portal would turn it into an email address.
        """
        assert ALLOWED_METADATA_KEYS == {
            "trigger_id", "object_id", "run_id", "route_id", "source",
            "gateway_version",
            # dispatch.mode = grouped — plural ids for a batched hand-off.
            # Widened deliberately on 06-Aug-2026; this assertion exists so
            # that widening is always a reviewed change and never a drift.
            "batch_id", "object_ids", "batch_size", "trigger_ids",
            # Rev 5 audience resolution: the blog Ticket id travels on the wire.
            "summary_ref_id",
            # Blog-ticket research hand-off (audience disabled): the HubSpot
            # event forwarded VERBATIM under HubSpot's original names, plus the
            # correlation id as camelCase ``triggerId``. Emitted ONLY for
            # agent == "research", and for that route the top-level mirrors are
            # suppressed -- one record id, one spelling (25-Aug-2026).
            #
            # propertyValue IS forwarded here, reversing the rule above for this
            # route only: on a ticket.propertyChange the value is blog copy the
            # research agent was written to consume, not a lead field. It stays
            # refused for every other agent (see the parametrized test below,
            # which still rejects the snake_case property_value).
            "objectId", "propertyName", "propertyValue", "subscriptionType",
            "portalId", "eventId", "occurredAt", "attemptNumber",
            "changeSource", "triggerId",
        }

    @pytest.mark.parametrize("field", ["property_name", "property_value"])
    def test_the_routing_basis_is_still_refused_on_the_wire(self, field):
        with pytest.raises(PayloadGuardError):
            A2AClient.build_message("trg-1", {field: "anything"})

    def test_no_profile_field_names_appear_anywhere_in_the_payload(
            self, decision, fake_session_factory, audit):
        session = fake_session_factory()
        dispatcher = gw_dispatch.Dispatcher(_client(session), audit)
        dispatcher.dispatch(decision, "run-1")
        wire = json.dumps(session.last_body).lower()
        for forbidden in ("email\"", "phone", "revenue", "job_title", "full_name",
                          "company\"", "industry", "linkedin"):
            assert forbidden not in wire, f"{forbidden} reached the wire"

    def test_the_payload_stays_small(self, decision, fake_session_factory, audit):
        """"keeps the gateway footprint small" — with a number on it."""
        session = fake_session_factory()
        dispatcher = gw_dispatch.Dispatcher(_client(session), audit)
        result = dispatcher.dispatch(decision, "run-1")
        assert result.payload_size_bytes < 1024

    def test_default_payload_is_the_trigger_id_the_record_id_and_correlation(
            self, generic_decision, fake_session_factory, audit):
        """Covers the plain generic branch -- unreachable through the real
        registry now that research/email/voice are all HubSpot-shaped, see
        the generic_decision fixture above."""
        session = fake_session_factory()
        gw_dispatch.Dispatcher(_client(session), audit).dispatch(generic_decision, "run-1")
        metadata = session.last_body["params"]["metadata"]
        assert set(metadata) == {"trigger_id", "object_id", "run_id", "source",
                                 "gateway_version"}

    def test_the_record_id_is_the_one_the_agent_can_actually_resolve(
            self, generic_decision, fake_session_factory, audit):
        """D-05. This is the whole point of the change: the agent does
        GET /crm/v3/objects/contacts/<object_id> and has the lead. Neither the
        trigger_id nor HubSpot's eventId is stored anywhere in the CRM.
        Generic branch only -- research/email/voice send objectId (HubSpot's
        own spelling) inside params.metadata instead, see test_dispatch.py's
        agent-specific tests below."""
        session = fake_session_factory()
        gw_dispatch.Dispatcher(_client(session), audit).dispatch(generic_decision, "run-1")
        assert session.last_body["params"]["metadata"]["object_id"] == "701"

    def test_opting_into_the_routing_basis_adds_only_the_route_id(
            self, generic_decision, fake_session_factory, audit):
        """include_routing_basis only affects the generic branch -- research,
        email and voice never look at it (they send propertyName/propertyValue
        unconditionally, as HubSpot sent them, regardless of this setting)."""
        session = fake_session_factory()
        gw_dispatch.Dispatcher(_client(session), audit,
                               include_routing_basis=True).dispatch(generic_decision, "run-1")
        metadata = session.last_body["params"]["metadata"]
        assert metadata["route_id"] == "R3-email-opened"
        assert "property_name" not in metadata and "property_value" not in metadata

    def test_correlation_headers_let_the_sidecar_log_be_joined(
            self, generic_decision, fake_session_factory, audit):
        """config/agentgateway.yaml reads these off the request; without them
        its access-log trigger_id/run_id fields are always null.

        KNOWN GAP, not introduced here: x-lqabr-run-id is read off
        metadata['run_id'] in a2a.py::send_trigger, and the HubSpot-shaped
        branch (research/email/voice) has never put run_id in metadata --
        true for research since Rev 5, and now equally true for email and
        voice since 25-Aug-2026. For those three agents this header is
        always empty in practice. Flagged, not fixed here."""
        session = fake_session_factory()
        gw_dispatch.Dispatcher(_client(session), audit).dispatch(generic_decision, "run-1")
        headers = session.calls[-1]["headers"]
        assert headers["x-lqabr-trigger-id"] == generic_decision.trigger_id
        assert headers["x-lqabr-run-id"] == "run-1"

    def test_payload_size_measures_the_json_not_the_repr(
            self, decision, fake_session_factory, audit):
        session = fake_session_factory()
        result = gw_dispatch.Dispatcher(_client(session), audit).dispatch(decision, "run-1")
        assert result.payload_size_bytes == len(
            json.dumps(session.last_body).encode("utf-8"))


# ==================================================================== delivery
class TestDispatch:
    def test_delivers_to_the_endpoint_the_router_resolved(
            self, decision, fake_session_factory, audit):
        session = fake_session_factory()
        gw_dispatch.Dispatcher(_client(session), audit).dispatch(decision, "run-1")
        assert session.calls[0]["url"] == "https://voice-agent.example.test/a2a"

    def test_a_successful_hand_off_reports_status_and_latency(
            self, decision, fake_session_factory, audit):
        result = gw_dispatch.Dispatcher(
            _client(fake_session_factory()), audit).dispatch(decision, "run-1")
        assert result.ok and result.status_code == 200
        assert result.latency_ms >= 0 and result.attempts == 1 and result.retries == 0

    def test_response_body_carries_no_lead_data(
            self, decision, fake_session_factory, audit):
        result = gw_dispatch.Dispatcher(
            _client(fake_session_factory()), audit).dispatch(decision, "run-1")
        assert set(result.as_dict()) == {
            "trigger_id", "agent", "dispatched", "status", "latency_ms",
            "retries", "error"}

    def test_email_gets_the_hubspot_event_verbatim_no_mirrors(
            self, router, fake_session_factory, audit):
        """Email 25-Aug-2026, Saroja's instruction: the email hand-off carries
        exactly the HubSpot event under HubSpot's own field names, the same
        treatment research already gets -- nothing gateway-invented, and none
        of the old top-level object_id/objectId/trigger_id mirrors."""
        email_decision = router.route_batch(
            [make_event("lead_context", "notes about the lead")]).decisions[0]
        assert email_decision.agent == "email"
        session = fake_session_factory()
        gw_dispatch.Dispatcher(_client(session), audit).dispatch(email_decision, "run-1")
        body = session.last_body
        assert set(body) == {"jsonrpc", "id", "method", "params"}, (
            "no top-level mirrors for email any more")
        metadata = body["params"]["metadata"]
        assert metadata["objectId"] == "701"
        assert metadata["propertyName"] == "lead_context"
        assert metadata["propertyValue"] == "notes about the lead"
        assert metadata["subscriptionType"] == "contact.propertyChange"
        assert "object_id" not in metadata and "trigger_id" not in metadata

    def test_voice_gets_the_hubspot_event_verbatim_no_mirrors(
            self, router, fake_session_factory, audit):
        """Voice 25-Aug-2026, same instruction as email: the voice hand-off
        carries exactly the HubSpot event under HubSpot's own field names --
        nothing gateway-invented, and none of the old top-level
        object_id/objectId/trigger_id mirrors."""
        voice_decision = router.route_batch(
            [make_event("email_status", "OPENED")]).decisions[0]
        assert voice_decision.agent == "voice"
        session = fake_session_factory()
        gw_dispatch.Dispatcher(_client(session), audit).dispatch(voice_decision, "run-1")
        body = session.last_body
        assert set(body) == {"jsonrpc", "id", "method", "params"}, (
            "no top-level mirrors for voice any more")
        metadata = body["params"]["metadata"]
        assert metadata["objectId"] == "701"
        assert metadata["propertyName"] == "email_status"
        assert metadata["propertyValue"] == "OPENED"
        assert metadata["subscriptionType"] == "contact.propertyChange"
        assert "object_id" not in metadata and "trigger_id" not in metadata


class TestRetries:
    def test_retries_a_transport_failure_then_succeeds(
            self, decision, fake_session_factory, fake_response_factory, audit):
        session = fake_session_factory([
            requests.ConnectionError("connection reset"),
            fake_response_factory(200),
        ])
        result = gw_dispatch.Dispatcher(_client(session), audit).dispatch(decision, "run-1")
        assert result.ok and result.attempts == 2 and result.retries == 1

    def test_retries_a_5xx(self, decision, fake_session_factory,
                           fake_response_factory, audit):
        session = fake_session_factory([
            fake_response_factory(503, text="upstream unavailable"),
            fake_response_factory(200),
        ])
        result = gw_dispatch.Dispatcher(_client(session), audit).dispatch(decision, "run-1")
        assert result.ok and result.attempts == 2

    def test_does_not_retry_a_4xx(self, decision, fake_session_factory,
                                  fake_response_factory, audit):
        """A rejected message will be rejected again; retrying only delays the
        audit record that says so."""
        session = fake_session_factory(fake_response_factory(400, text="bad request"))
        result = gw_dispatch.Dispatcher(_client(session), audit).dispatch(decision, "run-1")
        assert not result.ok and result.attempts == 1
        assert len(session.calls) == 1

    def test_gives_up_after_max_retries(self, decision, fake_session_factory, audit):
        session = fake_session_factory([requests.ConnectionError("down")] * 3)
        result = gw_dispatch.Dispatcher(
            _client(session, max_retries=2), audit).dispatch(decision, "run-1")
        assert not result.ok and result.attempts == 3
        assert "transport error" in result.error

    def test_an_a2a_protocol_error_is_terminal(
            self, decision, fake_session_factory, fake_response_factory, audit):
        session = fake_session_factory(fake_response_factory(
            200, payload={"error": {"code": -32600, "message": "invalid request"}}))
        result = gw_dispatch.Dispatcher(_client(session), audit).dispatch(decision, "run-1")
        assert not result.ok and result.attempts == 1
        assert "A2A error" in result.error

    def test_a_non_json_2xx_is_still_a_success(
            self, decision, fake_session_factory, fake_response_factory, audit):
        """An agent that acknowledges with an empty body has still accepted the
        trigger. The gateway's responsibility ends at the acknowledgement."""
        session = fake_session_factory(fake_response_factory(
            202, raise_exc=ValueError("no json")))
        result = gw_dispatch.Dispatcher(_client(session), audit).dispatch(decision, "run-1")
        assert result.ok


class TestBatchDispatch:
    def test_dispatches_every_decision(self, router, fake_session_factory, audit):
        result = router.route_batch([
            make_event("email_status", "OPENED", event_id="e1"),
            make_event("lead_context", "ctx", event_id="e2"),
        ])
        session = fake_session_factory()
        outcomes = gw_dispatch.Dispatcher(_client(session), audit).dispatch_all(
            result.decisions, "run-1")
        assert len(outcomes) == 2 and all(o.ok for o in outcomes)
        assert {c["url"] for c in session.calls} == {
            "https://voice-agent.example.test/a2a",
            "https://email-agent.example.test/a2a",
        }

    def test_one_failure_does_not_stop_the_others(
            self, router, fake_session_factory, fake_response_factory, audit):
        result = router.route_batch([
            make_event("email_status", "OPENED", event_id="e1"),
            make_event("lead_context", "ctx", event_id="e2"),
        ])
        session = fake_session_factory([
            fake_response_factory(400, text="nope"),
            fake_response_factory(200),
        ])
        outcomes = gw_dispatch.Dispatcher(_client(session), audit).dispatch_all(
            result.decisions, "run-1")
        assert [o.ok for o in outcomes] == [False, True]


class TestDispatchAuditing:
    def test_a_failed_hand_off_is_still_audited(
            self, decision, fake_session_factory, audit, hooks):
        session = fake_session_factory([requests.ConnectionError("down")] * 3)
        gw_dispatch.Dispatcher(_client(session), audit).dispatch(decision, "run-1")
        failures = [r for r in hooks.records if r["event"] == "agent_dispatch_failed"]
        assert failures and failures[0]["retry_count"] == 2
        assert audit.metrics.dispatched_failed == 1

    def test_built_from_config_uses_the_configured_timeouts(self, config, audit):
        dispatcher = gw_dispatch.Dispatcher.from_config(config, audit)
        client = dispatcher._client
        assert client._timeout == config.get("protocols.a2a.timeout_seconds")
        assert client._max_retries == config.get("protocols.a2a.max_retries")


class TestGroupedDispatch:
    """dispatch.mode = grouped — N leads in one hand-off per agent."""

    def _dispatcher(self, session, audit, batch_size=20):
        return gw_dispatch.Dispatcher(_client(session), audit,
                                      mode="grouped", batch_size=batch_size)

    def test_twenty_leads_two_agents_becomes_two_calls(
            self, router, fake_session_factory, audit):
        events = ([make_event("lead_context", "ctx", object_id=str(700 + i),
                              event_id=f"e{i}") for i in range(14)]
                  + [make_event("email_status", "OPENED", object_id=str(800 + i),
                                event_id=f"o{i}") for i in range(6)])
        result = router.route_batch(events)
        assert len(result.decisions) == 20

        session = fake_session_factory()
        outcomes = self._dispatcher(session, audit).dispatch_grouped(
            result.decisions, "run-1")

        assert len(session.calls) == 2, "one call per agent, not one per lead"
        assert [o.batch_size for o in outcomes] == [14, 6]
        assert all(o.ok for o in outcomes)

        by_url = {c["url"]: c["json"] for c in session.calls}
        email = by_url["https://email-agent.example.test/a2a"]
        assert len(email["params"]["metadata"]["object_ids"]) == 14
        assert email["params"]["metadata"]["batch_size"] == 14
        assert email["object_ids"] == email["params"]["metadata"]["object_ids"]

    def test_a_group_larger_than_batch_size_is_chunked(
            self, router, fake_session_factory, audit):
        events = [make_event("lead_context", "ctx", object_id=str(900 + i),
                             event_id=f"c{i}") for i in range(45)]
        result = router.route_batch(events)
        session = fake_session_factory()
        outcomes = self._dispatcher(session, audit, batch_size=20).dispatch_grouped(
            result.decisions, "run-1")

        assert [o.batch_size for o in outcomes] == [20, 20, 5]
        assert len(session.calls) == 3
        # every lead travelled exactly once
        sent = [oid for c in session.calls
                for oid in c["json"]["params"]["metadata"]["object_ids"]]
        assert len(sent) == 45 and len(set(sent)) == 45

    def test_every_event_id_rides_on_its_outcome(
            self, router, fake_session_factory, audit):
        """server.py releases exactly these from the dedupe store on failure."""
        events = [make_event("lead_context", "ctx", event_id=f"x{i}")
                  for i in range(3)]
        result = router.route_batch(events)
        outcomes = self._dispatcher(fake_session_factory(), audit).dispatch_grouped(
            result.decisions, "run-1")
        assert set(outcomes[0].event_ids) == {"x0", "x1", "x2"}

    def test_batch_ids_are_deterministic(self, router, fake_session_factory, audit):
        """A redelivery of the same run mints the same batch id, so the audit
        trail stays joinable."""
        events = [make_event("lead_context", "ctx", event_id="d1")]
        first = self._dispatcher(fake_session_factory(), audit).dispatch_grouped(
            router.route_batch(events).decisions, "run-same")
        second = self._dispatcher(fake_session_factory(), audit).dispatch_grouped(
            router.route_batch(events).decisions, "run-same")
        assert first[0].trigger_id == second[0].trigger_id
        assert first[0].trigger_id.startswith("bat-")

    def test_no_profile_field_reaches_the_wire_when_grouped(
            self, router, fake_session_factory, audit):
        events = [make_event("lead_context", "ctx", event_id=f"p{i}")
                  for i in range(4)]
        result = router.route_batch(events)
        session = fake_session_factory()
        self._dispatcher(session, audit).dispatch_grouped(result.decisions, "run-1")
        blob = json.dumps(session.calls[0]["json"]).lower()
        for field in ("email", "phone", "full_name", "company", "annual_revenue"):
            assert field not in blob

    def test_per_lead_is_still_one_call_per_lead(
            self, router, fake_session_factory, audit):
        """The default must be byte-for-byte what shipped."""
        events = [make_event("lead_context", "ctx", event_id=f"s{i}")
                  for i in range(5)]
        result = router.route_batch(events)
        session = fake_session_factory()
        gw_dispatch.Dispatcher(_client(session), audit).dispatch_all(
            result.decisions, "run-1")
        assert len(session.calls) == 5
        assert "object_ids" not in session.calls[0]["json"]

    def test_an_unknown_mode_falls_back_to_per_lead(self, fake_session_factory, audit):
        d = gw_dispatch.Dispatcher(_client(fake_session_factory()), audit,
                                   mode="grouepd")
        assert d.mode == "per_lead"
