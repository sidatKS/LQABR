"""The MCP's HubSpot REST surface (steps 5 and 9)."""

from dataclasses import replace

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

    lead = LeadProfile(full_name="Jane Smith", external_employee_id="E00002",
                       email="jane@acme.example", company="Acme",
                       job_title="VP Engineering", industry="Software",
                       external_company_id="C-1", probability=10, object_id="42")

    def __init__(self, access_token=None, session=None):
        self.access_token = access_token

    def get_lead(self, object_id):
        # A COPY: the real client builds a fresh profile per call, and the
        # company walk fills the profile in place — handing out the shared
        # class attribute would leak one test's enrichment into the next.
        return replace(self.lead)

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
    assert profile.first_name == "Jane" and profile.last_name == "Smith"
    assert obs.processes[0]["event"] == "schema_validated"
    assert obs.processes[0]["step"] == 5


# --------------------------------------------- step 5: the company walk
class NoIndustryClient(StubHubSpotClient):
    """A contact as HubSpot actually returns one: `industry`, `company_id`
    and revenue are COMPANY columns, so a contact GET never carries them."""

    lead = replace(StubHubSpotClient.lead, industry=None,
                   external_company_id=None, company_size_revenue=None)


def crm_with(client, responses):
    session = FakeSession(responses)
    obs = RecordingObs()
    return HubSpotCRM(tokens=StubTokens(), obs=obs, session=session,
                      client_factory=client, backoff_seconds=0), session, obs


def events(obs):
    return [p["event"] for p in obs.processes]


def test_industry_is_read_off_the_associated_company_not_the_contact():
    crm_, session, obs = crm_with(NoIndustryClient, [
        FakeResponse(200, {"results": [{"toObjectId": 9001}]}),
        FakeResponse(200, {"properties": {"industry": "Financial Services",
                                          "company_id": "C-77",
                                          "annualrevenue": "12000000"}}),
    ])
    profile = crm_.get_lead_profile("42")

    assert profile.industry == "Financial Services"
    assert profile.company_id == "C-77"
    assert profile.company_size_revenue == "12000000"
    # the whole point: this is what step 6 selects the skill on
    assert "industry" not in profile.missing_pointers

    assert "company_enriched" in events(obs)
    assert session.calls[0]["url"].endswith(
        "/crm/v4/objects/contacts/42/associations/companies")
    assert session.calls[1]["url"].endswith("/crm/v3/objects/companies/9001")


def test_a_profile_that_already_carries_an_industry_costs_no_extra_hops():
    crm_, session, _ = crm_with(StubHubSpotClient, [])
    profile = crm_.get_lead_profile("42")
    assert profile.industry == "Software"
    assert session.calls == []


def test_a_contact_with_no_associated_company_still_validates():
    crm_, _, obs = crm_with(NoIndustryClient, [FakeResponse(200, {"results": []})])
    profile = crm_.get_lead_profile("42")

    # best-effort: the lead is still worked, and the gap is named, not fatal
    assert profile.industry == ""
    assert "industry" in profile.missing_pointers
    assert "company_association_absent" in events(obs)
    assert "schema_validated" in events(obs)


def test_a_failing_company_read_never_guesses_the_industry():
    crm_, _, obs = crm_with(NoIndustryClient, [
        FakeResponse(200, {"results": [{"toObjectId": 9001}]}),
        FakeResponse(404, text="company not found"),
    ])
    profile = crm_.get_lead_profile("42")

    assert profile.industry == ""
    assert "company_read_failed" in events(obs)


def test_several_associated_companies_are_flagged_rather_than_picked_silently():
    crm_, _, obs = crm_with(NoIndustryClient, [
        FakeResponse(200, {"results": [{"toObjectId": 9001}, {"toObjectId": 9002}]}),
        FakeResponse(200, {"properties": {"industry": "Construction"}}),
    ])
    profile = crm_.get_lead_profile("42")

    assert profile.industry == "Construction"
    assert "company_association_ambiguous" in events(obs)


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
                                    "firstname": "Jane", "lastname": "Smith",
                                    "email_id": "jane@acme.example", "probability": "10"}}]})])
    client = HubSpotCRM(tokens=StubTokens(), obs=obs, session=session,
                        client_factory=StubHubSpotClient, backoff_seconds=0)

    leads = client.leads_for_trigger("trg-1", limit=25)
    assert [lead.object_id for lead in leads] == ["42"]
    assert leads[0].full_name == "Jane Smith"
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
    client = crm([FakeResponse(200, {"id": "42", "properties": {
        "lqabr_email_status": "OPENED", "probability": "17"}})], obs=obs)
    client.patch_object("42", {"lqabr_email_status": "opened", "probability": 17},
                        verify_after_seconds=0)

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


