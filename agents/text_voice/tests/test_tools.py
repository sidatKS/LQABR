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

# The three build_assistant_config tests that lived here (endCall tool,
# no analysisPlan, server -> report callback) were deleted 2026-08-07 along
# with the transient assistant they exercised. The first two asserted fields
# that now exist only in the Vapi dashboard assistant; the third is covered
# for the surviving call path by
# test_build_call_payload_dashboard_assistant_sends_id_and_own_server below.


def _lead(**overrides):
    from lqabr_core.types import VoiceLead
    base = dict(
        employee_id="E1", contact_id="123", phone_number="+15550001111",
        full_name="Jane Smith", job_title="VP", company_name="Acme",
        industry="Manufacturing", decision_maker="yes",
    )
    base.update(overrides)
    return VoiceLead(**base)


def test_build_call_payload_carries_ids_for_step_8(tv_tools, monkeypatch):
    monkeypatch.setattr(tv_tools, "VAPI_ASSISTANT_ID", "asst-abc")
    monkeypatch.setattr(tv_tools, "VAPI_PHONE_NUMBER_ID", "phone-abc")
    payload = tv_tools.build_call_payload(_lead())
    assert payload["customer"] == {"number": "+15550001111", "name": "Jane Smith"}
    assert payload["phoneNumberId"] == "phone-abc"
    assert "assistant" not in payload     # the script lives in the dashboard
    variables = payload["assistantOverrides"]["variableValues"]
    assert variables["contact_id"] == "123"
    assert variables["employee_id"] == "E1"
    assert payload["name"] == "lqabr-text-voice-E1"


def test_build_call_payload_dashboard_assistant_sends_id_and_own_server(tv_tools, monkeypatch):
    monkeypatch.setattr(tv_tools, "VAPI_ASSISTANT_ID", "asst-1")
    monkeypatch.setattr(tv_tools, "VAPI_REPORT_CALLBACK_URL",
                        "https://gw.example.com/voice_agent/vapi_report")
    payload = tv_tools.build_call_payload(_lead())
    assert payload["assistantId"] == "asst-1"
    assert "assistant" not in payload
    assert payload["assistantOverrides"]["server"] == {
        "url": "https://gw.example.com/voice_agent/vapi_report",
        "timeoutSeconds": tv_tools.VAPI_REPORT_TIMEOUT_SECONDS,
        "backoffPlan": {"maxRetries": 2, "baseDelaySeconds": 1},
    }


# ----------------------------- the report-delivery failure of 2026-08-18
#
# Two live calls carried `http://localhost:8082/voice_agent/vapi_report` as a
# per-call override while the dashboard held the correct public ngrok URL the
# whole time. The override REPLACES the dashboard's server config, so both
# calls ran and neither result was ever recorded.

def test_no_server_override_when_the_callback_url_is_local(tv_tools, monkeypatch):
    """Sending a local URL is strictly worse than sending nothing: it replaces
    a working dashboard target with a dead one. Omit it instead."""
    monkeypatch.setattr(tv_tools, "VAPI_ASSISTANT_ID", "asst-1")
    monkeypatch.setattr(tv_tools, "VAPI_REPORT_CALLBACK_URL",
                        "http://localhost:8082/voice_agent/vapi_report")
    payload = tv_tools.build_call_payload(_lead())
    assert "server" not in payload["assistantOverrides"]
    assert payload["assistantId"] == "asst-1"      # the call still goes out


def test_no_server_override_when_the_callback_url_is_unset(tv_tools, monkeypatch):
    monkeypatch.setattr(tv_tools, "VAPI_ASSISTANT_ID", "asst-1")
    monkeypatch.setattr(tv_tools, "VAPI_REPORT_CALLBACK_URL", "")
    assert "server" not in tv_tools.build_call_payload(_lead())["assistantOverrides"]


def test_unreachable_url_detection(tv_tools):
    for bad in ("", "http://localhost:8082/x", "http://127.0.0.1:8082/x",
                "http://0.0.0.0:8082/x", "https://LOCALHOST/x"):
        assert tv_tools._is_unreachable(bad) is True, bad
    for good in ("https://gw.example.com/x",
                 "https://irregular-thread-uninstall.ngrok-free.dev/voice_agent/vapi_report"):
        assert tv_tools._is_unreachable(good) is False, good


def test_secret_header_rides_on_the_override_when_configured(tv_tools, monkeypatch):
    monkeypatch.setattr(tv_tools, "VAPI_ASSISTANT_ID", "asst-1")
    monkeypatch.setattr(tv_tools, "VAPI_REPORT_CALLBACK_URL", "https://gw.example.com/r")
    monkeypatch.setattr(tv_tools, "VAPI_WEBHOOK_SECRET", "s3cret")
    server = tv_tools.build_call_payload(_lead())["assistantOverrides"]["server"]
    assert server["headers"] == {"x-vapi-secret": "s3cret"}


