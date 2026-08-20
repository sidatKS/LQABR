"""Step 3 / Step 7 — the four log streams, and the guard on what may be logged.

Rev 3's observability is a *functional* requirement, not a nice-to-have, so it
gets tested like one: the streams exist, they carry run_id and trigger_id, the
routing decision is logged next to the property and value it was based on, the
token/model stream is an explicit N/A, and lead-profile data cannot be written
at all.
"""

from __future__ import annotations

import json

import pytest

from conftest import gw_router, make_event
from soloai.audit_hooks import AuditHooks, ProfileFieldLeak, Stream


def _records(hooks, stream=None, event=None):
    items = hooks.records
    if stream is not None:
        items = [r for r in items if r["stream"] == stream.value]
    if event is not None:
        items = [r for r in items if r["event"] == event]
    return items


# ================================================================ the streams
class TestFourStreams:
    def test_all_four_streams_are_accounted_for(self):
        """Rev 3 names exactly four. Three the gateway writes; the fourth it
        must explicitly exclude."""
        assert {s.value for s in Stream} == {"audit", "process", "system", "token_model"}

    def test_config_yaml_enables_three_and_excludes_token_model(self, config):
        streams = config.section("audit.streams")
        assert streams["audit"] and streams["process"] and streams["system"]
        assert streams["token_model"] is False

    def test_every_record_carries_run_id(self, audit, hooks, router):
        run_id = audit.new_run_id()
        result = router.route_batch([make_event()])
        audit.record_ingress(run_id, source_ip="1.2.3.4", endpoint="/hubspot/events",
                             method="POST", event_count=1, payload_bytes=200,
                             signature_verified=True)
        audit.record_decision(run_id, result.decisions[0])
        assert hooks.records
        assert all(r["run_id"] == run_id for r in hooks.records
                   if r["event"] != "token_model_stream_not_applicable")

    def test_decision_records_carry_the_trigger_id(self, audit, hooks, router):
        run_id = audit.new_run_id()
        decision = router.route_batch([make_event()]).decisions[0]
        audit.record_decision(run_id, decision)
        record = _records(hooks, Stream.PROCESS, "routing_decision")[0]
        assert record["trigger_id"] == decision.trigger_id

    def test_one_lead_is_reconstructible_from_run_and_trigger_id(self, audit, hooks, router):
        """FR-7: *one lead's path can be reconstructed from log search alone.*"""
        run_id = audit.new_run_id()
        decision = router.route_batch([make_event()]).decisions[0]
        audit.record_ingress(run_id, source_ip=None, endpoint="/hubspot/events",
                             method="POST", event_count=1, payload_bytes=1,
                             signature_verified=True)
        audit.record_decision(run_id, decision)
        audit.record_dispatch(run_id, decision, ok=True, status_code=200,
                              latency_ms=12.0, attempts=1, payload_size_bytes=300)
        path = [r for r in hooks.records if r.get("trigger_id") == decision.trigger_id]
        assert {r["stream"] for r in path} == {"audit", "process", "system"}


