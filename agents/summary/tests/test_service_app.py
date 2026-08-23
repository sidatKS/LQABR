"""The HTTP surface.

What this file owns is the EDGE: the envelopes in, the payloads out, and the
route index. The run itself is covered by test_pipeline.py, so `run_summary`
is stubbed here — that keeps these tests about translation, which is where
an A2A envelope quietly loses an object id.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import service_app
from schema import SourceInfo, SummaryPayload, SummaryRequest, SummaryResponse


@pytest.fixture(autouse=True)
def _no_startup_mcp_call(monkeypatch):
    """The MCP is a separate container; the tests never dial it."""
    monkeypatch.setenv("LQABR_SUMMARY_MCP_STARTUP_CHECK", "off")


@pytest.fixture
def captured(monkeypatch):
    """Stub the run, keep the request it was given."""
    seen = {}

    def fake_run(request: SummaryRequest, **kwargs):
        seen["request"] = request
        return SummaryResponse(
            run_id="sum-test", status="completed",
            source=SourceInfo(kind=request.to_spec().kind, reference="ref"),
            summary=SummaryPayload(summary="It shipped."), model="m")

    monkeypatch.setattr(service_app, "run_summary", fake_run)
    return seen


@pytest.fixture
def client():
    with TestClient(service_app.app) as test_client:
        yield test_client


class TestIdentity:
    def test_root_is_an_index_not_a_404(self, client):
        body = client.get("/").json()
        assert body["service"] == "lqabr-summary-agent"
        assert "POST /summary/run" in body["routes"]
        assert "GET /mcp/tools" in body["routes"]

    def test_health_and_healthz_are_identical(self, client):
        assert client.get("/health").json() == client.get("/healthz").json()

    def test_health_reports_what_this_instance_is_bound_to(self, client):
        body = client.get("/health").json()
        assert body["status"] == "UP"
        assert body["mcp"]["write_tool"] == "post_patch_crm"
        assert body["hubspot"]["summary_property"] == "blog_summary"
        assert "auth" not in json.dumps(body).lower() or "token" not in json.dumps(body).lower()


class TestSummaryRun:
    def test_bare_url_string(self, client, captured):
        response = client.post("/summary/run", json={"source": "https://example.com/x"})
        assert response.status_code == 200
        assert response.json()["summary"]["summary"] == "It shipped."
        assert captured["request"].to_spec().kind == "url"

    def test_full_source_object_with_hubspot_target(self, client, captured):
        client.post("/summary/run", json={
            "source": {"kind": "api", "endpoint": "https://svc/x", "select": "$.a"},
            "hubspot": {"object_id": "t-1", "industry": "Fintech"}})
        request = captured["request"]
        assert request.to_spec().select == "$.a"
        assert request.hubspot.object_id == "t-1"
        assert request.hubspot.industry == "Fintech"

    def test_a_missing_source_is_a_422_not_a_500(self, client):
        assert client.post("/summary/run", json={}).status_code == 422

    def test_chat_only_deployment_does_not_serve_the_api(self, monkeypatch, captured):
        monkeypatch.setenv("LQABR_SUMMARY_ROUTES", "chat")
        with TestClient(service_app.app) as client:
            assert client.post("/summary/run", json={"source": "x"}).status_code == 404


class TestA2A:
    """The gateway's envelope. Losing the object id here means a silent no-write."""

    @staticmethod
    def envelope(text: str, **extra) -> dict:
        body = {"jsonrpc": "2.0", "id": "1", "method": "message/send",
                "params": {"message": {"role": "user",
                                       "parts": [{"kind": "text", "text": text}],
                                       "messageId": "m1"}}}
        body["params"].update(extra.pop("params", {}))
        body.update(extra)
        return body

    def test_message_text_becomes_the_source(self, client, captured):
        response = client.post("/summary/a2a", json=self.envelope("https://example.com/x"))
        assert response.status_code == 200
        assert captured["request"].to_spec().kind == "url"

    def test_object_id_from_params_metadata(self, client, captured):
        client.post("/summary/a2a", json=self.envelope(
            "https://example.com/x", params={"metadata": {"object_id": "t-9"}}))
        assert captured["request"].hubspot.object_id == "t-9"

    def test_object_id_from_the_gateways_top_level_mirror(self, client, captured):
        client.post("/summary/a2a", json=self.envelope("https://example.com/x", objectId="t-7"))
        assert captured["request"].hubspot.object_id == "t-7"

    def test_summary_ref_id_is_accepted_as_the_target(self, client, captured):
        client.post("/summary/a2a", json=self.envelope(
            "https://example.com/x", params={"metadata": {"summary_ref_id": "ticket-42"}}))
        assert captured["request"].hubspot.object_id == "ticket-42"

    def test_a_full_request_object_may_be_sent_as_the_text(self, client, captured):
        payload = json.dumps({"source": {"kind": "json", "payload": {"a": 1}}})
        client.post("/summary/a2a", json=self.envelope(payload))
        assert captured["request"].to_spec().kind == "json"

    def test_no_object_id_means_summarise_without_writing(self, client, captured):
        client.post("/summary/a2a", json=self.envelope("https://example.com/x"))
        assert captured["request"].hubspot is None

    @pytest.mark.parametrize("body,status,match", [
        ({"jsonrpc": "2.0", "method": "tasks/get", "params": {}}, 400, "unsupported A2A method"),
        ({"jsonrpc": "2.0", "method": "message/send", "params": {}}, 400, "no text"),
    ])
    def test_bad_envelopes_are_refused_with_a_reason(self, client, captured, body, status, match):
        response = client.post("/summary/a2a", json=body)
        assert response.status_code == status
        assert match in response.json()["detail"]

    def test_malformed_json_text_is_named(self, client, captured):
        response = client.post("/summary/a2a", json=self.envelope("{not json"))
        assert response.status_code == 400
        assert "not valid JSON" in response.json()["detail"]


class TestMcpTools:
    def test_reports_live_tools_and_what_is_missing(self, client, monkeypatch):
        class FakeClient:
            def list_tools(self):
                return ["patch_ticket", "get_lead_profile_details"]

        class FakeHubSpot:
            def __init__(self, *args, **kwargs):
                self.client = FakeClient()

        monkeypatch.setattr(service_app, "HubSpotMCP", FakeHubSpot)
        body = client.get("/mcp/tools").json()
        assert body["tools"] == ["get_lead_profile_details", "patch_ticket"]
        assert body["missing"] == ["list_trigger_leads", "post_patch_crm"]

    def test_an_unreachable_mcp_is_a_502_with_the_reason(self, client, monkeypatch):
        from summary_core.mcp.client import MCPError

        class FailingClient:
            def list_tools(self):
                raise MCPError("connection refused")

        class FakeHubSpot:
            def __init__(self, *args, **kwargs):
                self.client = FailingClient()

        monkeypatch.setattr(service_app, "HubSpotMCP", FakeHubSpot)
        response = client.get("/mcp/tools")
        assert response.status_code == 502
        assert "connection refused" in response.json()["detail"]
