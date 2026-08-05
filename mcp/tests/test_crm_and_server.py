"""The MCP's HubSpot REST surface (steps 5 and 9)."""

import pytest

from mcp_fakes import FakeResponse, FakeSession, RecordingObs
from lqabr_core.crm import CRMError
from lqabr_core.types import LeadProfile
from mcp.hubspot.crm import HubSpotCRM
from mcp.hubspot.schema import SchemaValidationError, campaign_complete_property
from mcp.hubspot.server import TOOLS, MCPSession


class StubTokens:
    def __init__(self, token="tok-1"):
        self.token, self.invalidations = token, 0

    def get(self):
        return self.token

    def invalidate(self):
        self.invalidations += 1


class StubHubSpotClient:
    """Stands in for lqabr_core.crm.HubSpotClient — the read path the MCP
    deliberately reuses rather than forking."""

    lead = LeadProfile(external_employee_id="E00002", email="jane@acme.example", company="Acme",
                       job_title="VP Engineering", industry="Software",
                       external_company_id="C-1", probability=10, hubspot_contact_id="42")

    def __init__(self, access_token=None, session=None):
        self.access_token = access_token

    def get_lead(self, object_id):
        return self.lead

    def find_lead_by_email(self, email):
        return self.lead if email == self.lead.email else None

    def leads_in_stage(self, stage, min_probability=0, limit=100):
        return [self.lead]


def crm(responses=None, obs=None, tokens=None):
    return HubSpotCRM(tokens=tokens or StubTokens(), obs=obs or RecordingObs(),
                      session=FakeSession(responses or []),
                      client_factory=StubHubSpotClient, backoff_seconds=0)


# ------------------------------------------------------------------ step 5
def test_get_lead_profile_validates_and_logs_the_validation():
    obs = RecordingObs()
    profile = crm(obs=obs).get_lead_profile("42")
    assert profile.object_id == "42" and profile.employee_id == "E00002"
    assert obs.processes[0]["event"] == "schema_validated"
    assert obs.processes[0]["step"] == 5


def test_the_read_path_is_bound_to_the_current_run_bearer():
    tokens = StubTokens("bearer-of-this-run")
    client_holder = {}

    def factory(access_token=None, session=None):
        client_holder["token"] = access_token
        return StubHubSpotClient()

    HubSpotCRM(tokens=tokens, obs=RecordingObs(), session=FakeSession(),
               client_factory=factory).get_lead_profile("42")
    assert client_holder["token"] == "bearer-of-this-run"


def test_leads_for_trigger_searches_on_the_trigger_property():
    obs = RecordingObs()
    session = FakeSession([FakeResponse(200, {"results": [
        {"id": "42", "properties": {"employee_id": "E00002",
                                    "email_id": "jane@acme.example", "probability": "10"}}]})])
    client = HubSpotCRM(tokens=StubTokens(), obs=obs, session=session,
                        client_factory=StubHubSpotClient, backoff_seconds=0)

    leads = client.leads_for_trigger("trg-1", limit=25)
    assert [lead.hubspot_contact_id for lead in leads] == ["42"]
    body = session.calls[0]["json"]
    assert body["filterGroups"][0]["filters"][0]["value"] == "trg-1"
    assert obs.processes[-1]["event"] == "trigger_batch_loaded"


def test_a_missing_object_id_property_fails_rather_than_emailing_other_leads(monkeypatch):
    """A wrong property name must NOT quietly become "work the not-yet-emailed
    queue instead" — that would send real email to a different set of leads
    than the campaign asked for, and still look like success."""
    monkeypatch.delenv("LQABR_EMAIL_ALLOW_QUEUE_FALLBACK", raising=False)
    obs = RecordingObs()
    session = FakeSession([FakeResponse(400, text="Invalid property name: object_id")])
    client = HubSpotCRM(tokens=StubTokens(), obs=obs, session=session,
                        client_factory=StubHubSpotClient, backoff_seconds=0)

    with pytest.raises(CRMError) as exc:
        client.leads_for_trigger("trg-1")
    assert "LQABR_HUBSPOT_OBJECT_ID_PROPERTY" in str(exc.value)
    assert any(p["event"] == "object_id_property_unavailable" for p in obs.processes)


def test_the_queue_fallback_is_available_but_only_on_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("LQABR_EMAIL_ALLOW_QUEUE_FALLBACK", "1")
    obs = RecordingObs()
    session = FakeSession([FakeResponse(400, text="Invalid property name: object_id")])
    client = HubSpotCRM(tokens=StubTokens(), obs=obs, session=session,
                        client_factory=StubHubSpotClient, backoff_seconds=0)

    leads = client.leads_for_trigger("trg-1")
    assert len(leads) == 1
    assert any(p["event"] == "object_id_fallback_used" for p in obs.processes)


