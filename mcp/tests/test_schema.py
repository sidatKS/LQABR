"""Schema validation — the same schema on the read (5) and the write (9)."""

import pytest

from lqabr_core.types import LeadProfile
from mcp.hubspot.schema import (
    CONSTRUCTION_FIELDS,
    EMAIL_STATUS_VALUES,
    SchemaValidationError,
    campaign_complete_property,
    lead_context_property,
    object_id_property,
    validate_profile,
    validate_writeback,
)


def lead(**overrides):
    base = dict(full_name="Jane Smith",
                external_employee_id="E00002", job_title="VP Engineering", company="Acme",
                email="jane@acme.example", phone="+15551234567", industry="Software",
                company_size_revenue="5000000", location="Austin, TX",
                linkedin_url="https://linkedin.example/in/jane",
                external_company_id="C-1", probability=10, object_id="42")
    base.update(overrides)
    return LeadProfile(**base)


# ---------------------------------------------------------------- read side
def test_a_complete_profile_validates_and_carries_the_employee_id():
    validated = validate_profile(lead())
    assert validated.object_id == "42"
    assert validated.employee_id == "E00002"
    assert validated.missing_pointers == []


def test_the_named_construction_fields_are_carried_through():
    """The email greets by first name; the standard firstname/lastname
    properties are split off full_name and carried into construction."""
    validated = validate_profile(lead())
    assert validated.first_name == "Jane"
    assert validated.last_name == "Smith"
    assert validated.company_id == "C-1"
    assert validated.industry == "Software"
    assert validated.email


def test_a_lead_with_no_email_is_rejected_with_a_reason_not_dropped():
    with pytest.raises(SchemaValidationError) as exc:
        validate_profile(lead(email=None))
    assert "bad-data" in str(exc.value)
    assert "no email ID" in str(exc.value)


def test_a_profile_with_no_contact_id_is_rejected():
    with pytest.raises(SchemaValidationError):
        validate_profile(lead(object_id=None))


def test_an_incomplete_profile_still_passes_and_names_its_gaps():
    validated = validate_profile(lead(industry=None, linkedin_url=None))
    assert set(validated.missing_pointers) == {"industry", "linkedin_url"}
    assert validated.industry == ""       # empty, so the skill writes around it


def test_an_out_of_range_probability_is_a_schema_error():
    with pytest.raises(SchemaValidationError):
        validate_profile(lead(probability=140))


def test_a_missing_employee_id_is_a_named_gap_not_an_invented_value():
    validated = validate_profile(lead(external_employee_id=None))
    assert validated.employee_id == ""
    assert "external_employee_id" in validated.missing_pointers


def test_an_absent_name_is_left_empty_not_placeholdered():
    """A lead with no name must not become a placeholder. first_name is
    simply empty, and DRAFTING_RULES tells the model to open with a plain
    nameless greeting rather than invent one or fall back to an id."""
    context = validate_profile(lead(full_name=None)).as_context()
    assert context["first_name"] == ""
    assert context["last_name"] == ""
    assert context["job_title"] == "VP Engineering"


def test_the_internal_identifiers_are_never_offered_to_construction():
    """The greeting is the first name. `employee_id` and `company_id` are
    internal references and must never reach the prose — `company_id` was
    dropped from the context in rev 8, when the MCP started reading the real
    company NAME off the associated company and there was no longer any reason
    to hand the model a stand-in for it."""
    context = validate_profile(lead()).as_context()
    assert set(context) == {"email", "first_name", "last_name", "company",
                            "job_title", "industry", "industry_group",
                            "company_about", "company_website", "annual_revenue",
                            "lead_context"}
    assert "employee_id" not in context
    assert "company_id" not in context
    assert context["first_name"] == "Jane"


# --------------------------------------------------------------- write side
def test_a_valid_writeback_normalises_to_strings():
    written = validate_writeback({"email_status": "delivered", "probability": 12})
    assert written == {"email_status": "DELIVERED", "probability": "12"}


def test_every_allowed_status_value_passes():
    for value in EMAIL_STATUS_VALUES:
        assert validate_writeback({"email_status": value})


