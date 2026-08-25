"""The ingress — the HTTP contract with HubSpot, end to end.

These tests drive the real ``create_app`` wiring with a fake A2A transport, so
what is exercised is the actual order of operations (ingress -> route -> audit ->
dispatch) rather than a re-implementation of it.

The status codes are the interesting part: HubSpot retries a non-2xx with an
incremented ``attemptNumber``, so 200 vs 503 is the difference between a lead
being handled and a lead being dropped.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from conftest import gw_audit, gw_dispatch, gw_router, gw_server, make_event
from soloai import load_config
from soloai.audit_hooks import AuditHooks
from soloai.protocols.a2a import A2AClient
from soloai.protocols.http import compute_v3_signature

SECRET = "test-app-secret"
BASE = "http://testserver"
VAPI_SECRET = "test-vapi-secret"
REPORT_URL = "http://text-voice.test/call-report"


@pytest.fixture()
def app_bundle(config_dir, registry, fake_session_factory, monkeypatch):
    """A wired app plus handles on its audit records and fake transport."""
    monkeypatch.setenv("HUBSPOT_APP_SECRET", SECRET)
    monkeypatch.setenv("LQABR_GATEWAY_PUBLIC_URL", BASE)
    monkeypatch.setenv("LQABR_VOICE_REPORT_URL", REPORT_URL)
    monkeypatch.setenv("LQABR_VAPI_WEBHOOK_SECRET", VAPI_SECRET)

    config = load_config(config_dir / "config.yaml")
    hooks = AuditHooks(sink="file", file_path="/dev/null", keep_records=True,
                       level="verbose")
    audit = gw_audit.GatewayAudit(hooks)
    session = fake_session_factory()
    dispatcher = gw_dispatch.Dispatcher(
        A2AClient(session=session, backoff_seconds=0, sleep=lambda _s: None), audit)
    app = gw_server.create_app(config=config, registry=registry,
                               dispatcher=dispatcher, audit=audit)
    return {"app": app, "hooks": hooks, "audit": audit, "session": session,
            "config": config}


@pytest.fixture()
def client(app_bundle):
    with TestClient(app_bundle["app"]) as test_client:
        yield test_client


def signed(payload, *, path="/hubspot/events", secret=SECRET, timestamp=None):
    """Headers HubSpot would send for this body."""
    body = json.dumps(payload)
    stamp = timestamp or str(int(time.time() * 1000))
    signature = compute_v3_signature(secret, "POST", f"{BASE}{path}", body, stamp)
    return body, {
        "X-HubSpot-Signature-v3": signature,
        "X-HubSpot-Request-Timestamp": stamp,
        "Content-Type": "application/json",
    }


# ====================================================================== probes
class TestProbes:
    def test_healthz(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.json()["runtime"] == "agentgateway"

    def test_readyz_is_ready_when_every_endpoint_resolves(self, client):
        response = client.get("/readyz")
        assert response.status_code == 200
        assert all(a["ready"] for a in response.json()["agents"].values())

    def test_readyz_is_503_when_an_endpoint_is_missing(
            self, config_dir, registry_document, fake_session_factory):
        registry = gw_router.AgentRegistry.from_document(registry_document, environ={})
        app = gw_server.create_app(config=load_config(config_dir / "config.yaml"),
                                   registry=registry)
        with TestClient(app) as broken:
            assert broken.get("/readyz").status_code == 503

    def test_root_describes_what_the_service_refuses_to_carry(self, client):
        body = client.get("/").json()
        assert body["carries"] == "trigger_id only"
        # 3, not 4: R1-contact-created was disabled 25-Aug-2026 (email
        # triggers off lead_context only now).
        assert len(body["routes"]) == 3

    def test_metrics_exposes_handoff_counters(self, client):
        body = client.get("/metrics").json()
        assert "handoff" in body and "ingress" in body
        assert body["ingress"]["concurrency_limit"] == 10


# =================================================================== signatures
class TestSignature:
    def test_a_correctly_signed_delivery_is_accepted(self, client):
        body, headers = signed([make_event()])
        assert client.post("/hubspot/events", content=body, headers=headers).status_code == 200

    def test_an_unsigned_delivery_is_rejected(self, client):
        response = client.post("/hubspot/events", json=[make_event()])
        assert response.status_code == 401
        assert response.json()["error"] == "unauthorized"

    def test_a_wrong_signature_is_rejected(self, client):
        body, headers = signed([make_event()], secret="not-the-secret")
        assert client.post("/hubspot/events", content=body,
                           headers=headers).status_code == 401

    def test_a_tampered_body_is_rejected(self, client):
        body, headers = signed([make_event("lqabr_email_status", "SENT")])
        tampered = json.dumps([make_event("lqabr_email_status", "OPENED")])
        assert client.post("/hubspot/events", content=tampered,
                           headers=headers).status_code == 401

    def test_a_stale_but_correctly_signed_delivery_is_rejected(self, client):
        """Replay guard: a valid signature over an old timestamp is still an
        attack, and the ingress can start outreach."""
        old = str(int((time.time() - 3600) * 1000))
        body, headers = signed([make_event()], timestamp=old)
        assert client.post("/hubspot/events", content=body,
                           headers=headers).status_code == 401

    def test_rejections_are_audited(self, client, app_bundle):
        client.post("/hubspot/events", json=[make_event()])
        rejected = [r for r in app_bundle["hooks"].records
                    if r["event"] == "hubspot_ingress_rejected"]
        assert rejected and rejected[0]["status"] == 401


# ==================================================================== envelope
class TestEnvelope:
    def test_a_non_array_body_is_a_400(self, client):
        body, headers = signed("not-a-list")
        assert client.post("/hubspot/events", content=body,
                           headers=headers).status_code == 400

    def test_invalid_json_is_a_400(self, client):
        stamp = str(int(time.time() * 1000))
        signature = compute_v3_signature(SECRET, "POST", f"{BASE}/hubspot/events",
                                         "{oops", stamp)
        response = client.post("/hubspot/events", content="{oops", headers={
            "X-HubSpot-Signature-v3": signature,
            "X-HubSpot-Request-Timestamp": stamp})
        assert response.status_code == 400

    def test_a_single_object_is_accepted_as_a_batch_of_one(self, client):
        body, headers = signed(make_event())
        response = client.post("/hubspot/events", content=body, headers=headers)
        assert response.status_code == 200 and response.json()["accepted"] == 1

    def test_a_batch_of_exactly_one_hundred_is_accepted(self, client):
        events = [make_event(event_id=f"evt-{i}", object_id=str(700 + i))
                  for i in range(100)]
        body, headers = signed(events)
        response = client.post("/hubspot/events", content=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["routed"] == 100

    def test_a_batch_over_one_hundred_is_a_413(self, client):
        body, headers = signed([make_event(event_id=f"evt-{i}") for i in range(101)])
        assert client.post("/hubspot/events", content=body,
                           headers=headers).status_code == 413

    def test_an_empty_array_is_a_400(self, client):
        body, headers = signed([])
        assert client.post("/hubspot/events", content=body,
                           headers=headers).status_code == 400


# ================================================================ routing e2e
class TestRoutingEndToEnd:
    def test_the_documented_paths_reach_the_documented_agents(self, client, app_bundle):
        cases = [
            (make_event("lead_context", "ctx", event_id="e1"),
             "https://email-agent.example.test/a2a"),
            (make_event("lqabr_email_status", "OPENED", event_id="e2"),
             "https://voice-agent.example.test/a2a"),
        ]
        for payload, expected_url in cases:
            body, headers = signed([payload])
            response = client.post("/hubspot/events", content=body, headers=headers)
            assert response.status_code == 200
            assert app_bundle["session"].calls[-1]["url"] == expected_url

    def test_only_the_trigger_id_crosses_the_gateway(self, client, app_bundle):
        """The whole design in one assertion: a payload full of profile data
        goes in, a trigger id goes out."""
        payload = make_event("lqabr_email_status", "OPENED")
        payload.update({"email": "lead@example.test", "phone": "+15550100",
                        "company": "Example Corp", "annual_revenue": 9_000_000})
        body, headers = signed([payload])
        assert client.post("/hubspot/events", content=body, headers=headers).status_code == 200
        wire = json.dumps(app_bundle["session"].last_body)
        assert "lead@example.test" not in wire
        assert "Example Corp" not in wire
        assert "9000000" not in wire
        parts = app_bundle["session"].last_body["params"]["message"]["parts"]
        assert parts[0]["text"].startswith("trg-")

    def test_discarded_values_return_200_and_dispatch_nothing(self, client, app_bundle):
        """Not the routing condition is a normal outcome, not an error — HubSpot
        must not retry it."""
        body, headers = signed([make_event("lqabr_email_status", "BOUNCED")])
        response = client.post("/hubspot/events", content=body, headers=headers)
        assert response.status_code == 200
        assert response.json()["discarded"] == 1
        assert app_bundle["session"].calls == []

    def test_a_mixed_batch_is_summarised_in_the_response(self, client):
        body, headers = signed([
            make_event("lqabr_email_status", "OPENED", event_id="e1"),
            make_event("lqabr_email_status", "SENT", event_id="e2"),
            make_event("lifecyclestage", "customer", event_id="e3"),
        ])
        summary = client.post("/hubspot/events", content=body, headers=headers).json()
        assert summary["routed"] == 1
        assert summary["discarded"] == 2
        assert summary["discards_by_reason"]["no_matching_route"] == 1

    def test_the_response_carries_the_run_id_for_log_search(self, client):
        body, headers = signed([make_event()])
        assert client.post("/hubspot/events", content=body,
                           headers=headers).json()["run_id"].startswith("run-")


# ================================================================ retry contract
class TestRetryContract:
    def test_a_routing_error_returns_503_so_hubspot_retries(
            self, config_dir, registry_document, monkeypatch):
        """Matched but undeliverable must not look like success — that is how a
        lead would get silently dropped."""
        monkeypatch.setenv("HUBSPOT_APP_SECRET", SECRET)
        monkeypatch.setenv("LQABR_GATEWAY_PUBLIC_URL", BASE)
        registry = gw_router.AgentRegistry.from_document(registry_document, environ={})
        app = gw_server.create_app(config=load_config(config_dir / "config.yaml"),
                                   registry=registry)
        with TestClient(app) as broken:
            body, headers = signed([make_event()])
            response = broken.post("/hubspot/events", content=body, headers=headers)
            assert response.status_code == 503
            assert response.json()["routing_errors"][0]["agent"] == "voice"

    def test_a_dispatch_failure_returns_503(
            self, config_dir, registry, fake_session_factory, fake_response_factory,
            monkeypatch):
        monkeypatch.setenv("HUBSPOT_APP_SECRET", SECRET)
        monkeypatch.setenv("LQABR_GATEWAY_PUBLIC_URL", BASE)
        hooks = AuditHooks(sink="file", file_path="/dev/null", keep_records=True)
        audit = gw_audit.GatewayAudit(hooks)
        session = fake_session_factory(fake_response_factory(500, text="agent down"))
        dispatcher = gw_dispatch.Dispatcher(
            A2AClient(session=session, backoff_seconds=0, sleep=lambda _s: None), audit)
        app = gw_server.create_app(config=load_config(config_dir / "config.yaml"),
                                   registry=registry, dispatcher=dispatcher, audit=audit)
        with TestClient(app) as failing:
            body, headers = signed([make_event()])
            assert failing.post("/hubspot/events", content=body,
                                headers=headers).status_code == 503

    def test_a_successful_dispatch_dedupes_the_redelivery(self, client, app_bundle):
        body, headers = signed([make_event(event_id="evt-dedupe")])
        assert client.post("/hubspot/events", content=body, headers=headers).status_code == 200
        calls_after_first = len(app_bundle["session"].calls)

        retry_body, retry_headers = signed([make_event(event_id="evt-dedupe",
                                                      attempt_number=1)])
        response = client.post("/hubspot/events", content=retry_body,
                               headers=retry_headers)
        assert response.status_code == 200
        assert response.json()["discards_by_reason"]["duplicate_event"] == 1
        # and crucially, the agent was not woken twice
        assert len(app_bundle["session"].calls) == calls_after_first

    def test_a_redelivery_during_an_in_flight_dispatch_is_deduped(
            self, config_dir, registry, fake_response_factory, monkeypatch):
        """Why the reservation is taken BEFORE the hand-off, not after.

        HubSpot's response budget is 5s. A slow agent means the redelivery
        lands while the first hand-off is still open — observed live on
        05-Aug-2026 as ``peak_in_flight: 4`` against a 30s dispatch. With the
        store written only on success it is still empty when the redelivery
        checks it, and the same lead is handed over twice.
        """
        monkeypatch.setenv("HUBSPOT_APP_SECRET", SECRET)
        monkeypatch.setenv("LQABR_GATEWAY_PUBLIC_URL", BASE)

        started = threading.Event()
        release = threading.Event()

        class SlowSession:
            def __init__(self) -> None:
                self.calls = []

            def post(self, url, **kwargs):
                self.calls.append(url)
                started.set()
                release.wait(10)
                return fake_response_factory(200)

        session = SlowSession()
        hooks = AuditHooks(sink="file", file_path="/dev/null", keep_records=True)
        audit = gw_audit.GatewayAudit(hooks)
        dispatcher = gw_dispatch.Dispatcher(
            A2AClient(session=session, backoff_seconds=0, sleep=lambda _s: None), audit)
        app = gw_server.create_app(config=load_config(config_dir / "config.yaml"),
                                   registry=registry, dispatcher=dispatcher, audit=audit)

        body, headers = signed([make_event(event_id="evt-slow")])
        retry_body, retry_headers = signed(
            [make_event(event_id="evt-slow", attempt_number=1)])

        with TestClient(app) as first_client, TestClient(app) as second_client:
            outcome = {}

            def fire_first():
                outcome["response"] = first_client.post(
                    "/hubspot/events", content=body, headers=headers)

            worker = threading.Thread(target=fire_first)
            worker.start()
            assert started.wait(10), "the first dispatch never reached the agent"

            redelivery = second_client.post(
                "/hubspot/events", content=retry_body, headers=retry_headers)

            release.set()
            worker.join(15)

        assert redelivery.status_code == 200
        assert redelivery.json()["discards_by_reason"]["duplicate_event"] == 1
        assert outcome["response"].status_code == 200
        # The whole point: one HubSpot event, one hand-off.
        assert len(session.calls) == 1

    def test_a_failed_event_is_retried_rather_than_deduped(
            self, config_dir, registry, fake_session_factory, fake_response_factory,
            monkeypatch):
        """The asymmetry that makes 503 meaningful: a failure is not remembered,
        so the redelivery gets a real second attempt."""
        monkeypatch.setenv("HUBSPOT_APP_SECRET", SECRET)
        monkeypatch.setenv("LQABR_GATEWAY_PUBLIC_URL", BASE)
        hooks = AuditHooks(sink="file", file_path="/dev/null", keep_records=True)
        audit = gw_audit.GatewayAudit(hooks)
        session = fake_session_factory([
            fake_response_factory(500, text="down"),   # attempt 1
            fake_response_factory(500, text="down"),   # retry
            fake_response_factory(500, text="down"),   # retry
            fake_response_factory(200),                # redelivery succeeds
        ])
        dispatcher = gw_dispatch.Dispatcher(
            A2AClient(session=session, backoff_seconds=0, sleep=lambda _s: None), audit)
        app = gw_server.create_app(config=load_config(config_dir / "config.yaml"),
                                   registry=registry, dispatcher=dispatcher, audit=audit)
        with TestClient(app) as flaky:
            body, headers = signed([make_event(event_id="evt-flaky")])
            assert flaky.post("/hubspot/events", content=body,
                              headers=headers).status_code == 503
            retry_body, retry_headers = signed([make_event(event_id="evt-flaky",
                                                           attempt_number=1)])
            response = flaky.post("/hubspot/events", content=retry_body,
                                  headers=retry_headers)
            assert response.status_code == 200
            assert response.json()["routed"] == 1


# ================================================================ observability
class TestIngressObservability:
    def test_ingress_is_audited_with_source_and_endpoint(self, client, app_bundle):
        body, headers = signed([make_event()])
        client.post("/hubspot/events", content=body, headers=headers)
        received = [r for r in app_bundle["hooks"].records
                    if r["event"] == "hubspot_ingress_received"]
        assert received[0]["endpoint"] == "/hubspot/events"
        assert received[0]["event_count"] == 1
        assert received[0]["signature_verified"] is True

    def test_concurrency_is_recorded_against_the_limit_of_ten(self, client, app_bundle):
        body, headers = signed([make_event()])
        client.post("/hubspot/events", content=body, headers=headers)
        concurrency = [r for r in app_bundle["hooks"].records
                       if r["event"] == "ingress_concurrency"]
        assert concurrency[0]["concurrency_limit"] == 10

    def test_startup_records_the_runtime_and_the_token_model_exclusion(
            self, client, app_bundle):
        events = {r["event"] for r in app_bundle["hooks"].records}
        assert "gateway_startup" in events
        assert "token_model_stream_not_applicable" in events

    def test_the_full_path_of_one_lead_is_in_the_log(self, client, app_bundle):
        body, headers = signed([make_event("lead_context", "ctx")])
        trigger_id = client.post("/hubspot/events", content=body,
                                 headers=headers).json()["dispatched"][0]["trigger_id"]
        path = [r for r in app_bundle["hooks"].records
                if r.get("trigger_id") == trigger_id]
        assert {r["event"] for r in path} >= {
            "routing_decision", "agent_dispatch", "protocol_conversion"}


# ================================================================== config gate
class TestConfigGuards:
    def test_the_app_refuses_to_start_if_configured_to_proxy_profiles(
            self, config_dir, registry):
        """A config change must not be able to turn the gateway into a profile
        proxy quietly."""
        from soloai.protocols.mcp import MCPConfigError
        config = load_config(config_dir / "config.yaml")
        config.as_dict()["protocols"]["mcp"]["proxy_profile_data"] = True
        with pytest.raises(MCPConfigError, match="proxy_profile_data"):
            gw_server.create_app(config=config, registry=registry)

    def test_shipped_config_matches_the_documented_limits(self, config):
        assert config.get("gateway.ingress.max_batch_size") == 100
        assert config.get("gateway.ingress.max_concurrent_requests") == 10
        assert config.get("lead_profile.chunk_size_hint") == 5
        assert config.get("protocols.mcp.proxy_profile_data") is False


# ============================== regressions found in adversarial review
class TestSignatureHardening:
    def test_a_non_ascii_signature_is_an_auth_failure_not_a_crash(self):
        """hmac.compare_digest raises TypeError on a non-ASCII str, and Starlette
        decodes headers latin-1 — so one high byte in the header turned an auth
        failure into a 500 and filled the system stream from unauthenticated
        traffic. Asserted at the function, because httpx refuses to *send* a
        non-ASCII header, which is exactly why it slipped through the e2e tests.
        """
        from soloai.protocols.http import SignatureError, verify_v3_signature
        with pytest.raises(SignatureError):
            verify_v3_signature(
                secret=SECRET, method="POST", uri=f"{BASE}/hubspot/events",
                body="[]", timestamp=str(int(time.time() * 1000)),
                signature="sig\u00ff\u00fe")

    def test_a_timestamp_far_in_the_future_is_rejected(self, client):
        """abs() on the age accepted 300s of future skew as well, quietly
        doubling the replay window."""
        future = str(int((time.time() + 3600) * 1000))
        body, headers = signed([make_event()], timestamp=future)
        assert client.post("/hubspot/events", content=body,
                           headers=headers).status_code == 401

    def test_small_clock_skew_is_still_tolerated(self, client):
        soon = str(int((time.time() + 20) * 1000))
        body, headers = signed([make_event()], timestamp=soon)
        assert client.post("/hubspot/events", content=body,
                           headers=headers).status_code == 200


class TestFailClosedConfig:
    """Both of these fail *closed*: the service looks healthy and 401s every
    real webhook forever. /readyz has to say so."""

    def test_readyz_reports_a_missing_app_secret(self, config_dir, registry, monkeypatch):
        monkeypatch.delenv("HUBSPOT_APP_SECRET", raising=False)
        monkeypatch.setenv("LQABR_GATEWAY_PUBLIC_URL", BASE)
        app = gw_server.create_app(config=load_config(config_dir / "config.yaml"),
                                   registry=registry)
        with TestClient(app) as unconfigured:
            response = unconfigured.get("/readyz")
            assert response.status_code == 503
            assert "HUBSPOT_APP_SECRET" in response.json()["config"]

    def test_readyz_reports_a_missing_public_url(self, config_dir, registry, monkeypatch):
        monkeypatch.setenv("HUBSPOT_APP_SECRET", SECRET)
        monkeypatch.delenv("LQABR_GATEWAY_PUBLIC_URL", raising=False)
        app = gw_server.create_app(config=load_config(config_dir / "config.yaml"),
                                   registry=registry)
        with TestClient(app) as unconfigured:
            assert "LQABR_GATEWAY_PUBLIC_URL" in unconfigured.get("/readyz").json()["config"]

    def test_the_problem_is_recorded_at_startup_too(self, config_dir, registry, monkeypatch):
        monkeypatch.delenv("HUBSPOT_APP_SECRET", raising=False)
        monkeypatch.delenv("LQABR_GATEWAY_PUBLIC_URL", raising=False)
        hooks = AuditHooks(sink="file", file_path="/dev/null", keep_records=True)
        audit = gw_audit.GatewayAudit(hooks)
        app = gw_server.create_app(config=load_config(config_dir / "config.yaml"),
                                   registry=registry, audit=audit)
        with TestClient(app):
            pass
        assert [r for r in hooks.records if r["event"] == "configuration_error"]


class TestIngressDoesNotBlockTheEventLoop:
    def test_concurrent_batches_overlap(self, config_dir, registry, monkeypatch):
        """Regression, and the most consequential bug found: the handler was
        `async def` but called blocking requests.post/time.sleep, so ten
        concurrent HubSpot deliveries serialised (measured 30s for what should
        take 3s) and /healthz starved past the container's own 3s HEALTHCHECK.
        """
        import threading

        monkeypatch.setenv("HUBSPOT_APP_SECRET", SECRET)
        monkeypatch.setenv("LQABR_GATEWAY_PUBLIC_URL", BASE)

        class SlowSession:
            def post(self, url, json=None, timeout=None, headers=None, **kw):
                time.sleep(0.4)
                return type("R", (), {"status_code": 200, "text": "{}",
                                      "json": staticmethod(lambda: {"result": {}})})()

        hooks = AuditHooks(sink="file", file_path="/dev/null", keep_records=True)
        audit = gw_audit.GatewayAudit(hooks)
        dispatcher = gw_dispatch.Dispatcher(A2AClient(session=SlowSession()), audit)
        app = gw_server.create_app(config=load_config(config_dir / "config.yaml"),
                                   registry=registry, dispatcher=dispatcher, audit=audit)

        with TestClient(app) as slow:
            body, headers = signed([make_event(event_id="evt-slow")])
            codes: list = []

            def fire() -> None:
                codes.append(slow.post("/hubspot/events", content=body,
                                       headers=headers).status_code)

            started = time.perf_counter()
            threads = [threading.Thread(target=fire) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            elapsed = time.perf_counter() - started

        assert len(codes) == 4
        # Serialised would be >= 1.6s; overlapped is ~0.4s. A generous bound so
        # this asserts "not serialised" rather than a specific speed.
        assert elapsed < 1.2, f"requests serialised: {elapsed:.2f}s for 4x0.4s"


class TestGroupedMode:
    """dispatch.mode = grouped, end to end through the ingress."""

    def _app(self, config_dir, registry, session, monkeypatch, batch_size=20):
        monkeypatch.setenv("HUBSPOT_APP_SECRET", SECRET)
        monkeypatch.setenv("LQABR_GATEWAY_PUBLIC_URL", BASE)
        hooks = AuditHooks(sink="file", file_path="/dev/null", keep_records=True)
        audit = gw_audit.GatewayAudit(hooks)
        dispatcher = gw_dispatch.Dispatcher(
            A2AClient(session=session, backoff_seconds=0, sleep=lambda _s: None),
            audit, mode="grouped", batch_size=batch_size)
        return gw_server.create_app(config=load_config(config_dir / "config.yaml"),
                                    registry=registry, dispatcher=dispatcher,
                                    audit=audit), audit

    def test_twenty_events_produce_two_agent_calls(
            self, config_dir, registry, fake_session_factory, monkeypatch):
        session = fake_session_factory()
        app, _ = self._app(config_dir, registry, session, monkeypatch)
        events = ([make_event("lead_context", "ctx", event_id=f"g{i}")
                   for i in range(14)]
                  + [make_event("lqabr_email_status", "OPENED", event_id=f"h{i}")
                     for i in range(6)])
        body, headers = signed(events)
        with TestClient(app) as client:
            response = client.post("/hubspot/events", content=body, headers=headers)

        assert response.status_code == 200
        assert response.json()["routed"] == 20
        assert len(session.calls) == 2, "20 routed, 2 dispatched"
        assert sorted(d["batch_size"] for d in response.json()["dispatched"]) == [6, 14]

    def test_a_failed_batch_releases_every_member_for_retry(
            self, config_dir, registry, fake_session_factory, fake_response_factory,
            monkeypatch):
        """The bookkeeping that matters: one failed call must free ALL of its
        leads, or HubSpot's retry is deduped away and they are never contacted."""
        session = fake_session_factory([
            fake_response_factory(500, text="down"),   # attempt 1
            fake_response_factory(500, text="down"),   # retry
            fake_response_factory(500, text="down"),   # retry
            fake_response_factory(200),                # the redelivery succeeds
        ])
        app, _ = self._app(config_dir, registry, session, monkeypatch)
        events = [make_event("lead_context", "ctx", event_id=f"f{i}")
                  for i in range(3)]
        with TestClient(app) as client:
            body, headers = signed(events)
            assert client.post("/hubspot/events", content=body,
                               headers=headers).status_code == 503
            retry = [make_event("lead_context", "ctx", event_id=f"f{i}",
                                attempt_number=1) for i in range(3)]
            retry_body, retry_headers = signed(retry)
            second = client.post("/hubspot/events", content=retry_body,
                                 headers=retry_headers)

        assert second.status_code == 200
        assert second.json()["routed"] == 3, "none of the three was deduped away"
