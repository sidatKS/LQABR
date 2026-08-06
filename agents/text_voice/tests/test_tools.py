"""Unit tests for agents/text_voice/src/tools.py — Steps 2 and 4.

Covers: the transient assistant/call payload builders, VapiClient's HTTP/retry
behaviour (same contract as HubSpotClient, mirrored here), place_call's
boundary checks, and the two inbound routes (/voice_agent/lead, /voice_agent/vapi_report).

The routes are tested with `_handoff_new_lead`/`_handoff_call_report`
monkeypatched out. Those two functions import `text_voice.py`, which imports
`google.adk` at module scope — exercising that import chain belongs to
test_text_voice.py (which stubs it in via conftest's `tv_agent` fixture), not
here. What belongs here is Step 2/6→7's own job: parse, validate, answer 200
fast, and schedule the right handoff with the right arguments — independent of
what the handoff eventually does.
"""

import json

import pytest
from fastapi.testclient import TestClient


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Scripted requests.Session stand-in; records every call."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


# ============================================================ payload builders

def test_build_assistant_config_uses_endcall_tool_not_removed_field(tv_tools):
    """Regression tests for two live Vapi 400s: `endCallFunctionEnabled` is
    gone from Vapi's schema (the `endCall` tool replaced it), and tools live
    under model.tools — a top-level assistant.tools gets HTTP 400
    "assistant.property tools should not exist" (verified live)."""
    config = tv_tools.build_assistant_config()
    assert config["model"]["tools"] == [{"type": "endCall"}]
    assert "tools" not in config          # never top-level on the assistant
    assert "endCallFunctionEnabled" not in config


def test_build_assistant_config_has_no_analysis_plan(tv_tools):
    """Step 7's own model decides the outcome — paying Vapi for a prebuilt
    analysisPlan we then ignore would be waste and a second, disagreeing
    verdict in the logs."""
    assert "analysisPlan" not in tv_tools.build_assistant_config()


def test_build_assistant_config_server_points_at_report_callback(tv_tools):
    config = tv_tools.build_assistant_config()
    assert config["server"] == {
        "url": tv_tools.VAPI_REPORT_CALLBACK_URL,
        "timeoutSeconds": tv_tools.VAPI_REPORT_TIMEOUT_SECONDS,
        "backoffPlan": {"maxRetries": 2, "baseDelaySeconds": 1},
    }
    assert config["serverMessages"] == ["end-of-call-report"]


def _lead(**overrides):
    from lqabr_core.types import VoiceLead
    base = dict(
        employee_id="E1", contact_id="123", phone_number="+15550001111",
        full_name="Jane Smith", job_title="VP", company_name="Acme",
        industry="Manufacturing", decision_maker="yes",
    )
    base.update(overrides)
    return VoiceLead(**base)


def test_build_call_payload_transient_assistant_carries_ids_for_step_8(tv_tools, monkeypatch):
    monkeypatch.setattr(tv_tools, "VAPI_ASSISTANT_ID", "")
    monkeypatch.setattr(tv_tools, "VAPI_PHONE_NUMBER_ID", "phone-abc")
    payload = tv_tools.build_call_payload(_lead())
    assert payload["customer"] == {"number": "+15550001111", "name": "Jane Smith"}
    assert payload["phoneNumberId"] == "phone-abc"
    assert "assistantId" not in payload
    assert payload["assistant"]["model"]["tools"] == [{"type": "endCall"}]
    variables = payload["assistantOverrides"]["variableValues"]
    assert variables["contact_id"] == "123"
    assert variables["employee_id"] == "E1"
    assert payload["name"] == "lqabr-text-voice-E1"


