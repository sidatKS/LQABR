"""Step 4 — the hand-off. Trigger id only, and provably so.

The central assertion of this file is negative: whatever else the dispatch does,
the bytes on the wire contain a trigger id and correlation metadata, and nothing
that could identify or describe a lead.
"""

from __future__ import annotations

import json

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
    return router.route_batch([make_event("lqabr_email_status", "OPENED")]).decisions[0]


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
            self, decision, fake_session_factory, audit):
        session = fake_session_factory()
        gw_dispatch.Dispatcher(_client(session), audit).dispatch(decision, "run-1")
        metadata = session.last_body["params"]["metadata"]
        assert set(metadata) == {"trigger_id", "object_id", "run_id", "source",
                                 "gateway_version"}

    def test_the_record_id_is_the_one_the_agent_can_actually_resolve(
            self, decision, fake_session_factory, audit):
        """D-05. This is the whole point of the change: the agent does
        GET /crm/v3/objects/contacts/<object_id> and has the lead. Neither the
        trigger_id nor HubSpot's eventId is stored anywhere in the CRM."""
        session = fake_session_factory()
        gw_dispatch.Dispatcher(_client(session), audit).dispatch(decision, "run-1")
        assert session.last_body["params"]["metadata"]["object_id"] == "701"

    def test_opting_into_the_routing_basis_adds_only_the_route_id(
            self, decision, fake_session_factory, audit):
        session = fake_session_factory()
        gw_dispatch.Dispatcher(_client(session), audit,
                               include_routing_basis=True).dispatch(decision, "run-1")
        metadata = session.last_body["params"]["metadata"]
        assert metadata["route_id"] == "R3-email-opened"
        assert "property_name" not in metadata and "property_value" not in metadata

    def test_correlation_headers_let_the_sidecar_log_be_joined(
            self, decision, fake_session_factory, audit):
        """config/agentgateway.yaml reads these off the request; without them
        its access-log trigger_id/run_id fields are always null."""
        session = fake_session_factory()
        gw_dispatch.Dispatcher(_client(session), audit).dispatch(decision, "run-1")
        headers = session.calls[-1]["headers"]
        assert headers["x-lqabr-trigger-id"] == decision.trigger_id
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
            make_event("lqabr_email_status", "OPENED", event_id="e1"),
            make_event("decision_maker", "true", event_id="e2"),
            make_event("lqabr_voice_status", "COMPLETED", event_id="e3"),
        ])
        session = fake_session_factory()
        outcomes = gw_dispatch.Dispatcher(_client(session), audit).dispatch_all(
            result.decisions, "run-1")
        assert len(outcomes) == 3 and all(o.ok for o in outcomes)
        assert {c["url"] for c in session.calls} == {
            "https://voice-agent.example.test/a2a",
            "https://email-agent.example.test/a2a",
            "https://scheduling-agent.example.test/a2a",
        }

    def test_one_failure_does_not_stop_the_others(
            self, router, fake_session_factory, fake_response_factory, audit):
        result = router.route_batch([
            make_event("lqabr_email_status", "OPENED", event_id="e1"),
            make_event("decision_maker", "true", event_id="e2"),
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
