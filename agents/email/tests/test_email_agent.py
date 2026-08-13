import email_agent
from lqabr_core.types import LeadProfile, LeadStage


class FakeCRM:
    def __init__(self, lead=None):
        self.lead = lead
        self.stage_calls = []

    def find_lead_by_email(self, email):
        return self.lead

    def set_stage(self, contact_id, stage, reason=None):
        self.stage_calls.append((contact_id, stage, reason))

    def leads_in_stage(self, stage, min_probability=0, limit=100):
        return [self.lead] if self.lead else []


class FakeMailgun:
    def __init__(self, *args, **kwargs):
        pass

    def send_email(self, **kwargs):
        FakeMailgun.last_send = kwargs
        return {"id": "<msg-1@mg>"}


def test_send_personalizes_and_tags_contact_id(monkeypatch):
    lead = LeadProfile(full_name="Jane Smith", email="jane@acme.example",
                       company="Acme", job_title="VP", industry="Mfg",
                       stage=LeadStage.PROFILED, hubspot_contact_id="42")
    crm = FakeCRM(lead)
    monkeypatch.setattr(email_agent, "HubSpotClient", lambda: crm)
    monkeypatch.setattr(email_agent, "MailgunClient", FakeMailgun)

    result = email_agent.send_outreach_email("jane@acme.example", cta_url="https://x.example/go")
    assert result["status"] == "sent"
    sent = FakeMailgun.last_send
    assert sent["to"] == "jane@acme.example"
    assert "Jane" in sent["html"] and "Acme" in sent["subject"]
    assert sent["variables"] == {"hubspot_contact_id": "42"}
    # first send moves the lead into email_outreach
    assert crm.stage_calls[0][1] is LeadStage.EMAIL_OUTREACH


def test_unknown_lead_is_an_error_not_a_send(monkeypatch):
    monkeypatch.setattr(email_agent, "HubSpotClient", lambda: FakeCRM(None))
    result = email_agent.send_outreach_email("ghost@x.com")
    assert "no HubSpot contact" in result["error"]


def test_queue_lists_stage_leads(monkeypatch):
    lead = LeadProfile(email="a@b.c", stage=LeadStage.EMAIL_OUTREACH, hubspot_contact_id="1")
    monkeypatch.setattr(email_agent, "HubSpotClient", lambda: FakeCRM(lead))
    queue = email_agent.list_email_queue()
    assert queue["count"] == 1