def test_build_call_payload_dashboard_assistant_sends_id_and_own_server(tv_tools, monkeypatch):
    monkeypatch.setattr(tv_tools, "VAPI_ASSISTANT_ID", "asst-1")
    payload = tv_tools.build_call_payload(_lead())
    assert payload["assistantId"] == "asst-1"
    assert "assistant" not in payload
    assert payload["assistantOverrides"]["server"] == {
        "url": tv_tools.VAPI_REPORT_CALLBACK_URL,
        "timeoutSeconds": tv_tools.VAPI_REPORT_TIMEOUT_SECONDS,
        "backoffPlan": {"maxRetries": 2, "baseDelaySeconds": 1},
    }


def test_build_call_payload_omits_hubspot_ids_when_absent(tv_tools, monkeypatch):
    monkeypatch.setattr(tv_tools, "VAPI_ASSISTANT_ID", "")
    payload = tv_tools.build_call_payload(_lead(contact_id=None, employee_id=None))
    variables = payload["assistantOverrides"]["variableValues"]
    assert "contact_id" not in variables
    assert "employee_id" not in variables


# =================================================================== VapiClient

def make_vapi_client(tv_tools, responses, api_key="test-vapi-key"):
    session = FakeSession(responses)
    return tv_tools.VapiClient(api_key=api_key, session=session, backoff_seconds=0), session


def test_vapi_client_create_call_posts_to_call(tv_tools):
    client, session = make_vapi_client(tv_tools, [FakeResponse(200, {"id": "call-1", "status": "queued"})])
    result = client.create_call({"assistant": {}})
    assert result == {"id": "call-1", "status": "queued"}
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url.endswith("/call")
    assert kwargs["json"] == {"assistant": {}}
    assert kwargs["headers"]["Authorization"] == "Bearer test-vapi-key"


def test_vapi_client_retries_on_5xx_then_raises(tv_tools):
    client, session = make_vapi_client(tv_tools, [FakeResponse(500), FakeResponse(502), FakeResponse(503)])
    with pytest.raises(tv_tools.VapiError, match="after 3 retries"):
        client.create_call({})
    assert len(session.calls) == 3


def test_vapi_client_recovers_after_a_5xx_retry(tv_tools):
    """The retry-then-succeed path: distinct from 'fails all 3' and 'fails
    immediately' — this is the actual value retrying provides."""
    client, session = make_vapi_client(
        tv_tools, [FakeResponse(500), FakeResponse(200, {"id": "call-2"})])
    result = client.create_call({})
    assert result == {"id": "call-2"}
    assert len(session.calls) == 2


def test_vapi_client_4xx_raises_immediately_without_retry(tv_tools):
    client, session = make_vapi_client(tv_tools, [FakeResponse(400, {"message": "bad assistant config"})])
    with pytest.raises(tv_tools.VapiError, match="HTTP 400"):
        client.create_call({})
    assert len(session.calls) == 1


def test_vapi_client_never_retries_a_2xx_so_a_lead_is_never_dialed_twice(tv_tools):
    client, session = make_vapi_client(tv_tools, [FakeResponse(200, {"id": "call-3"})])
    client.create_call({})
    assert len(session.calls) == 1


def test_vapi_client_audit_logs_every_attempt(tv_tools, monkeypatch):
    """Every attempt must reach audit_log with a credential *reference* (never
    the key itself), the status code, and which service this was for — the
    same contract HubSpotClient's _request already proved out. Asserting on
    the `obs.log_http_out` call directly (rather than parsing captured stdout)
    sidesteps `observability.configure()`'s one-time-per-process handler setup,
    which is idempotent by design and so cannot be re-armed mid-suite."""
    from lqabr_core import observability as obs
    calls = []
    monkeypatch.setattr(obs, "log_http_out", lambda *a, **kw: calls.append((a, kw)))
    client, _ = make_vapi_client(tv_tools, [FakeResponse(200, {"id": "call-4"})])
    client.create_call({})
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["status_code"] == 200
    assert kwargs["service"] == "vapi"
    assert kwargs["attempt"] == 1
    assert kwargs["credential"] == "injected:<len=13 ...-key>"
    assert "test-vapi-key" not in kwargs["credential"]  # the raw key never leaks in full