def test_an_out_of_vocabulary_status_is_caught_before_the_hop():
    with pytest.raises(SchemaValidationError) as exc:
        validate_writeback({"email_status": "CLICKED"})
    assert "schema-error" in str(exc.value)


def test_an_unknown_property_name_is_rejected():
    with pytest.raises(SchemaValidationError):
        validate_writeback({"lqabr_emial_status": "SENT"})


def test_a_non_numeric_probability_is_rejected():
    with pytest.raises(SchemaValidationError):
        validate_writeback({"probability": "high"})


def test_probability_outside_0_to_100_is_rejected():
    with pytest.raises(SchemaValidationError):
        validate_writeback({"probability": 101})


def test_the_campaign_complete_column_writes_a_hubspot_boolean():
    assert validate_writeback({campaign_complete_property(): True}) == {
        campaign_complete_property(): "true"}
    assert validate_writeback({campaign_complete_property(): False}) == {
        campaign_complete_property(): "false"}


def test_the_campaign_complete_name_is_config_because_it_is_a_placeholder(monkeypatch):
    monkeypatch.setenv("LQABR_HUBSPOT_CAMPAIGN_COMPLETE_PROPERTY", "lqabr_email_done")
    assert campaign_complete_property() == "lqabr_email_done"
    assert validate_writeback({"lqabr_email_done": True}) == {"lqabr_email_done": "true"}


def test_the_object_id_property_is_config_too(monkeypatch):
    monkeypatch.setenv("LQABR_HUBSPOT_OBJECT_ID_PROPERTY", "lqabr_object")
    assert object_id_property() == "lqabr_object"


def test_an_empty_bag_is_rejected_rather_than_a_no_op_hop():
    with pytest.raises(SchemaValidationError):
        validate_writeback({})
    with pytest.raises(SchemaValidationError):
        validate_writeback({"probability": None})


# --------------------------------------------------- rev 8: the lead_context
def test_a_lead_context_on_the_record_is_carried_onto_the_profile():
    """Written by the research agent at FRD step 7, read by the email agent at
    step 9. It rides on `extra` because `LeadProfile` is shared with the
    sibling agents and its 9-pointer shape is not this schema's to change."""
    validated = validate_profile(lead(extra={"lead_context": "  Acme is consolidating.  "}))
    assert validated.lead_context == "Acme is consolidating."
    assert validated.has_lead_context is True
    assert validated.as_context()["lead_context"] == "Acme is consolidating."
    assert validated.to_dict()["lead_context"] == "Acme is consolidating."


def test_a_lead_with_no_context_still_validates_and_is_not_defaulted():
    """Strictly additive: a portal without the property, or a lead the research
    agent has not reached, validates exactly as before. It must NOT be given a
    placeholder narrative — the email agent's gate is what handles the gap, and
    it can only do that if the absence survives validation."""
    validated = validate_profile(lead())
    assert validated.lead_context == ""
    assert validated.has_lead_context is False


def test_a_whitespace_only_context_is_not_a_context():
    assert validate_profile(lead(extra={"lead_context": "\n \t "})).has_lead_context is False


def test_the_lead_context_property_is_writable_through_the_same_schema():
    """The research agent's step-7 persist goes through `validate_writeback`
    like every other write, so one MCP serves both agents. (The email agent
    only ever reads it.)"""
    written = validate_writeback({lead_context_property(): "Acme is consolidating."})
    assert written == {lead_context_property(): "Acme is consolidating."}


def test_the_lead_context_property_name_is_config_not_code(monkeypatch):
    """Not confirmed against the live schema yet — the 2026-08-05 audit of all
    410 contact properties found neither `object_id` nor
    `email_campaign_complete`, so this one is assumed absent too. Renaming it
    must never be a code edit."""
    assert lead_context_property() == "lead_context"
    monkeypatch.setenv("LQABR_HUBSPOT_LEAD_CONTEXT_PROPERTY", "lqabr_research_context")
    assert lead_context_property() == "lqabr_research_context"
    assert validate_writeback({"lqabr_research_context": "x"}) == {"lqabr_research_context": "x"}
    # And the old name is no longer writable once renamed.
    with pytest.raises(SchemaValidationError):
        validate_writeback({"lead_context": "x"})


