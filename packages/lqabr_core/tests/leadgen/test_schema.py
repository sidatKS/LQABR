"""FR §0 data contract + Step 5 baseline validation."""

from __future__ import annotations

import pytest

from lqabr_core.leadgen.hubspot.schema import (
    LeadProfile,
    SchemaMismatchError,
    assert_valid,
    normalise_industry,
    validate_lead_profile,
)


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


def test_valid_profile_has_no_reasons():
    assert validate_lead_profile(make()) == []


@pytest.mark.parametrize("field", ["job_title", "email", "phone", "industry", "annual_revenue_m", "frequency_of_purchase"])
def test_optional_fields_may_be_none(field):
    assert validate_lead_profile(make(**{field: None})) == []


@pytest.mark.parametrize("field", ["employee_id", "company_id", "decision_maker_flag"])
def test_missing_required_field_is_a_mismatch(field):
    reasons = validate_lead_profile(make(**{field: None}))
    assert any(field in reason for reason in reasons)


@pytest.mark.parametrize("field", ["employee_id", "company_id"])
def test_empty_dedup_key_is_a_mismatch(field):
    reasons = validate_lead_profile(make(**{field: "   "}))
    assert any("empty" in reason for reason in reasons)


def test_wrong_type_is_a_mismatch():
    reasons = validate_lead_profile(make(annual_revenue_m=12.5))
    assert any("annual_revenue_m" in reason for reason in reasons)


def test_non_uppercase_industry_is_a_mismatch():
    reasons = validate_lead_profile(make(industry="Manufacturing"))
    assert any("industry" in reason for reason in reasons)


def test_validation_does_not_reject_non_numeric_revenue():
    # DECIDED: baseline only, no network. HubSpot rejects this, not us.
    assert validate_lead_profile(make(annual_revenue_m="not-a-number")) == []


def test_assert_valid_raises_with_lead_ref_id():
    with pytest.raises(SchemaMismatchError) as exc:
        assert_valid(make(employee_id=None), lead_ref_id="lead-abc")
    assert exc.value.lead_ref_id == "lead-abc"
    assert exc.value.source == "validation"


# --- HubSpot property mapping ---------------------------------------------


def test_contact_properties_use_the_contract_names():
    props = make().to_contact_properties()
    assert props["employee_id"] == "E1"
    assert props["jobtitle"] == "Head of Ops"
    assert props["phone"] == "555-0001"
    # CUSTOM property, deliberately not standard "email"
    assert props["email_id"] == "lead@example.com"
    assert "email" not in props


def test_decision_maker_is_a_bool():
    assert make(decision_maker_flag="Yes").to_contact_properties()["decision_maker"] is True
    assert make(decision_maker_flag="yes").to_contact_properties()["decision_maker"] is True
    assert make(decision_maker_flag="No").to_contact_properties()["decision_maker"] is False


def test_company_properties_use_the_contract_names():
    props = make().to_company_properties()
    assert props["company_id"] == "C1"
    assert props["industry"] == "MANUFACTURING"
    assert props["annualrevenue"] == "12.5"
    assert props["frequency_of_purchase"] == "Quarterly"


def test_absent_optionals_are_omitted_not_nulled():
    props = make(job_title=None, phone=None).to_contact_properties()
    assert "jobtitle" not in props
    assert "phone" not in props


def test_normalise_industry():
    assert normalise_industry("  retail ") == "RETAIL"
    assert normalise_industry(None) is None
