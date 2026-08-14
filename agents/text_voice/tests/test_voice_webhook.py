import pytest
from fastapi.testclient import TestClient

from lqabr_core.types import EventType, LeadProfile, LeadStage


class FakeCRM:
    events = []
    lead = LeadProfile(full_name="Jane Smith", email="j@x.com", phone="+1555",
                       company="Acme", industry="Mfg",
                       stage=LeadStage.TEXT_VOICE_OUTREACH, hubspot_contact_id="42")

    def get_lead(self, contact_id):
        return self.lead

    def record_event(self, event):
        FakeCRM.events.append(event)
        return self.lead


class FakeTwilio:
    sms = []

    def __init__(self, *args, **kwargs):
        pass

    def send_sms(self, **kwargs):
        FakeTwilio.sms.append(kwargs)
        return {"sid": "SM1"}


@pytest.fixture
def client(tv_webhook_app, monkeypatch):
    FakeCRM.events = []
    FakeTwilio.sms = []
    monkeypatch.setenv("LQABR_SKIP_TWILIO_SIGNATURE", "1")
    monkeypatch.setattr(tv_webhook_app, "_crm", lambda: FakeCRM())
    monkeypatch.setattr(tv_webhook_app, "TwilioClient", FakeTwilio)
    return TestClient(tv_webhook_app.app)


def test_human_answer_starts_qa_flow(client):
    resp = client.post("/voice/answer?contact_id=42",
                       data={"AnsweredBy": "human", "CallSid": "CA1"})
    assert resp.status_code == 200
    assert "<Gather" in resp.text
    assert FakeCRM.events[0].event_type is EventType.CALL_ANSWERED


def test_machine_answer_leaves_voicemail_and_sends_sms(client):
    resp = client.post("/voice/answer?contact_id=42",
                       data={"AnsweredBy": "machine_end_beep", "CallSid": "CA1"})
    assert "<Hangup/>" in resp.text and "<Gather" not in resp.text
    assert FakeCRM.events[0].event_type is EventType.VOICEMAIL_LEFT
    assert FakeTwilio.sms and FakeTwilio.sms[0]["to"] == "+1555"


def test_completed_qa_records_engaged(client, tv_webhook_app):
    import conversation
    resp = client.post(f"/voice/qa?contact_id=42&step={conversation.FINAL_STEP}",
                       data={"SpeechResult": "yes definitely"})
    assert resp.status_code == 200
    assert any(e.event_type is EventType.CALL_ENGAGED for e in FakeCRM.events)


def test_sms_delivered_status_recorded(client):
    resp = client.post("/sms/status?contact_id=42",
                       data={"MessageStatus": "delivered", "MessageSid": "SM1"})
    assert resp.json()["status"] == "recorded"
    assert FakeCRM.events[0].event_type is EventType.SMS_DELIVERED