def test_vapi_client_audit_logs_each_retry_attempt_number(tv_tools, monkeypatch):
    from lqabr_core import observability as obs
    calls = []
    monkeypatch.setattr(obs, "log_http_out", lambda *a, **kw: calls.append(kw))
    client, _ = make_vapi_client(tv_tools, [FakeResponse(500), FakeResponse(200, {"id": "x"})])
    client.create_call({})
    assert [c["attempt"] for c in calls] == [1, 2]
    assert calls[0]["status_code"] == 500
    assert calls[1]["status_code"] == 200


# ======================================================================= place_call

def test_place_call_rejects_missing_phone_without_calling_vapi(tv_tools, monkeypatch):
    called = []
    monkeypatch.setattr(tv_tools, "_vapi", lambda: called.append("called"))
    monkeypatch.setattr(tv_tools, "VAPI_PHONE_NUMBER_ID", "phone-1")
    result = tv_tools.place_call(_lead(phone_number=None))
    assert result["error"].startswith("bad-data")
    assert not called


def test_place_call_rejects_opted_out_without_calling_vapi(tv_tools, monkeypatch):
    called = []
    monkeypatch.setattr(tv_tools, "_vapi", lambda: called.append("called"))
    monkeypatch.setattr(tv_tools, "VAPI_PHONE_NUMBER_ID", "phone-1")
    result = tv_tools.place_call(_lead(opted_out=True))
    assert result["error"].startswith("opted-out")
    assert not called


def test_place_call_rejects_missing_phone_number_id_config(tv_tools, monkeypatch):
    called = []
    monkeypatch.setattr(tv_tools, "_vapi", lambda: called.append("called"))
    monkeypatch.setattr(tv_tools, "VAPI_PHONE_NUMBER_ID", "")
    result = tv_tools.place_call(_lead())
    assert result["error"].startswith("config-error")
    assert not called


def test_place_call_success_returns_call_id_and_dials_once(tv_tools, monkeypatch):
    class FakeVapi:
        def __init__(self):
            self.payloads = []

        def create_call(self, payload):
            self.payloads.append(payload)
            return {"id": "call-9", "status": "queued"}

    fake = FakeVapi()
    monkeypatch.setattr(tv_tools, "_vapi", lambda: fake)
    monkeypatch.setattr(tv_tools, "VAPI_PHONE_NUMBER_ID", "phone-1")
    # Force the transient path regardless of a local .env that sets
    # LQABR_VAPI_ASSISTANT_ID (which would make place_call report "dashboard").
    monkeypatch.setattr(tv_tools, "VAPI_ASSISTANT_ID", "")
    result = tv_tools.place_call(_lead())
    assert result == {
        "status": "initiated", "call_id": "call-9", "call_status": "queued",
        "contact_id": "123", "to": "+15550001111", "assistant": "transient",
    }
    assert len(fake.payloads) == 1


# ============================================================================ routes

@pytest.fixture
def client(tv_tools, monkeypatch):
    """A TestClient with the handoffs stubbed out — see module docstring for why."""
    recorded = {"lead": [], "report": []}
    monkeypatch.setattr(tv_tools, "_handoff_new_lead",
                        lambda contact_id, correlation_id: recorded["lead"].append(
                            (contact_id, correlation_id)))
    monkeypatch.setattr(tv_tools, "_handoff_call_report",
                        lambda message, correlation_id: recorded["report"].append(
                            (message, correlation_id)))
    test_client = TestClient(tv_tools.app)
    test_client.recorded = recorded
    return test_client


