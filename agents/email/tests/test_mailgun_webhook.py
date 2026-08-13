import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from lqabr_core.types import EventType, LeadProfile, LeadStage

SIGNING_KEY = "test-signing-key"


@pytest.fixture(autouse=True)
def signing_key_env(monkeypatch):
    from lqabr_core.secrets import get_secret
    get_secret.cache_clear()
    monkeypatch.setenv("LQABR_MAILGUN_WEBHOOK_SIGNING_KEY", SIGNING_KEY)
    yield
    get_secret.cache_clear()


class FakeCRM:
    events = []

    def record_event(self, event):
        FakeCRM.events.append(event)
        return LeadProfile(probability=17, stage=LeadStage.EMAIL_OUTREACH,
                           hubspot_contact_id=event.hubspot_contact_id)


def signed(timestamp="100", token="tok"):
    signature = hmac.new(SIGNING_KEY.encode(), f"{timestamp}{token}".encode(),
                         hashlib.sha256).hexdigest()
    return {"timestamp": timestamp, "token": token, "signature": signature}


def payload(event="opened", variables=None):
    if variables is None:
        variables = {"hubspot_contact_id": "42"}
    return {"signature": signed(),
            "event-data": {"event": event, "timestamp": 1234.5,
                           "user-variables": variables}}


@pytest.fixture
def client(email_webhook_app, monkeypatch):
    FakeCRM.events = []
    monkeypatch.setattr(email_webhook_app, "_crm", lambda: FakeCRM())
    return TestClient(email_webhook_app.app)


def test_opened_event_recorded(client):
    resp = client.post("/webhooks/mailgun", json=payload("opened"))
    assert resp.status_code == 200
    assert resp.json()["status"] == "recorded"
    assert FakeCRM.events[0].event_type is EventType.EMAIL_OPENED


def test_bad_signature_rejected(client):
    body = payload()
    body["signature"]["signature"] = "forged"
    assert client.post("/webhooks/mailgun", json=body).status_code == 401


def test_unscored_event_ignored(client):
    resp = client.post("/webhooks/mailgun", json=payload("accepted"))
    assert resp.json()["status"] == "ignored"
    assert FakeCRM.events == []


def test_missing_contact_id_is_422_never_silently_dropped(client):
    resp = client.post("/webhooks/mailgun", json=payload("clicked", variables={}))
    assert resp.status_code == 422
