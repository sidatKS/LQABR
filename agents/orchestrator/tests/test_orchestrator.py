import json

import pytest

import orchestrator_agent
from a2a_dispatch import A2ADispatcher, A2AError, agent_url
from lqabr_core.types import LeadProfile, LeadStage


class FakeCRM:
    def __init__(self, stage_leads):
        self.stage_leads = stage_leads

    def leads_in_stage(self, stage, min_probability=0, limit=100):
        return self.stage_leads.get(stage, [])[:limit]


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {"jsonrpc": "2.0", "result": {"ok": True}}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse()


def test_agent_url_requires_configuration(monkeypatch):
    monkeypatch.delenv("LQABR_EMAIL_AGENT_URL", raising=False)
    with pytest.raises(A2AError, match="not configured"):
        agent_url("email")
    with pytest.raises(A2AError, match="unknown agent"):
        agent_url("mystery")


def test_dispatcher_sends_a2a_message_send(monkeypatch):
    monkeypatch.setenv("LQABR_EMAIL_AGENT_URL", "https://email.example")
    session = FakeSession()
    result = A2ADispatcher(session=session).send_message("email", "do the thing")
    assert result == {"ok": True}
    url, kwargs = session.calls[0]
    assert url == "https://email.example"
    body = kwargs["json"]
    assert body["method"] == "message/send"
    assert body["params"]["message"]["parts"][0]["text"] == "do the thing"


def test_dry_run_plans_routing_per_stage(monkeypatch):
    leads = {
        LeadStage.PROFILED: [LeadProfile(email="new@x.com", probability=10,
                                         stage=LeadStage.PROFILED, hubspot_contact_id="1")],
        LeadStage.TEXT_VOICE_OUTREACH: [LeadProfile(email="warm@x.com", probability=40,
                                                    stage=LeadStage.TEXT_VOICE_OUTREACH,
                                                    hubspot_contact_id="2")],
        LeadStage.SCHEDULING: [LeadProfile(email="hot@x.com", probability=70,
                                           stage=LeadStage.SCHEDULING, hubspot_contact_id="3")],
    }
    monkeypatch.setattr(orchestrator_agent, "_engine_crm", lambda: FakeCRM(leads))
    report = orchestrator_agent.dispatch_cycle(dry_run=True)
    routed = {item["contact_id"]: item["agent"] for item in report["dispatched"]}
    assert routed == {"1": "email", "2": "text_voice", "3": "scheduling"}
    assert report["failures"] == []


def test_lead_without_email_is_flagged_not_dropped(monkeypatch):
    leads = {LeadStage.PROFILED: [LeadProfile(phone="+1555", probability=10,
                                              stage=LeadStage.PROFILED, hubspot_contact_id="9")]}
    monkeypatch.setattr(orchestrator_agent, "_engine_crm", lambda: FakeCRM(leads))
    report = orchestrator_agent.dispatch_cycle(dry_run=True)
    assert report["dispatched"] == []
    assert report["failures"][0]["reason"].startswith("bad-data")