# ============================================================ the decision log
class TestDecisionLogging:
    def test_the_decision_is_logged_with_the_property_and_value_behind_it(
            self, audit, hooks, router):
        """The single most-quoted line in Rev 3: *Log the routing decision
        together with the property and value it was based on.*"""
        run_id = audit.new_run_id()
        decision = router.route_batch([
            make_event("lqabr_email_status", "OPENED")]).decisions[0]
        audit.record_decision(run_id, decision)
        record = _records(hooks, Stream.PROCESS, "routing_decision")[0]
        assert record["agent"] == "voice"
        assert record["property_name"] == "lqabr_email_status"
        assert record["property_value"] == "OPENED"
        assert record["route_id"] == "R3-email-opened"

    def test_discards_are_logged_with_a_reason(self, audit, hooks, router):
        run_id = audit.new_run_id()
        result = router.route_batch([make_event("lqabr_email_status", "BOUNCED")])
        audit.record_discards(run_id, result.discarded)
        record = _records(hooks, Stream.PROCESS, "event_discarded")[0]
        assert record["reason"] == "not_routing_condition"
        assert record["property_value"] == "BOUNCED"

    def test_discards_are_summarised_at_standard_level(self, router):
        """A subscription that fires on every value produces a lot of
        legitimate discards; per-event logging is opt-in."""
        from conftest import gw_audit
        hooks = AuditHooks(sink="file", file_path="/dev/null",
                           keep_records=True, level="standard")
        audit = gw_audit.GatewayAudit(hooks)
        run_id = audit.new_run_id()
        result = router.route_batch([
            make_event("lqabr_email_status", "SENT", event_id=f"e{i}") for i in range(5)])
        audit.record_discards(run_id, result.discarded)
        assert _records(hooks, Stream.PROCESS, "event_discarded") == []
        summary = _records(hooks, Stream.PROCESS, "events_discarded")[0]
        assert summary["discarded"] == 5
        assert summary["by_reason"] == {"not_routing_condition": 5}

    def test_routing_errors_reach_both_process_and_system(
            self, registry_document, audit, hooks):
        """An unresolvable endpoint is a config fault as well as a data event."""
        registry = gw_router.AgentRegistry.from_document(registry_document, environ={})
        router = gw_router.Router(registry=registry)
        run_id = audit.new_run_id()
        result = router.route_batch([make_event()])
        audit.record_routing_errors(run_id, result.errors)
        assert _records(hooks, Stream.PROCESS, "routing_error")
        assert _records(hooks, Stream.SYSTEM, "routing_errors_in_batch")

    def test_minimal_level_keeps_only_the_audit_stream(self, router):
        from conftest import gw_audit
        hooks = AuditHooks(sink="file", file_path="/dev/null",
                           keep_records=True, level="minimal")
        audit = gw_audit.GatewayAudit(hooks)
        run_id = audit.new_run_id()
        decision = router.route_batch([make_event()]).decisions[0]
        audit.record_decision(run_id, decision)
        audit.record_ingress(run_id, source_ip=None, endpoint="/x", method="POST",
                             event_count=1, payload_bytes=1, signature_verified=True)
        assert _records(hooks, Stream.PROCESS) == []
        assert _records(hooks, Stream.AUDIT)


# ============================================================== network + system
class TestAuditAndSystemStreams:
    def test_audit_stream_records_where_from_when_and_which_endpoint(self, audit, hooks):
        run_id = audit.new_run_id()
        audit.record_ingress(run_id, source_ip="10.0.0.9", endpoint="/hubspot/events",
                             method="POST", event_count=3, payload_bytes=1024,
                             signature_verified=True)
        record = _records(hooks, Stream.AUDIT, "hubspot_ingress_received")[0]
        assert record["source_ip"] == "10.0.0.9"
        assert record["endpoint"] == "/hubspot/events"
        assert record["event_count"] == 3
        assert record["received_at"]

    def test_dispatch_audit_carries_status_latency_and_retry_count(
            self, audit, hooks, router):
        """Step 4's observability row, item by item."""
        run_id = audit.new_run_id()
        decision = router.route_batch([make_event()]).decisions[0]
        audit.record_dispatch(run_id, decision, ok=True, status_code=200,
                              latency_ms=42.5, attempts=3, payload_size_bytes=310)
        record = _records(hooks, Stream.AUDIT, "agent_dispatch")[0]
        assert (record["status"], record["latency_ms"], record["retry_count"]) == \
            (200, 42.5, 2)

    def test_process_stream_records_the_protocol_conversion_and_payload_size(
            self, audit, hooks, router):
        run_id = audit.new_run_id()
        decision = router.route_batch([make_event()]).decisions[0]
        audit.record_dispatch(run_id, decision, ok=True, status_code=200,
                              latency_ms=1.0, attempts=1, payload_size_bytes=307)
        record = _records(hooks, Stream.PROCESS, "protocol_conversion")[0]
        assert record["conversion"] == "https_ingress -> a2a_message_send"
        assert record["payload_size_bytes"] == 307

    def test_system_stream_records_container_memory(self, audit, hooks):
        audit.record_startup(runtime={"name": "agentgateway"}, registry_health={},
                             ingress_path="/hubspot/events", concurrency_limit=10,
                             chunk_size_hint=5)
        record = _records(hooks, Stream.SYSTEM, "gateway_startup")[0]
        assert "memory_rss_mb" in record

    def test_system_stream_records_ingress_concurrency_against_the_limit(
            self, audit, hooks):
        audit.record_ingress(audit.new_run_id(), source_ip=None, endpoint="/x",
                             method="POST", event_count=1, payload_bytes=1,
                             signature_verified=True,
                             concurrency={"in_flight": 4, "concurrency_limit": 10,
                                          "peak_in_flight": 7, "headroom": 6})
        record = _records(hooks, Stream.SYSTEM, "ingress_concurrency")[0]
        assert record["concurrency_limit"] == 10 and record["in_flight"] == 4

    def test_unresolved_endpoints_are_flagged_at_startup(self, audit, hooks):
        audit.record_startup(
            runtime={}, ingress_path="/x", concurrency_limit=10, chunk_size_hint=5,
            registry_health={"email": {"ready": True},
                             "voice": {"ready": False, "reason": "unset"}})
        flagged = _records(hooks, Stream.SYSTEM, "agent_endpoints_unresolved")[0]
        assert "voice" in flagged["agents"] and "email" not in flagged["agents"]

    def test_exceptions_reach_the_system_stream(self, audit, hooks):
        audit.record_exception(audit.new_run_id(), ValueError("boom"), where="ingress")
        record = _records(hooks, Stream.SYSTEM, "unhandled_exception")[0]
        assert record["exception_type"] == "ValueError"