# def test_mark_campaign_complete_writes_the_single_handoff_column():
#     obs = RecordingObs()
#     crm([FakeResponse(200, {"id": "42", "properties": {
#         campaign_complete_property(): "true"}})], obs=obs
#         ).mark_campaign_complete("42", verify_after_seconds=0)
#     written = [p for p in obs.processes if p["event"] == "writeback_applied"][0]["written"]
#     assert written == {campaign_complete_property(): "true"}


def test_writeback_verification_catches_a_property_the_patch_response_never_echoed():
    """HubSpot can return HTTP 200 while silently refusing one property —
    e.g. a workflow-controlled or write-restricted field. The PATCH
    response itself won't carry it, and that must be caught without a
    second hop."""
    obs = RecordingObs()
    session = FakeSession([FakeResponse(200, {"id": "42", "properties": {
        "probability": "17"}})])   # lqabr_email_status silently absent
    client = HubSpotCRM(tokens=StubTokens(), obs=obs, session=session,
                        client_factory=StubHubSpotClient, backoff_seconds=0)

    client.patch_object("42", {"lqabr_email_status": "OPENED", "probability": 17},
                        verify_after_seconds=0)

    failure = [p for p in obs.processes if p["event"] == "writeback_verification_failed"][0]
    assert "lqabr_email_status" in failure["mismatched"]
    assert len(session.calls) == 1   # caught from the PATCH response alone — no reread needed


def test_a_workflow_reverting_the_property_after_success_is_caught_on_reread(monkeypatch):
    """The PATCH response can echo success correctly and a HubSpot workflow
    can still silently revert the property moments later — invisible to the
    PATCH response no matter how carefully it's checked. Only a later read
    catches it."""
    import mcp.hubspot.crm as crm_module
    monkeypatch.setattr(crm_module.time, "sleep", lambda seconds: None)

    obs = RecordingObs()
    session = FakeSession([
        FakeResponse(200, {"id": "42", "properties": {"lqabr_email_status": "OPENED"}}),
        FakeResponse(200, {"id": "42", "properties": {"lqabr_email_status": "SENT"}}),
    ])
    client = HubSpotCRM(tokens=StubTokens(), obs=obs, session=session,
                        client_factory=StubHubSpotClient, backoff_seconds=0)

    client.patch_object("42", {"lqabr_email_status": "OPENED"}, verify_after_seconds=2.0)

    reverted = [p for p in obs.processes if p["event"] == "writeback_reverted_after_success"][0]
    assert reverted["reverted"]["lqabr_email_status"] == {
        "we_set": "OPENED", "hubspot_now_holds": "SENT"}
    assert session.calls[1]["method"] == "GET"


# --------------------------------------------------------------- transport
def test_a_401_invalidates_the_bearer_and_retries():
    tokens = StubTokens()
    client = HubSpotCRM(tokens=tokens, obs=RecordingObs(),
                        session=FakeSession([FakeResponse(401, text="expired"),
                                             FakeResponse(200, {"id": "42",
                                                                "properties": {"probability": "12"}})]),
                        client_factory=StubHubSpotClient, backoff_seconds=0)
    client.patch_object("42", {"probability": 12}, verify_after_seconds=0)
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
    session = FakeSession([FakeResponse(200, {"id": "42", "properties": {"probability": "12"}})])
    HubSpotCRM(tokens=StubTokens("tok-abc"), obs=RecordingObs(), session=session,
               client_factory=StubHubSpotClient, backoff_seconds=0
               ).patch_object("42", {"probability": 12}, verify_after_seconds=0)
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
        lead = LeadProfile(external_employee_id="E00002", object_id="42")

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
