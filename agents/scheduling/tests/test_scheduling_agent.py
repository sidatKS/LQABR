import scheduling_agent
from lqabr_core.types import LeadProfile, LeadStage


class FakeCRM:
    def __init__(self, lead):
        self.lead = lead

    def find_lead_by_email(self, email):
        return self.lead

    def leads_in_stage(self, stage, min_probability=0, limit=100):
        return [self.lead] if self.lead else []


class FakeZoom:
    def __init__(self, *args, **kwargs):
        pass

    def booking_link(self, schedule_id=None):
        return "https://scheduler.zoom.us/team/intro"


class FakeMailgun:
    def __init__(self, *args, **kwargs):
        pass

    def send_email(self, **kwargs):
        FakeMailgun.last_send = kwargs
        return {"id": "<m1@mg>"}


def test_invite_offers_all_four_timezones(monkeypatch):
    lead = LeadProfile(full_name="Jane Smith", email="jane@acme.example",
                       company="Acme", stage=LeadStage.SCHEDULING, hubspot_contact_id="42")
    monkeypatch.setattr(scheduling_agent, "HubSpotClient", lambda: FakeCRM(lead))
    monkeypatch.setattr(scheduling_agent, "ZoomSchedulerClient", FakeZoom)
    monkeypatch.setattr(scheduling_agent, "MailgunClient", FakeMailgun)

    result = scheduling_agent.send_schedule_invite("jane@acme.example")
    assert result["status"] == "sent"
    assert result["timezones_offered"] == ["EST", "CST", "PST", "IST"]
    html = FakeMailgun.last_send["html"]
    for zone in ("EST", "CST", "PST", "IST"):
        assert zone in html
    assert "https://scheduler.zoom.us/team/intro" in html
    assert FakeMailgun.last_send["variables"] == {"hubspot_contact_id": "42"}


def test_unknown_lead_is_error(monkeypatch):
    monkeypatch.setattr(scheduling_agent, "HubSpotClient", lambda: FakeCRM(None))
    result = scheduling_agent.send_schedule_invite("ghost@x.com")
    assert "no HubSpot contact" in result["error"]
