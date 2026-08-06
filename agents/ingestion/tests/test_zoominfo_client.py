import json

import pytest

from zoominfo_client import ZoomInfoClient, ZoomInfoError


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


ZI_CONTACT = {
    "id": 42, "firstName": "Jane", "lastName": "Smith", "jobTitle": "VP Procurement",
    "email": "jane@acme.example", "directPhone": "+15550001111",
    "city": "Chicago", "state": "IL", "country": "USA",
    "linkedinUrl": "https://linkedin.com/in/janesmith",
    "company": {"id": 7, "name": "Acme Corp", "primaryIndustry": "Manufacturing",
                "employeeCount": 250, "revenue": 17300000},
}


def make_client(responses):
    session = FakeSession(responses)
    return ZoomInfoClient(username="u", password="p", session=session), session


def test_pull_records_authenticates_then_searches_with_batch_size():
    client, session = make_client([
        FakeResponse(200, {"jwt": "token"}),
        FakeResponse(200, {"data": [ZI_CONTACT]}),
    ])
    records = client.pull_records(batch_size=20)
    assert session.calls[0][0].endswith("/authenticate")
    assert session.calls[1][1]["json"]["rs"] == 20
    assert len(records) == 1


def test_normalize_maps_the_nine_pointers():
    record = ZoomInfoClient.normalize(ZI_CONTACT)
    assert record["full_name"] == "Jane Smith"
    assert record["company"] == "Acme Corp"
    assert record["phone"] == "+15550001111"
    assert record["company_size"] == "250"
    assert record["annual_revenue"] == "17300000"
    assert record["location"] == "Chicago, IL, USA"
    assert record["linkedin_url"].endswith("janesmith")
    assert record["source"] == "zoominfo"
    assert record["external_employee_id"] == "42"


def test_auth_failure_raises():
    client, _ = make_client([FakeResponse(401, {"error": "bad creds"})])
    with pytest.raises(ZoomInfoError, match="auth failed"):
        client.pull_records()
