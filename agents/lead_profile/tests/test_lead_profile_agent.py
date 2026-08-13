import lead_profile_agent
from lqabr_core.types import LeadProfile

RECORD = {
    "full_name": "Jane Smith", "job_title": "VP", "company": "Acme",
    "email": "jane@acme.example", "phone": "+1555", "industry": "Mfg",
    "company_size": "Medium", "annual_revenue": "17.3 M₺",
    "location": "Adana, Ceyhan", "linkedin_url": None,
    "external_employee_id": "E1", "external_company_id": "C1", "source": "csv",
}


def test_dry_run_builds_all_nine_pointers_without_hubspot():
    result = lead_profile_agent.build_lead_profiles([RECORD], save_to_hubspot=False)
    assert result["summary"]["profiles_built"] == 1
    profile = result["profiles"][0]
    for pointer in LeadProfile.POINTER_FIELDS:
        assert pointer in profile
    assert profile["stage"] == "profiled"
    assert profile["probability"] > 0


def test_bad_record_is_flagged_with_reason():
    bad = {**RECORD, "email": "", "phone": ""}
    result = lead_profile_agent.build_lead_profiles([bad], save_to_hubspot=False)
    assert result["summary"]["profiles_built"] == 0
    assert result["unresolved"][0]["reason"] == "bad-data: no email and no phone"


def test_get_lead_profile_reads_from_hubspot(monkeypatch):
    class FakeCRM:
        def find_lead_by_email(self, email):
            return LeadProfile(full_name="Jane Smith", email=email, contact_id="9")

    monkeypatch.setattr(lead_profile_agent, "HubSpotClient", FakeCRM)
    result = lead_profile_agent.get_lead_profile("jane@acme.example")
    assert result["contact_id"] == "9"

    class EmptyCRM:
        def find_lead_by_email(self, email):
            return None

    monkeypatch.setattr(lead_profile_agent, "HubSpotClient", EmptyCRM)
    assert "error" in lead_profile_agent.get_lead_profile("missing@x.com")
