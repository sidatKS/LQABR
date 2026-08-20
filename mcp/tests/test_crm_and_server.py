"""The MCP's HubSpot REST surface (steps 5 and 9)."""

from dataclasses import replace

import pytest

from mcp_fakes import FakeResponse, FakeSession, RecordingObs
from lqabr_core.crm import CRMError
from lqabr_core.types import LeadProfile
from mcp.hubspot.crm import HubSpotCRM
from mcp.hubspot.schema import SchemaValidationError
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
    # `schema_validated` is no longer the FIRST record: the rev-8 lead_context
    # read (step 9) is logged ahead of validation. Assert the record exists and
    # carries its step, rather than that it happens to come first — what reads
    # run before it is not this test's subject.
    validated = [p for p in obs.processes if p["event"] == "schema_validated"]
    assert len(validated) == 1
    assert validated[0]["step"] == 5


# --------------------------------------------- step 5: the company walk
class NoIndustryClient(StubHubSpotClient):
    """A contact as HubSpot actually returns one: `industry`, `company_id`,
    revenue and the company NAME are all COMPANY columns, so a contact GET
    never carries them. Confirmed against contact 533967041217 on
    ldqfingsrv-dev, whose record holds none of the four."""

    lead = replace(StubHubSpotClient.lead, industry=None, company=None,
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


def test_the_company_walk_is_bounded_to_one_association_and_one_company_read():
    """CHANGED IN REV 8. This used to assert ZERO company hops for a profile
    that already carried an industry. That optimisation died with the widened
    read: `about_us`, `hs_industry_group` and `website` exist only on the
    company object, so a contact GET can never satisfy them and the walk has to
    run for every lead with an associated company.

    What still must hold is that it is BOUNDED — one association read and one
    company read, never a per-property fetch or a loop."""
    crm_, session, _ = crm_with(StubHubSpotClient, [
        FakeResponse(200, {"results": [{"toObjectId": 9001}]}),
        FakeResponse(200, {"properties": {"about_us": "Fintech platform."}}),
        FakeResponse(200, {"properties": {"lead_context": "Research narrative."}}),
    ])
    profile = crm_.get_lead_profile("42")
    assert profile.industry == "Software"

    assert len([c for c in session.calls if "/associations/companies" in c["url"]]) == 1
    assert len([c for c in session.calls if "/objects/companies/" in c["url"]]) == 1


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


class CompanyCompleteClient(StubHubSpotClient):
    """A contact whose company-backed values are ALL already populated, so the
    association walk is skipped and the only remaining hop is the lead_context
    read. Used to isolate that read in the tests below — without it the walk's
    two GETs consume the queued responses and the assertions read the wrong
    call."""

    lead = replace(StubHubSpotClient.lead,
                   extra={"company_website": "https://acme.example",
                          "company_about": "Delivery platform vendor.",
                          "industry_group": "Software (Delivery Tooling)"})


# ------------------------------------- rev 8: reading the research context
def test_the_lead_context_is_read_off_the_contact_and_carried_through():
    """FRD step 9. `lqabr_core.crm.HubSpotClient` owns the confirmed 9-pointer
    mapping and knows nothing about this column, and lqabr_core is shared with
    the sibling agents — so the MCP reads it with its own audited hop rather
    than forking that mapping."""
    crm_, session, obs = crm_with(CompanyCompleteClient, [
        FakeResponse(200, {"properties": {"lead_context": "Acme is consolidating."}}),
    ])
    profile = crm_.get_lead_profile("42")

    assert profile.lead_context == "Acme is consolidating."
    assert profile.has_lead_context is True
    assert session.calls[0]["url"].endswith("/crm/v3/objects/contacts/42")
    assert session.calls[0]["params"] == {"properties": "lead_context"}

    read = [p for p in obs.processes if p["event"] == "lead_context_read"]
    assert len(read) == 1
    assert read[0]["step"] == 9 and read[0]["present"] is True
    assert read[0]["word_count"] == 3


def test_a_portal_without_the_property_does_not_fail_the_run():
    """HubSpot answers a read for a property it does not have with a 400. The
    2026-08-05 audit found neither `object_id` nor `email_campaign_complete`
    among all 410 contact properties, so this one is assumed absent too — and a
    missing column must not take a whole campaign down. The lead comes back with
    no context, the email agent's gate flags it, and nothing is emailed off a
    guessed narrative."""
    crm_, _, obs = crm_with(CompanyCompleteClient, [
        FakeResponse(400, {}, text='{"message":"Property \\"lead_context\\" does not exist"}'),
    ])
    profile = crm_.get_lead_profile("42")

    assert profile.lead_context == ""
    assert profile.has_lead_context is False
    unavailable = [p for p in obs.processes if p["event"] == "lead_context_unavailable"]
    assert len(unavailable) == 1
    assert unavailable[0]["property_name"] == "lead_context"
    assert "LQABR_HUBSPOT_LEAD_CONTEXT_PROPERTY" in unavailable[0]["detail"]


def test_an_absent_value_is_reported_as_absent_not_as_a_failure():
    """The property exists but this lead has none yet — research has not
    reached it. That is a different log record from the property not existing,
    because it is a different problem with a different fix."""
    crm_, _, obs = crm_with(CompanyCompleteClient, [
        FakeResponse(200, {"properties": {"lead_context": None}}),
    ])
    profile = crm_.get_lead_profile("42")

    assert profile.lead_context == ""
    read = [p for p in obs.processes if p["event"] == "lead_context_read"]
    assert read[0]["present"] is False
    assert "lead_context_unavailable" not in events(obs)


def test_clearing_the_property_name_skips_the_hop_entirely(monkeypatch):
    """The escape hatch for a portal where the column does not exist: stop
    asking for it at all rather than eating a 400 per lead."""
    monkeypatch.setenv("LQABR_HUBSPOT_LEAD_CONTEXT_PROPERTY", "")
    crm_, session, obs = crm_with(CompanyCompleteClient, [])
    profile = crm_.get_lead_profile("42")

    assert profile.lead_context == ""
    assert session.calls == []
    assert "lead_context_read_disabled" in events(obs)


def test_a_context_already_on_the_profile_costs_no_hop():
    """If the read path ever starts carrying it, do not ask twice."""
    class WithContext(CompanyCompleteClient):
        lead = replace(CompanyCompleteClient.lead,
                       extra={**CompanyCompleteClient.lead.extra,
                              "lead_context": "Already here."})

    crm_, session, _ = crm_with(WithContext, [])
    profile = crm_.get_lead_profile("42")

    assert profile.lead_context == "Already here."
    assert session.calls == []


def test_the_configured_property_name_is_what_gets_asked_for(monkeypatch):
    monkeypatch.setenv("LQABR_HUBSPOT_LEAD_CONTEXT_PROPERTY", "lqabr_research_context")
    crm_, session, _ = crm_with(CompanyCompleteClient, [
        FakeResponse(200, {"properties": {"lqabr_research_context": "Renamed column."}}),
    ])
    profile = crm_.get_lead_profile("42")

    assert profile.lead_context == "Renamed column."
    assert session.calls[0]["params"] == {"properties": "lqabr_research_context"}


# ------------------------- rev 8: the company-owned construction inputs
def test_the_named_construction_inputs_are_read_off_the_company():
    """The FRD names what construction is entitled to — the lead, their
    company, its industry and the post. Four of those are COMPANY-owned and
    were never fetched: the name, the website, the About-Us text and the
    industry group. Confirmed live on ldqfingsrv-dev 2026-08-18, where M1
    Finance holds all four."""
    crm_, session, obs = crm_with(NoIndustryClient, [
        FakeResponse(200, {"results": [{"toObjectId": 9001}]}),
        FakeResponse(200, {"properties": {
            "name": "M1 Finance",
            "website": "https://m1.com",
            "domain": "m1.com",
            "about_us": "Fintech platform offering commission-free automated investing.",
            "industry": "FINANCIAL_SERVICES",
            "hs_industry_group": "Investment / Wealth Management (Automated Investing)",
            "company_id": "C0021",
            "annualrevenue": "4.7",
        }}),
        FakeResponse(200, {"properties": {"lead_context": "Research narrative."}}),
    ])
    profile = crm_.get_lead_profile("42")

    assert profile.company == "M1 Finance"
    assert profile.company_website == "https://m1.com"
    assert profile.company_about.startswith("Fintech platform")
    assert profile.industry_group == "Investment / Wealth Management (Automated Investing)"
    assert profile.industry == "FINANCIAL_SERVICES"
    assert profile.company_id == "C0021"

    # and every one of them reaches construction
    context = profile.as_context()
    for field in ("company", "company_website", "company_about", "industry_group"):
        assert context[field], f"{field} never reached the model"
    # ...while the internal reference does not
    assert "company_id" not in context

    assert "company_enriched" in events(obs)
    asked = session.calls[1]["params"]["properties"]
    for column in ("name", "website", "about_us", "hs_industry_group"):
        assert column in asked, f"the company GET never asked for {column}"


def test_a_company_with_no_website_falls_back_to_its_domain():
    crm_, _, _ = crm_with(NoIndustryClient, [
        FakeResponse(200, {"results": [{"toObjectId": 9001}]}),
        FakeResponse(200, {"properties": {"name": "M1 Finance", "domain": "m1.com",
                                          "industry": "FINANCIAL_SERVICES"}}),
        FakeResponse(200, {"properties": {}}),
    ])
    assert crm_.get_lead_profile("42").company_website == "m1.com"


def test_a_contact_carrying_an_industry_still_gets_the_other_company_fields():
    """Rev 7 short-circuited the company walk on `industry` alone. Now that the
    name, website, about-us and industry group also come from there, that
    short-circuit would silently strip four construction inputs from any lead
    whose contact happened to carry an industry."""
    crm_, _, obs = crm_with(StubHubSpotClient, [   # this stub DOES carry industry
        FakeResponse(200, {"results": [{"toObjectId": 9001}]}),
        FakeResponse(200, {"properties": {"name": "M1 Finance",
                                          "about_us": "Fintech platform.",
                                          "hs_industry_group": "Automated Investing"}}),
        FakeResponse(200, {"properties": {"lead_context": "Research narrative."}}),
    ])
    profile = crm_.get_lead_profile("42")

    # The contact's own values still win where it has them...
    assert profile.industry == "Software"
    assert profile.company == "Acme"
    # ...but the walk ran anyway, so the company-only fields are populated
    # instead of being silently lost to the rev-7 short-circuit.
    assert profile.company_about == "Fintech platform."
    assert profile.industry_group == "Automated Investing"
    assert "company_enriched" in events(obs)
