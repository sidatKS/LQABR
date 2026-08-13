"""Integration test: does the orchestrator's dispatch_cycle actually reach a
running Email Agent over real HTTP (A2A) and trigger it to send an email?

Unlike the per-agent unit tests (which monkeypatch A2ADispatcher/HubSpotClient
in isolation), this spins up a real local HTTP server standing in for the
Email Agent's A2A endpoint and drives a genuine socket round-trip:

    orchestrator_agent.dispatch_cycle()
        -> real A2ADispatcher.send_message() (a real requests.post over HTTP)
        -> local stub server (extracts the lead's email from the instruction
           text the way the Email Agent's own model would, then calls the
           REAL email_agent.send_outreach_email tool — actual code, not a
           mock of "what it would do")
        -> send_outreach_email's HubSpotClient/MailgunClient calls are faked
           so the test needs no real credentials, sends no real email, and
           costs nothing

No real LLM call, no ANTHROPIC_API_KEY, no real HubSpot/Mailgun account is
used or required. This proves the wiring (does dispatch_cycle really reach
the Email Agent and cause its real send path to run) and the deterministic
business logic (personalization, stage promotion, cooldown stamping) — not
model behavior, which is out of scope for a repeatable offline test.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import email_agent
import orchestrator_agent
from lqabr_core.types import LeadProfile, LeadStage

EMAIL_RE = re.compile(r"email ([\w.+-]+@[\w.-]+\.[\w.-]+)")


class FakeHubSpot:
    """One shared fake CRM used by both sides of the wire — the orchestrator
    reads the profiled queue from it and stamps the dispatch cooldown on it;
    the Email Agent looks the lead up and writes its stage transition to it.
    A real HubSpotClient talking to a real portal would keep both views
    consistent; this fake mirrors that by being the single shared source."""

    def __init__(self, lead):
        self.lead = lead
        self.stage_calls = []
        self.mark_dispatched_calls = []

    # --- orchestrator side ---
    def leads_in_stage(self, stage, min_probability=0, limit=100):
        return [self.lead] if self.lead.stage is stage else []

    def mark_dispatched(self, contact_id, occurred_at=None):
        self.mark_dispatched_calls.append((contact_id, occurred_at))

    # --- email agent side ---
    def find_lead_by_email(self, email):
        return self.lead if self.lead.email == email else None

    def set_stage(self, contact_id, stage, reason=None):
        self.stage_calls.append((contact_id, stage, reason))
        self.lead.stage = stage


class FakeMailgun:
    sent = []

    def __init__(self, *args, **kwargs):
        pass

    def send_email(self, **kwargs):
        FakeMailgun.sent.append(kwargs)
        return {"id": "<integration-test@mg>"}


def make_stub_email_agent_server():
    """A minimal A2A JSON-RPC server standing in for a running Email Agent
    process. It parses the instruction text the same way the real agent's
    model would (who am I being asked to email?) and then calls the real
    send_outreach_email tool — the actual send logic under test."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence request logging
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            text = body["params"]["message"]["parts"][0]["text"]
            match = EMAIL_RE.search(text)
            result = (email_agent.send_outreach_email(contact_email=match.group(1))
                      if match else {"error": "could not parse recipient from instruction"})
            payload = {"jsonrpc": "2.0", "id": body.get("id"), "result": result}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_orchestrator_dispatch_triggers_email_agent_end_to_end(monkeypatch):
    lead = LeadProfile(full_name="Jane Smith", email="jane@acme.example",
                       company="Acme", job_title="VP", industry="Manufacturing",
                       stage=LeadStage.PROFILED, hubspot_contact_id="42")
    hub = FakeHubSpot(lead)

    # Email Agent side: fake HubSpot + Mailgun so its real send_outreach_email
    # code runs end to end with zero real network calls.
    monkeypatch.setattr(email_agent, "HubSpotClient", lambda: hub)
    FakeMailgun.sent = []
    monkeypatch.setattr(email_agent, "MailgunClient", FakeMailgun)

    server = make_stub_email_agent_server()
    port = server.server_address[1]
    monkeypatch.setenv("LQABR_EMAIL_AGENT_URL", f"http://127.0.0.1:{port}")

    # Orchestrator side: same shared fake CRM for the stage-queue read and
    # the dispatch-cooldown stamp.
    monkeypatch.setattr(orchestrator_agent, "_engine_crm", lambda: hub)

    try:
        report = orchestrator_agent.dispatch_cycle(dry_run=False)
    finally:
        server.shutdown()

    # The orchestrator believes the dispatch succeeded.
    assert report["failures"] == []
    assert report["dispatched"][0]["contact_id"] == "42"
    assert report["dispatched"][0]["agent"] == "email"

    # The real send_outreach_email code path actually ran end to end, over
    # a real HTTP round-trip, not a mocked function call.
    assert len(FakeMailgun.sent) == 1
    sent = FakeMailgun.sent[0]
    assert sent["to"] == "jane@acme.example"
    assert "Jane" in sent["html"]
    assert sent["variables"] == {"hubspot_contact_id": "42"}

    # The stage promotion (profiled -> email_outreach) really happened on
    # the shared CRM, driven by the Email Agent's own code, not asserted
    # against the orchestrator's report alone.
    assert hub.stage_calls[0][1] is LeadStage.EMAIL_OUTREACH
    assert lead.stage is LeadStage.EMAIL_OUTREACH

    # The orchestrator's dispatch cooldown was stamped on a real dispatch.
    assert len(hub.mark_dispatched_calls) == 1
    assert hub.mark_dispatched_calls[0][0] == "42"


def test_orchestrator_skips_email_agent_when_lead_in_cooldown(monkeypatch):
    """The flip side: prove dispatch_cycle does NOT hit the Email Agent a
    second time for a lead it already contacted inside the cooldown window
    — the fix from earlier in this session, checked here at the same
    real-HTTP integration level rather than as a unit test."""
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    recently = (now - timedelta(hours=1)).isoformat()  # inside a 72h cooldown
    lead = LeadProfile(email="warm@acme.example", probability=15,
                       stage=LeadStage.EMAIL_OUTREACH, hubspot_contact_id="99",
                       last_dispatched_at=recently)
    hub = FakeHubSpot(lead)

    hits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            hits.append(1)
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"jsonrpc":"2.0","result":{}}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("LQABR_EMAIL_AGENT_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(orchestrator_agent, "_engine_crm", lambda: hub)

    try:
        report = orchestrator_agent.dispatch_cycle(dry_run=False, now=now)
    finally:
        server.shutdown()

    assert hits == []  # the Email Agent's endpoint was never even called
    assert report["dispatched"] == []
    assert report["skipped"][0]["contact_id"] == "99"
