"""STEP 6 — get_lead_profile (READ). The shared read path, never writes."""

from __future__ import annotations

import pytest

from lqabr_core.leadgen.hubspot.crm import get_lead_profile, upsert_lead_profiles
from lqabr_core.leadgen.hubspot.schema import LeadProfile


def seed(fake_hubspot) -> None:
    upsert_lead_profiles(
        LeadProfile(
            employee_id="E1",
            company_id="C1",
            decision_maker_flag="Yes",
            job_title="Head of Ops",
            email="lead@example.com",
            phone="555-0001",
            industry="MANUFACTURING",
            annual_revenue_m="12.5",
            frequency_of_purchase="Quarterly",
        ),
        "lead-seed",
    )


def test_returns_the_profile_plus_hubspot_ids(fake_hubspot):
    seed(fake_hubspot)
    record = get_lead_profile(employee_id="E1")

    assert record.found is True
    assert record.contact_hs_id and record.company_hs_id
    assert isinstance(record.profile, LeadProfile)
    assert record.profile.employee_id == "E1"
    assert record.profile.company_id == "C1"
    assert record.profile.job_title == "Head of Ops"
    assert record.profile.industry == "MANUFACTURING"
    assert record.profile.annual_revenue_m == "12.5"


def test_wrapper_shape_is_the_nine_fields_plus_ids(fake_hubspot):
    seed(fake_hubspot)
    payload = get_lead_profile(employee_id="E1").to_dict()
    assert set(payload) == {
        "found",
        "profile",
        "contact_hs_id",
        "company_hs_id",
        "company_resolved",
        "warnings",
    }
    assert len(payload["profile"]) == 9   # the 9-field contract is unchanged
    assert payload["company_resolved"] is True
    assert payload["warnings"] == []


def test_email_lookup_searches_the_custom_email_id_property(fake_hubspot):
    seed(fake_hubspot)
    fake_hubspot.calls.clear()
    record = get_lead_profile(email="lead@example.com")
    assert record.found is True
    assert record.profile.employee_id == "E1"


def test_unknown_key_returns_not_found(fake_hubspot):
    seed(fake_hubspot)
    record = get_lead_profile(employee_id="NOPE")
    assert record.found is False
    assert record.profile is None


def test_read_never_writes(fake_hubspot):
    seed(fake_hubspot)
    before_contacts = dict(fake_hubspot.contacts)
    before_companies = dict(fake_hubspot.companies)
    fake_hubspot.calls.clear()

    get_lead_profile(employee_id="E1")

    assert fake_hubspot.contacts == before_contacts
    assert fake_hubspot.companies == before_companies
    assert all(method in ("GET", "POST") for method, _ in fake_hubspot.calls)
    assert all(
        not (method == "POST" and path in ("/crm/v3/objects/contacts", "/crm/v3/objects/companies"))
        for method, path in fake_hubspot.calls
    )


def test_requires_a_lookup_key(fake_hubspot):
    with pytest.raises(ValueError):
        get_lead_profile()


def test_decision_maker_bool_round_trips_to_the_flag(fake_hubspot):
    seed(fake_hubspot)
    record = get_lead_profile(employee_id="E1")
    assert record.profile.decision_maker_flag == "Yes"
    assert record.profile.decision_maker is True