def test_build_call_payload_omits_hubspot_ids_when_absent(tv_tools, monkeypatch):
    monkeypatch.setattr(tv_tools, "VAPI_ASSISTANT_ID", "asst-1")
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
    # Pin the assistant id rather than depending on a local .env: it is
    # mandatory since 2026-08-07 (build_call_payload raises without it) and it
    # is what place_call reports as "dashboard".
    monkeypatch.setattr(tv_tools, "VAPI_ASSISTANT_ID", "asst-1")
    result = tv_tools.place_call(_lead(), lead_context="Follow-up on the cloud migration email")
    assert result == {
        "status": "initiated", "call_id": "call-9", "call_status": "queued",
        "contact_id": "123", "to": "+15550001111", "assistant": "dashboard",
        "lead_context": "Follow-up on the cloud migration email",
        "lead_context_chars": 38,
    }
    assert len(fake.payloads) == 1


# ------------------------------------------------------------ SP-2 FR4: lead_context

def test_build_call_payload_always_sends_lead_context_even_when_empty(tv_tools, monkeypatch):
    """Vapi leaves an unknown {{variable}} in the prompt verbatim, so omitting
    the key would put the literal "{{lead_context}}" into the assistant's
    instructions. An empty string is what makes the dashboard prompt's
    LiquidJS `{% if lead_context %}` fallback take the generic-intro branch."""
    monkeypatch.setattr(tv_tools, "VAPI_ASSISTANT_ID", "asst-abc")
    variables = tv_tools.build_call_payload(_lead())["assistantOverrides"]["variableValues"]
    assert variables["lead_context"] == ""

    variables = tv_tools.build_call_payload(_lead(), lead_context=None)[
        "assistantOverrides"]["variableValues"]
    assert variables["lead_context"] == ""


def test_build_call_payload_passes_lead_context_through(tv_tools, monkeypatch):
    monkeypatch.setattr(tv_tools, "VAPI_ASSISTANT_ID", "asst-abc")
    payload = tv_tools.build_call_payload(
        _lead(), lead_context="We emailed you about cutting cloud spend.")
    variables = payload["assistantOverrides"]["variableValues"]
    assert variables["lead_context"] == "We emailed you about cutting cloud spend."


def test_cap_lead_context_normalises_whitespace_and_leaves_short_text_alone(tv_tools):
    assert tv_tools.cap_lead_context("  cloud   migration\n email ") == "cloud migration email"
    assert tv_tools.cap_lead_context(None) == ""
    assert tv_tools.cap_lead_context("") == ""


def test_cap_lead_context_truncates_on_a_word_boundary(tv_tools, monkeypatch):
    monkeypatch.setattr(tv_tools, "LEAD_CONTEXT_MAX_CHARS", 20)
    capped = tv_tools.cap_lead_context("alpha bravo charlie delta echo foxtrot")
    assert capped == "alpha bravo charlie…"
    assert len(capped) <= 21          # 20 chars + the ellipsis
    assert not capped.startswith("alpha bravo charlie d")   # no half word


def test_cap_lead_context_still_cuts_a_single_oversized_token(tv_tools, monkeypatch):
    monkeypatch.setattr(tv_tools, "LEAD_CONTEXT_MAX_CHARS", 10)
    assert tv_tools.cap_lead_context("x" * 50) == "x" * 10 + "…"


def test_place_call_caps_lead_context_at_the_boundary(tv_tools, monkeypatch):
    """The cap is enforced HERE, on the last line before the wire — not only
    wherever the value happened to be read."""
    class FakeVapi:
        def __init__(self):
            self.payloads = []

        def create_call(self, payload):
            self.payloads.append(payload)
            return {"id": "call-1", "status": "queued"}

    fake = FakeVapi()
    monkeypatch.setattr(tv_tools, "_vapi", lambda: fake)
    monkeypatch.setattr(tv_tools, "VAPI_PHONE_NUMBER_ID", "phone-1")
    monkeypatch.setattr(tv_tools, "VAPI_ASSISTANT_ID", "asst-1")
    monkeypatch.setattr(tv_tools, "LEAD_CONTEXT_MAX_CHARS", 12)

    result = tv_tools.place_call(_lead(), lead_context="one two three four five")
    sent = fake.payloads[0]["assistantOverrides"]["variableValues"]["lead_context"]
    assert sent == "one two…"
    # The log field reports what was actually SENT, not what was passed in.
    assert result["lead_context_chars"] == len(sent)


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
    """The id is HubSpot's own internal contact id for the enrolled record
    (confirmed against HubSpot's custom-workflow-action docs), not the old
    `employee_id`. The wire key is snake_case `object_id` only —
    `_extract_object_id` deliberately does not accept camelCase `objectId`,
    per explicit instruction, so this test used to assert the opposite of
    what the code guarantees (corrected 2026-08-17)."""
    resp = client.post("/voice_agent/lead", json={"object_id": "904"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["object_id"] == "904"
    assert body["correlation_id"]
    assert client.recorded["lead"] == [("904", body["correlation_id"])]


def test_lead_route_reuses_inbound_correlation_id(client):
    resp = client.post("/voice_agent/lead", json={"object_id": "904"},
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