# ============================================================ token/model = N/A
class TestTokenModelExclusion:
    def test_recorded_as_an_explicit_exclusion_not_an_omission(self, audit, hooks):
        """Rev 3 is emphatic about this wording, so it is asserted literally."""
        audit.record_startup(runtime={}, registry_health={}, ingress_path="/x",
                             concurrency_limit=10, chunk_size_hint=5)
        record = _records(hooks, None, "token_model_stream_not_applicable")[0]
        assert record["stream_status"] == "n/a"
        assert "no model calls" in record["reason"]

    def test_the_gateway_writes_no_token_records(self, audit, hooks, router):
        run_id = audit.new_run_id()
        decision = router.route_batch([make_event()]).decisions[0]
        audit.record_decision(run_id, decision)
        audit.record_dispatch(run_id, decision, ok=True, status_code=200,
                              latency_ms=1.0, attempts=1, payload_size_bytes=1)
        assert _records(hooks, Stream.TOKEN_MODEL) == []


# ============================================================ the leak guard
class TestProfileDataGuard:
    """The trigger-only guarantee, enforced rather than promised.

    A log line is the easiest way for profile data to cross a service that
    swore it wouldn't carry any, so the writer refuses instead of trusting.
    """

    @pytest.mark.parametrize("field,value", [
        ("email", "lead@example.test"),
        ("phone_number", "+15550100"),
        ("annual_revenue", 5_000_000),
        ("job_title", "VP Engineering"),
        ("company", "Example Corp"),
        ("full_name", "A Person"),
        ("industry", "Manufacturing"),
    ])
    def test_refuses_to_log_any_of_the_nine_lead_parameters(self, hooks, field, value):
        with pytest.raises(ProfileFieldLeak, match="lead-profile data"):
            hooks.process("routing_decision", run_id="run-1", **{field: value})

    def test_catches_profile_data_nested_in_a_structure(self, hooks):
        with pytest.raises(ProfileFieldLeak):
            hooks.audit("agent_dispatch", run_id="run-1",
                        response={"lead": {"contact": {"email": "x@y.test"}}})

    def test_property_name_and_value_are_allowed_because_fr7_requires_them(self, hooks):
        record = hooks.process("routing_decision", run_id="run-1",
                               property_name="lead_context", property_value="ctx")
        assert record["property_name"] == "lead_context"

    def test_object_id_is_a_record_id_not_profile_data(self, hooks):
        assert hooks.process("routing_decision", run_id="r", object_id="701")

    def test_guard_can_be_disabled_but_is_on_in_the_shipped_config(self, config):
        assert config.get("audit.forbid_profile_fields") is True
        permissive = AuditHooks(sink="file", file_path="/dev/null",
                                forbid_profile_fields=False, keep_records=True)
        assert permissive.process("x", email="a@b.test")   # no raise


