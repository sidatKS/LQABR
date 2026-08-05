import json

import pytest

from lqabr_core.crm.base import CRMError
from lqabr_core.crm.hubspot import HubSpotClient
from lqabr_core.probability import TEXT_VOICE_THRESHOLD
from lqabr_core.types import EngagementEvent, EventType, LeadProfile, LeadStage


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


def make_client(responses):
    session = FakeSession(responses)
    return HubSpotClient(access_token="t", session=session, backoff_seconds=0), session


def contact_body(contact_id="123", **props):
    return {"id": contact_id, "properties": props}


def test_upsert_creates_when_email_not_found():
    client, session = make_client([
        FakeResponse(200, {"results": []}),                 # search
        FakeResponse(200, {"id": "999"}),                   # create
    ])
    lead = client.upsert_lead(LeadProfile(full_name="Jane Smith", email="j@x.com"))
    assert lead.hubspot_contact_id == "999"
    method, url, kwargs = session.calls[1]
    assert method == "POST" and url.endswith("/crm/v3/objects/contacts")
    assert kwargs["json"]["properties"]["firstname"] == "Jane"
    assert kwargs["json"]["properties"]["lastname"] == "Smith"


def test_upsert_patches_when_contact_exists():
    client, session = make_client([
        FakeResponse(200, {"results": [contact_body("55", email_id="j@x.com")]}),  # search
        FakeResponse(200, {"results": []}),                                      # associations (no company)
        FakeResponse(200, {}),                                                   # patch
    ])
    lead = client.upsert_lead(LeadProfile(email="j@x.com"))
    assert lead.hubspot_contact_id == "55"
    assert session.calls[2][0] == "PATCH"
    assert session.calls[2][1].endswith("/contacts/55")


def test_record_event_updates_email_status_and_promotes():
    start = TEXT_VOICE_THRESHOLD - 2  # +5 open crosses the threshold
    client, session = make_client([
        FakeResponse(200, contact_body("77", probability=str(start),
                                       lqabr_email_status="SENT", email_id="j@x.com")),
        FakeResponse(200, {"results": []}),  # associations (no company)
        FakeResponse(200, {}),  # patch
    ])
    lead = client.record_event(EngagementEvent(EventType.EMAIL_OPENED, "77"))
    assert lead.probability == start + 5
    # promotion is in-memory only — no HubSpot property represents
    # cross-agent pipeline stage in this schema.
    assert lead.stage is LeadStage.TEXT_VOICE_OUTREACH
    props = session.calls[2][2]["json"]["properties"]
    assert props["lqabr_email_status"] == "OPENED"
    assert props["probability"] == str(start + 5)
    assert "email_opened" not in props  # no counter property exists


def test_retries_on_5xx_then_raises_crm_error():
    client, session = make_client([FakeResponse(500), FakeResponse(502), FakeResponse(503)])
    with pytest.raises(CRMError, match="after 3 retries"):
        client.get_lead("1")
    assert len(session.calls) == 3


def test_4xx_raises_immediately_without_retry():
    client, session = make_client([FakeResponse(403, {"message": "nope"})])
    with pytest.raises(CRMError, match="HTTP 403"):
        client.get_lead("1")
    assert len(session.calls) == 1
