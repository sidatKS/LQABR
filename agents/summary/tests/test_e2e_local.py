"""End to end over real sockets, with no cloud and no API key.

Everything else in this suite injects a fake session somewhere. This file
does not: it stands up a REAL HTTP server that speaks MCP JSON-RPC, points
the agent's `LQABR_SUMMARY_MCP_BASE_URL` at it, and drives the real FastAPI
app. The only thing faked is the model, because a summary needs judgement
and a test must not need a key.

What that proves, which the unit tests cannot: the wire format is right.
Headers, the Mcp-Session-Id round trip, content-block unwrapping and the
JSON-RPC envelope all have to be correct against a server that was not
written to be convenient.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

import service_app
import summarizer

GOOD_SUMMARY = {
    "title": "Spring Boot 4", "topic": "Java frameworks",
    "summary": "Spring Boot 4 shipped with virtual threads.",
    "key_points": ["virtual threads", "GraalVM images"],
    "concepts": ["JVM concurrency"], "technologies": ["Spring Boot", "GraalVM"],
    "takeaways": ["plan the upgrade"], "industry": "Software",
}

WRITES: list = []


class StubMCP(BaseHTTPRequestHandler):
    """A HubSpot MCP, small but honest: real JSON-RPC over a real socket."""

    TOOLS = ["get_lead_profile_details", "list_trigger_leads", "post_patch_crm"]

    def log_message(self, *args):  # keep pytest output clean
        return

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's name
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        method = request.get("method")

        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return

        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {},
                      "serverInfo": {"name": "stub-hubspot-mcp"}}
            self._reply(request, result, session="stub-session-1")
            return

        if method == "tools/list":
            self._reply(request, {"tools": [{"name": name} for name in self.TOOLS]})
            return

        if method == "tools/call":
            params = request.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "post_patch_crm":
                # A real MCP echoes the session header back to us; assert the
                # client sent one rather than trusting it did.
                assert self.headers.get("Mcp-Session-Id") == "stub-session-1"
                WRITES.append(arguments)
                payload = {"status": "written", "object_id": arguments.get("object_id"),
                           "properties": sorted(arguments.get("properties") or {})}
            elif name == "get_lead_profile_details":
                payload = {"object_id": arguments.get("object_id"), "industry": "Fintech"}
            else:
                payload = {"leads": []}
            # Tool results come back as content blocks, as the protocol says.
            self._reply(request, {"content": [{"type": "text", "text": json.dumps(payload)}]})
            return

        self._reply(request, {}, error={"code": -32601, "message": f"unknown method {method}"})

    def _reply(self, request, result, *, session=None, error=None):
        body = {"jsonrpc": "2.0", "id": request.get("id")}
        body["error" if error else "result"] = error or result
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture(scope="module")
def mcp_url():
    server = HTTPServer(("127.0.0.1", 0), StubMCP)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/mcp"
    server.shutdown()


@pytest.fixture
def client(monkeypatch, mcp_url):
    WRITES.clear()
    monkeypatch.setenv("LQABR_SUMMARY_MCP_BASE_URL", mcp_url)
    monkeypatch.setenv("LQABR_SUMMARY_MCP_STARTUP_CHECK", "strict")
    monkeypatch.setattr(
        summarizer, "_default_completion",
        lambda **kwargs: {"choices": [{"message": {"content": json.dumps(GOOD_SUMMARY)}}]})
    with TestClient(service_app.app) as test_client:
        yield test_client


def test_startup_discovered_the_real_server(client):
    """`strict` means the app only came up because the handshake succeeded."""
    health = client.get("/health").json()
    assert health["mcp"]["reachable"] is True
    assert health["mcp"]["tools"] == ["get_lead_profile_details", "list_trigger_leads",
                                      "post_patch_crm"]


def test_mcp_tools_endpoint_answers_from_the_live_server(client):
    body = client.get("/mcp/tools").json()
    assert body["missing"] == []
    assert "post_patch_crm" in body["tools"]


def test_text_to_hubspot_over_the_wire(client):
    response = client.post("/summary/run", json={
        "source": {"kind": "text", "text": "Spring Boot 4 shipped with virtual threads."},
        "hubspot": {"object_id": "ticket-777"}})

    body = response.json()
    assert body["status"] == "completed"
    assert body["hubspot"]["status"] == "written"
    assert body["hubspot"]["properties"] == ["blog_industry", "blog_summary"]

    assert len(WRITES) == 1
    written = WRITES[0]
    assert written["object_id"] == "ticket-777"
    assert "Spring Boot 4" in written["properties"]["blog_summary"]
    assert "virtual threads" in written["properties"]["blog_summary"]
    assert written["properties"]["blog_industry"] == "SOFTWARE"   # normalised to the portal enum form


def test_a2a_envelope_over_the_wire(client):
    response = client.post("/summary/a2a", json={
        "jsonrpc": "2.0", "id": "1", "method": "message/send",
        "params": {"message": {"role": "user", "messageId": "m1",
                               "parts": [{"kind": "text", "text": "prose to summarise"}]},
                   "metadata": {"object_id": "ticket-888", "trigger_id": "t-1"}}})
    assert response.json()["hubspot"]["status"] == "written"
    assert WRITES[-1]["object_id"] == "ticket-888"


def test_json_source_end_to_end(client):
    response = client.post("/summary/run", json={
        "source": {"kind": "json", "payload": {"report": {"body": "Quarterly numbers."}},
                   "select": "$.report.body"},
        "hubspot": {"object_id": "ticket-999", "industry": "Fintech"}})
    assert response.json()["status"] == "completed"
    assert WRITES[-1]["properties"]["blog_industry"] == "FINTECH", "the caller's industry wins (normalised)"


def test_dry_run_reaches_the_server_for_discovery_but_never_writes(client, monkeypatch):
    monkeypatch.setenv("LQABR_SUMMARY_DRY_RUN", "1")
    with TestClient(service_app.app) as dry_client:
        body = dry_client.post("/summary/run", json={
            "source": "prose", "hubspot": {"object_id": "ticket-000"}}).json()
    assert body["hubspot"]["status"] == "dry_run"
    assert all(w["object_id"] != "ticket-000" for w in WRITES)