def test_an_empty_property_name_disables_the_column_without_opening_a_hole(monkeypatch):
    """Clearing the name is the escape hatch for a portal that has no such
    column. It must not make the empty string a writable property."""
    monkeypatch.setenv("LQABR_HUBSPOT_LEAD_CONTEXT_PROPERTY", "")
    assert lead_context_property() == ""
    with pytest.raises(SchemaValidationError):
        validate_writeback({"": "x"})


# ------------------------- the confirmed construction field list (2026-08-18)
#: Exactly what email construction receives, agreed with the user 2026-08-18,
#: imported from the schema so this suite asserts the REAL list rather than a
#: copy of it that could drift.


def test_construction_receives_exactly_the_agreed_field_list():
    context = validate_profile(lead(
        extra={"lead_context": "Research narrative.",
               "company_website": "https://m1.com",
               "company_about": "Fintech platform.",
               "industry_group": "Investment / Wealth Management"})).as_context()
    assert set(context) == set(CONSTRUCTION_FIELDS)


def test_the_lead_profile_still_carries_the_identifiers_construction_does_not_get():
    """`employee_id` and `company_id` remain part of the PROFILE — the portal
    shows them and `to_dict()` returns them. They are simply not construction
    inputs: they are references for our systems and cannot appear in prose."""
    validated = validate_profile(lead())
    assert validated.employee_id == "E00002"
    assert validated.company_id == "C-1"
    assert validated.to_dict()["employee_id"] == "E00002"
    assert validated.to_dict()["company_id"] == "C-1"

    context = validated.as_context()
    assert "employee_id" not in context and "company_id" not in context


def test_the_email_address_reaches_construction_as_a_fact():
    """Offered so the model knows WHO it is writing to — a personal address and
    a corporate one are different readers. DRAFTING_RULES forbids writing it
    into the body; that it is available is what this asserts."""
    assert validate_profile(lead()).as_context()["email"] == "jane@acme.example"


def test_annual_revenue_is_passed_through_verbatim_and_unitless():
    """HubSpot stores it with no unit — M1 Finance holds the literal string
    `4.7`. It must reach the model unchanged: inventing a unit here would be
    the same fabrication the drafting rules forbid the model from making."""
    context = validate_profile(lead(company_size_revenue="4.7")).as_context()
    assert context["annual_revenue"] == "4.7"
    assert "$" not in context["annual_revenue"]
    assert "M" not in context["annual_revenue"] and "B" not in context["annual_revenue"]


def test_a_company_with_no_revenue_offers_no_revenue_field_value():
    context = validate_profile(lead(company_size_revenue=None)).as_context()
    assert context["annual_revenue"] == ""


def test_the_construction_view_and_the_model_context_cannot_drift_apart():
    """One source of truth. `as_context()` feeds the model, `construction_view()`
    feeds the operator's `get_lead_profile`. If they ever list different fields,
    the person reviewing a draft is reviewing a different lead than the one it
    was written from — which is exactly the bug reported on 2026-08-18."""
    validated = validate_profile(lead())
    assert tuple(validated.construction_view()) == CONSTRUCTION_FIELDS
    assert set(validated.as_context()) == set(CONSTRUCTION_FIELDS)


def test_the_construction_view_shows_a_real_gap_rather_than_the_model_fallback():
    """`as_context()` substitutes "your role" for an empty job title so the
    model has something to write with. A PROFILE view must not: an operator
    asking why a draft reads oddly has to see that the field is empty."""
    validated = validate_profile(lead(job_title=None))
    assert validated.as_context()["job_title"] == "your role"
    assert validated.construction_view()["job_title"] == ""


def test_the_construction_view_carries_the_company_fields_not_the_identifiers():
    validated = validate_profile(lead(
        extra={"company_website": "https://www.rocketmatter.com",
               "company_about": "Cloud-based legal practice management software.",
               "industry_group": "Legal Practice Management",
               "lead_context": "Research narrative."}))
    view = validated.construction_view()

    assert view["company"] == "Acme"
    assert view["industry_group"] == "Legal Practice Management"
    assert view["company_about"].startswith("Cloud-based legal")
    assert view["company_website"] == "https://www.rocketmatter.com"
    assert view["annual_revenue"] == "5000000"
    assert "employee_id" not in view and "company_id" not in view
