from lqabr_core.crm.base import CRMClient, CRMError
from lqabr_core.probability import BASE_PROBABILITY
from lqabr_core.profile import build_profile, profile_and_upsert, profile_records
from lqabr_core.types import LeadSource, LeadStage


RECORD = {
    "full_name": "Jane Smith",
    "job_title": "VP Procurement",
    "company": "Acme Corp",
    "email": "jane@acme.example",
    "phone": "+15550001111",
    "industry": "Manufacturing",
    "company_size": "Medium",
    "annual_revenue": "17.3 M",
    "location": "Chicago, IL",
    "linkedin_url": "https://linkedin.com/in/janesmith",
    "external_employee_id": "E1",
    "external_company_id": "C1",
    "source": "zoominfo",
}


class FakeCRM(CRMClient):
    def __init__(self, fail_emails=()):
        self.upserts = []
        self.fail_emails = set(fail_emails)

    def upsert_lead(self, profile):
        if profile.email in self.fail_emails:
            raise CRMError("boom")
        profile.contact_id = f"hs-{len(self.upserts)}"
        self.upserts.append(profile)
        return profile

    def get_lead(self, contact_id):  # pragma: no cover
        raise NotImplementedError

    def find_lead_by_email(self, email):  # pragma: no cover
        return None

    def find_lead_by_phone(self, phone):  # pragma: no cover
        return None

    def leads_in_stage(self, stage, min_probability=0, limit=100):  # pragma: no cover
        return []

    def record_event(self, event):  # pragma: no cover
        raise NotImplementedError

    def set_stage(self, contact_id, stage, reason=None):  # pragma: no cover
        pass


def test_build_profile_maps_all_nine_pointers():
    p = build_profile(RECORD)
    assert p.missing_pointers() == []
    assert p.company_size_revenue == "Medium / 17.3 M"
    assert p.source is LeadSource.ZOOMINFO
    assert p.stage is LeadStage.PROFILED
    assert p.probability == BASE_PROBABILITY


def test_non_contactable_record_is_flagged_not_dropped():
    bad = {**RECORD, "email": "", "phone": None}
    result = profile_records([RECORD, bad])
    assert len(result.profiles) == 1
    assert len(result.unresolved) == 1
    assert "no email and no phone" in result.unresolved[0].reason
    assert result.summary["records_seen"] == 2


def test_profile_and_upsert_collects_crm_failures():
    other = {**RECORD, "email": "other@acme.example"}
    crm = FakeCRM(fail_emails={"other@acme.example"})
    result = profile_and_upsert([RECORD, other], crm)
    assert len(result.profiles) == 1
    assert result.profiles[0].contact_id == "hs-0"
    assert len(result.upsert_failures) == 1
    assert result.upsert_failures[0].reason.startswith("crm-error")