# ==================================================================== metrics
class TestHandoffMetrics:
    def test_counts_the_handoff(self, audit, router):
        run_id = audit.new_run_id()
        result = router.route_batch([
            make_event("lqabr_email_status", "OPENED", event_id="e1"),
            make_event("lqabr_email_status", "SENT", event_id="e2"),
        ])
        audit.record_ingress(run_id, source_ip=None, endpoint="/x", method="POST",
                             event_count=2, payload_bytes=1, signature_verified=True)
        for decision in result.decisions:
            audit.record_decision(run_id, decision)
            audit.record_dispatch(run_id, decision, ok=True, status_code=200,
                                  latency_ms=10.0, attempts=2, payload_size_bytes=1)
        audit.record_discards(run_id, result.discarded)
        metrics = audit.metrics.as_dict()
        assert metrics["requests"] == 1
        assert metrics["events_received"] == 2
        assert metrics["routed"] == 1
        assert metrics["discarded"] == 1
        assert metrics["dispatched_ok"] == 1
        assert metrics["dispatch_retries"] == 1
        assert metrics["by_agent"] == {"voice": 1}
        assert metrics["dispatch_latency_ms_mean"] == 10.0


# ============================== regressions found in adversarial review
class TestGuardEscapes:
    """Each of these got past the guard on the first implementation."""

    def test_a_profile_field_nested_in_a_list_is_caught(self, hooks):
        """Suppression for structural containers used to propagate into lists,
        so routes=[{"email": ...}] wrote a lead's address to the log."""
        with pytest.raises(ProfileFieldLeak):
            hooks.process("routing_decision", run_id="r",
                          routes=[{"email": "jane.doe@acme.test"}])

    def test_a_container_name_reused_at_depth_does_not_re_suppress(self, hooks):
        """Suppression was recomputed from the parent key at every level, so any
        nested key that happened to be a container name turned the guard off."""
        with pytest.raises(ProfileFieldLeak):
            hooks.process("routing_decision", run_id="r",
                          agents={"health": {"email": "jane.doe@acme.test"}})

    def test_structural_containers_still_work_for_their_real_purpose(self, hooks):
        """The Email agent is called "email" — a routing table is not a lead."""
        assert hooks.system("gateway_startup", agents={"email": {"ready": True}},
                            runtime={"name": "agentgateway"})


class TestValueRedaction:
    """The key-name guard cannot see values, and a property *value* is
    config-controlled: subscribe to a profile property in the portal and
    propertyValue arrives holding an email address, under a key FR-7 requires."""

    def test_an_email_in_a_property_value_is_redacted_not_logged(self, hooks):
        record = hooks.process("routing_decision", run_id="r",
                               property_name="email",
                               property_value="jane.doe@acme.test")
        assert "jane.doe@acme.test" not in json.dumps(record)
        assert record["redacted_values"] == 1

    def test_an_email_in_free_text_is_redacted(self, hooks):
        record = hooks.process("event_discarded", run_id="r",
                               detail="no route for jane.doe@acme.test")
        assert "jane.doe@acme.test" not in json.dumps(record)

    def test_a_phone_number_is_redacted(self, hooks):
        record = hooks.process("routing_decision", run_id="r",
                               property_value="+1 555 010 0199")
        assert "555" not in json.dumps(record)

    def test_ordinary_routing_values_are_left_alone(self, hooks):
        for value in ("OPENED", "COMPLETED", "true", "1753876800000", "R3-email-opened"):
            record = hooks.process("routing_decision", run_id="r", property_value=value)
            assert record["property_value"] == value
            assert "redacted_values" not in record

    def test_redaction_is_visible_rather_than_silent(self, hooks):
        record = hooks.process("x", run_id="r", property_value="a@b.test")
        assert record["redacted_values"] == 1