# ------------------------------------------------------------------ step 9
def test_patch_validates_before_the_hop_and_audits_it():
    obs = RecordingObs()
    client = crm([FakeResponse(200, {"id": "42"})], obs=obs)
    client.patch_object("42", {"lqabr_email_status": "opened", "probability": 17})

    audit = [a for a in obs.audits if a["step"] == 9][0]
    assert audit["method"] == "PATCH" and audit["status_code"] == 200
    assert audit["endpoint"] == "/crm/v3/objects/contacts/42"
    assert [p["event"] for p in obs.processes] == ["writeback_validated", "writeback_applied"]


def test_an_invalid_property_never_reaches_hubspot():
    session = FakeSession([FakeResponse(200)])
    client = HubSpotCRM(tokens=StubTokens(), obs=RecordingObs(), session=session,
                        client_factory=StubHubSpotClient, backoff_seconds=0)
    with pytest.raises(SchemaValidationError):
        client.patch_object("42", {"lqabr_email_status": "CLICKED"})
    assert session.calls == []


def test_mark_campaign_complete_writes_the_single_handoff_column():
    obs = RecordingObs()
    crm([FakeResponse(200, {"id": "42"})], obs=obs).mark_campaign_complete("42")
    written = [p for p in obs.processes if p["event"] == "writeback_applied"][0]["written"]
    assert written == {campaign_complete_property(): "true"}


# --------------------------------------------------------------- transport
def test_a_401_invalidates_the_bearer_and_retries():
    tokens = StubTokens()
    client = HubSpotCRM(tokens=tokens, obs=RecordingObs(),
                        session=FakeSession([FakeResponse(401, text="expired"),
                                             FakeResponse(200, {"id": "42"})]),
                        client_factory=StubHubSpotClient, backoff_seconds=0)
    client.patch_object("42", {"probability": 12})
    assert tokens.invalidations == 1


def test_a_5xx_is_retried_then_raises_crm_error():
    client = HubSpotCRM(tokens=StubTokens(), obs=RecordingObs(),
                        session=FakeSession([FakeResponse(503, text="busy")] * 3),
                        client_factory=StubHubSpotClient, backoff_seconds=0)
    with pytest.raises(CRMError):
        client.patch_object("42", {"probability": 12})


def test_a_4xx_is_not_retried():
    session = FakeSession([FakeResponse(403, text="forbidden")])
    client = HubSpotCRM(tokens=StubTokens(), obs=RecordingObs(), session=session,
                        client_factory=StubHubSpotClient, backoff_seconds=0)
    with pytest.raises(CRMError):
        client.patch_object("42", {"probability": 12})
    assert len(session.calls) == 1


def test_every_hop_carries_the_bearer_header():
    session = FakeSession([FakeResponse(200, {"id": "42"})])
    HubSpotCRM(tokens=StubTokens("tok-abc"), obs=RecordingObs(), session=session,
               client_factory=StubHubSpotClient, backoff_seconds=0
               ).patch_object("42", {"probability": 12})
    assert session.calls[0]["headers"]["Authorization"] == "Bearer tok-abc"


# ------------------------------------------------------------------ server
def test_the_tool_surface_is_the_three_named_tools():
    assert set(TOOLS) == {"get_lead_profile_details", "list_trigger_leads", "post_patch_crm"}


def test_get_lead_profile_details_returns_a_tool_shaped_dict():
    session = MCPSession(crm=crm())
    result = session.get_lead_profile_details("42")
    assert result["object_id"] == "42" and result["email_id"] == "jane@acme.example"


def test_an_unworkable_lead_comes_back_as_an_error_not_an_exception():
    class NoEmailClient(StubHubSpotClient):
        lead = LeadProfile(external_employee_id="E00002", hubspot_contact_id="42")

    inner = HubSpotCRM(tokens=StubTokens(), obs=RecordingObs(), session=FakeSession(),
                       client_factory=NoEmailClient, backoff_seconds=0)
    result = MCPSession(crm=inner).get_lead_profile_details("42")
    assert result["error"].startswith("bad-data")


def test_post_patch_crm_reports_a_validation_failure_rather_than_a_false_success():
    result = MCPSession(crm=crm()).post_patch_crm("42", {"lqabr_email_status": "NOPE"})
    assert "error" in result and "status" not in result


def test_an_os_level_failure_is_retried_not_crashed():
    """`requests.RequestException` is a subclass of OSError, not a superset.
    A plain OSError from below the requests layer — an unreadable TLS CA
    bundle, socket exhaustion — must be treated as a transport failure and
    retried, not escape as an unhandled 500."""
    class ExplodingSession:
        def __init__(self):
            self.calls = 0

        def request(self, *a, **k):
            self.calls += 1
            raise OSError("Could not find a suitable TLS CA certificate bundle")

    session = ExplodingSession()
    client = HubSpotCRM(tokens=StubTokens(), obs=RecordingObs(), session=session,
                        client_factory=StubHubSpotClient, backoff_seconds=0)
    with pytest.raises(CRMError):
        client.patch_object("42", {"probability": 12})
    assert session.calls == 3          # retried, then a typed error