def test_lead_route_accepts_and_schedules_handoff(client):
    """`objectId` is HubSpot's own field for the enrolled record's real
    contact id (confirmed against HubSpot's custom-workflow-action docs) —
    the gateway forwards it under that name, not the old `employee_id`."""
    resp = client.post("/voice_agent/lead", json={"objectId": "904"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["object_id"] == "904"
    assert body["correlation_id"]
    assert client.recorded["lead"] == [("904", body["correlation_id"])]


def test_lead_route_reuses_inbound_correlation_id(client):
    resp = client.post("/voice_agent/lead", json={"objectId": "904"},
                        headers={"x-correlation-id": "abc-123"})
    assert resp.json()["correlation_id"] == "abc-123"
    assert client.recorded["lead"] == [("904", "abc-123")]


def test_lead_route_rejects_missing_object_id(client):
    resp = client.post("/voice_agent/lead", json={"foo": "bar"})
    assert resp.status_code == 400
    assert not client.recorded["lead"]


def test_lead_route_rejects_malformed_json(client):
    resp = client.post("/voice_agent/lead", content=b"not json", headers={"content-type": "application/json"})
    assert resp.status_code == 400
    assert not client.recorded["lead"]


def test_call_report_route_unwraps_message_envelope(client):
    envelope = {"message": {"type": "end-of-call-report", "call": {"id": "call-1"},
                            "endedReason": "customer-ended-call"}}
    resp = client.post("/voice_agent/vapi_report", json=envelope)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["call_id"] == "call-1"
    assert client.recorded["report"] == [(envelope["message"], body["correlation_id"])]


def test_call_report_route_accepts_unwrapped_message(client):
    """The gateway may forward the envelope verbatim or strip the wrapper —
    both shapes must work."""
    message = {"type": "end-of-call-report", "call": {"id": "call-2"}}
    resp = client.post("/voice_agent/vapi_report", json=message)
    assert resp.status_code == 200
    assert client.recorded["report"] == [(message, resp.json()["correlation_id"])]


def test_call_report_route_ignores_non_report_message_types(client):
    resp = client.post("/voice_agent/vapi_report", json={"message": {"type": "status-update"}})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored", "message_type": "status-update"}
    assert not client.recorded["report"]


def test_call_report_route_rejects_malformed_json(client):
    resp = client.post("/voice_agent/vapi_report", content=b"{not json", headers={"content-type": "application/json"})
    assert resp.status_code == 400
    assert not client.recorded["report"]


def test_healthz(tv_tools):
    test_client = TestClient(tv_tools.app)
    resp = test_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ======================================================================= _correlation_id

class _FakeRequest:
    def __init__(self, headers):
        self.headers = headers


def test_correlation_id_prefers_x_correlation_id(tv_tools):
    req = _FakeRequest({"x-correlation-id": "cid-1", "x-request-id": "rid-1"})
    assert tv_tools._correlation_id(req) == "cid-1"


def test_correlation_id_falls_back_to_x_request_id(tv_tools):
    req = _FakeRequest({"x-request-id": "rid-1"})
    assert tv_tools._correlation_id(req) == "rid-1"


def test_correlation_id_mints_one_when_neither_header_present(tv_tools):
    req = _FakeRequest({})
    minted = tv_tools._correlation_id(req)
    assert minted
    assert minted != tv_tools._correlation_id(_FakeRequest({}))  # not reused across calls


def test_vapi_client_is_env_only_never_secret_manager(tv_tools, monkeypatch):
    """Decision 2026-07-31: the Vapi key is env-only. A missing env var must
    fail fast with a clear message — never fall through to Secret Manager
    (whose gRPC retry loop was observed live hanging a dial for 60s on a
    machine with stale gcloud auth)."""
    import pytest
    monkeypatch.delenv("LQABR_VAPI_API_KEY", raising=False)
    with pytest.raises(tv_tools.VapiError) as excinfo:
        tv_tools.VapiClient()
    assert "env-only" in str(excinfo.value)
    monkeypatch.setenv("LQABR_VAPI_API_KEY", "test-key-from-env")
    client = tv_tools.VapiClient()
    assert client._key == "test-key-from-env"
