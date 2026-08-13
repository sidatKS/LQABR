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
    assert lead.contact_id == "999"
    method, url, kwargs = session.calls[1]
    assert method == "POST" and url.endswith("/crm/v3/objects/contacts")
    assert kwargs["json"]["properties"]["firstname"] == "Jane"
    assert kwargs["json"]["properties"]["lastname"] == "Smith"


def test_upsert_patches_when_contact_exists():
    client, session = make_client([
        FakeResponse(200, {"results": [contact_body("55", email="j@x.com")]}),  # search
        FakeResponse(200, {}),                                                   # patch
    ])
    lead = client.upsert_lead(LeadProfile(email="j@x.com"))
    assert lead.contact_id == "55"
    assert session.calls[1][0] == "PATCH"
    assert session.calls[1][1].endswith("/contacts/55")


def test_find_lead_by_email_matches_via_email_id_fallback():
    # some contacts hold the address in the custom `email_id` property
    # instead of the standard `email` one — search must catch both.
    client, session = make_client([
        FakeResponse(200, {"results": [contact_body("88", email_id="j@x.com")]}),
    ])
    lead = client.find_lead_by_email("j@x.com")
    assert lead is not None and lead.contact_id == "88"
    assert lead.email == "j@x.com"
    filter_groups = session.calls[0][2]["json"]["filterGroups"]
    assert {"propertyName": "email", "operator": "EQ", "value": "j@x.com"} in filter_groups[0]["filters"]
    assert {"propertyName": "email_id", "operator": "EQ", "value": "j@x.com"} in filter_groups[1]["filters"]


def test_find_lead_by_phone_returns_match():
    client, session = make_client([
        FakeResponse(200, {"results": [contact_body("42", phone="+15550001111", email="j@x.com")]}),
    ])
    lead = client.find_lead_by_phone("+15550001111")
    assert lead is not None and lead.contact_id == "42"
    method, url, kwargs = session.calls[0]
    assert method == "POST" and url.endswith("/crm/v3/objects/contacts/search")
    filt = kwargs["json"]["filterGroups"][0]["filters"][0]
    assert filt == {"propertyName": "phone", "operator": "EQ", "value": "+15550001111"}


def test_find_lead_by_phone_returns_none_when_no_match():
    client, session = make_client([FakeResponse(200, {"results": []})])
    assert client.find_lead_by_phone("+19998887777") is None


def test_record_event_writes_probability_and_derives_stage():
    start = TEXT_VOICE_THRESHOLD - 2  # +5 open crosses the threshold
    client, session = make_client([
        FakeResponse(200, contact_body("77", probability=str(start), email="j@x.com")),
        FakeResponse(200, {}),  # patch
    ])
    lead = client.record_event(EngagementEvent(EventType.EMAIL_OPENED, "77"))
    assert lead.probability == start + 5
    assert lead.stage is LeadStage.TEXT_VOICE_OUTREACH  # derived, not stored
    props = session.calls[1][2]["json"]["properties"]
    assert props == {"probability": str(start + 5)}


def test_record_event_writes_voice_status_for_call_answered():
    client, session = make_client([
        FakeResponse(200, contact_body("77", probability="30", email="j@x.com")),
        FakeResponse(200, {}),  # patch
    ])
    client.record_event(EngagementEvent(EventType.CALL_ANSWERED, "77"))
    props = session.calls[1][2]["json"]["properties"]
    assert props["probability"] == "45"
    assert props["lqabr_voice_status"] == "COMPLETED"
    # call-related events also stamp the Text/Voice Agent's own last-touched marker
    assert props["last_modfied_voice"].isdigit()


def test_record_event_writes_voice_status_for_voicemail_left():
    client, session = make_client([
        FakeResponse(200, contact_body("77", probability="30", email="j@x.com")),
        FakeResponse(200, {}),  # patch
    ])
    client.record_event(EngagementEvent(EventType.VOICEMAIL_LEFT, "77"))
    props = session.calls[1][2]["json"]["properties"]
    assert props["probability"] == "40"  # 30 + 10 (raised from +2, 2026-08-06)
    assert props["lqabr_voice_status"] == "VOICEMAIL_LEFT"
    assert props["last_modfied_voice"].isdigit()


def test_record_event_writes_voice_status_for_call_not_answered_and_leaves_probability_untouched():
    client, session = make_client([
        FakeResponse(200, contact_body("77", probability="30", email="j@x.com")),
        FakeResponse(200, {}),  # patch
    ])
    client.record_event(EngagementEvent(EventType.CALL_NOT_ANSWERED, "77"))
    props = session.calls[1][2]["json"]["properties"]
    # 0 increment: a call that never connected must never move probability.
    assert props["probability"] == "30"
    assert props["lqabr_voice_status"] == "FAILED"
    assert props["last_modfied_voice"].isdigit()


def test_leads_in_stage_filters_by_probability_range():
    client, session = make_client([FakeResponse(200, {"results": []})])
    client.leads_in_stage(LeadStage.TEXT_VOICE_OUTREACH)
    method, url, kwargs = session.calls[0]
    filters = kwargs["json"]["filterGroups"][0]["filters"]
    assert {"propertyName": "probability", "operator": "GTE", "value": "30"} in filters
    assert {"propertyName": "probability", "operator": "LT", "value": "60"} in filters


def test_get_lead_parses_opted_out_and_real_fields():
    client, session = make_client([
        FakeResponse(200, contact_body("42", email="j@x.com", opted_out="true",
                                       probability="45", employee_id="E1",
                                       decision_maker="yes", lqabr_voice_status="COMPLETED")),
    ])
    lead = client.get_lead("42")
    assert lead.opted_out is True
    assert lead.probability == 45
    assert lead.external_employee_id == "E1"
    assert lead.stage is LeadStage.TEXT_VOICE_OUTREACH  # derived from 45
    # decision_maker (not decision_maker_flag — that name doesn't exist in
    # this portal) and lqabr_voice_status (labeled "voice_status" in the UI).
    assert lead.extra["decision_maker"] == "yes"
    assert lead.extra["voice_status"] == "COMPLETED"


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
