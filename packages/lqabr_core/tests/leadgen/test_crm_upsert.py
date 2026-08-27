"""STEP 5 — upsert_lead_profiles (WRITE). post + update are ONE upsert."""

from __future__ import annotations

import json
import os
from pathlib import Path

from lqabr_core.leadgen.hubspot.crm import upsert_lead_profiles
from lqabr_core.leadgen.hubspot.schema import LeadProfile


def make(**overrides) -> LeadProfile:
    base = dict(
        employee_id="E1",
        company_id="C1",
        decision_maker_flag="Yes",
        job_title="Head of Ops",
        email="lead@example.com",
        phone="555-0001",
        industry="MANUFACTURING",
        annual_revenue_m="12.5",
        frequency_of_purchase="Quarterly",
    )
    base.update(overrides)
    return LeadProfile(**base)


def _mismatch_lines() -> list[dict]:
    path = Path(os.environ["LQABR_ERRORS_DIR"]) / "schema_mismatch.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_absent_record_is_created_and_associated(fake_hubspot):
    result = upsert_lead_profiles(make(), "lead-1")

    assert result.status == "pushed"
    assert result.contact_action == "create"
    assert result.company_action == "create"
    assert result.associated is True
    assert result.contact_hs_id and result.company_hs_id
    assert (result.contact_hs_id, result.company_hs_id) in fake_hubspot.associations
    assert ("POST", "/crm/v3/objects/contacts") in fake_hubspot.calls
    assert ("POST", "/crm/v3/objects/companies") in fake_hubspot.calls


def test_present_record_is_patched_not_duplicated(fake_hubspot):
    first = upsert_lead_profiles(make(), "lead-1")
    second = upsert_lead_profiles(make(job_title="VP Ops"), "lead-2")

    assert second.contact_action == "update"
    assert second.company_action == "update"
    assert second.contact_hs_id == first.contact_hs_id
    assert len(fake_hubspot.contacts) == 1
    assert len(fake_hubspot.companies) == 1
    assert fake_hubspot.contacts[second.contact_hs_id]["jobtitle"] == "VP Ops"


def test_dedup_is_on_employee_id_and_company_id(fake_hubspot):
    upsert_lead_profiles(make(), "lead-1")
    # same company, different employee -> new contact, same company
    result = upsert_lead_profiles(make(employee_id="E2"), "lead-2")
    assert result.contact_action == "create"
    assert result.company_action == "update"
    assert len(fake_hubspot.contacts) == 2
    assert len(fake_hubspot.companies) == 1


def test_standard_email_property_is_written(fake_hubspot):
    """Decided 2026-08-25: the standard ``email`` property, not the custom
    ``email_id`` (which required portal provisioning)."""
    result = upsert_lead_profiles(make(), "lead-1")
    props = fake_hubspot.contacts[result.contact_hs_id]
    assert props["email"] == "lead@example.com"
    assert "email_id" not in props
    assert props["decision_maker"] is True


def test_validation_failure_is_recorded_and_never_inserted(fake_hubspot):
    result = upsert_lead_profiles(make(employee_id="  "), "lead-bad")

    assert result.status == "failed"
    assert fake_hubspot.contacts == {}  # nothing written
    lines = _mismatch_lines()
    assert len(lines) == 1
    assert lines[0]["lead_ref_id"] == "lead-bad"
    assert lines[0]["source"] == "validation"
    assert lines[0]["record"]["company_id"] == "C1"  # the record is KEPT


def test_hubspot_400_is_a_schema_mismatch_not_a_crash(fake_hubspot):
    fake_hubspot.reject_company_400 = True
    result = upsert_lead_profiles(make(), "lead-400")

    assert result.status == "failed"
    lines = _mismatch_lines()
    assert lines[0]["source"] == "hubspot"
    assert lines[0]["lead_ref_id"] == "lead-400"


def test_the_run_continues_after_a_failure(fake_hubspot):
    bad = upsert_lead_profiles(make(company_id=""), "lead-bad")
    good = upsert_lead_profiles(make(employee_id="E9"), "lead-good")

    assert bad.status == "failed"
    assert good.status == "pushed"
    assert len(_mismatch_lines()) == 1


def test_retryable_status_is_retried(fake_hubspot):
    fake_hubspot.fail_search_times = 1
    result = upsert_lead_profiles(make(), "lead-retry")
    assert result.status == "pushed"
    searches = [c for c in fake_hubspot.calls if c[1].endswith("/search")]
    assert len(searches) >= 3  # contact search retried once, then company search


def test_a_fresh_token_is_requested_before_every_call(fake_hubspot):
    from lqabr_core.leadgen.hubspot import auth as auth_module

    provider = auth_module._CACHE.provider  # StubTokenProvider from the fixture
    upsert_lead_profiles(make(), "lead-1")
    # private-app style stub never expires -> one mint, reused per call
    assert provider.calls == 1
