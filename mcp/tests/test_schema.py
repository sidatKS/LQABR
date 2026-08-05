"""Schema validation — the same schema on the read (5) and the write (9)."""

import pytest

from lqabr_core.types import LeadProfile
from mcp.hubspot.schema import (
    EMAIL_STATUS_VALUES,
    SchemaValidationError,
    campaign_complete_property,
    object_id_property,
    validate_profile,
    validate_writeback,
)


def lead(**overrides):
    base = dict(external_employee_id="E00002", job_title="VP Engineering", company="Acme",
                email="jane@acme.example", phone="+15551234567", industry="Software",
                company_size_revenue="5000000", location="Austin, TX",
                linkedin_url="https://linkedin.example/in/jane",
                external_company_id="C-1", probability=10, hubspot_contact_id="42")
    base.update(overrides)
    return LeadProfile(**base)


# ---------------------------------------------------------------- read side
def test_a_complete_profile_validates_and_carries_the_employee_id():
    validated = validate_profile(lead())
    assert validated.object_id == "42"
    assert validated.employee_id == "E00002"
    assert validated.missing_pointers == []


def test_the_named_construction_fields_are_carried_through():
    """The confirmed contact schema has no name properties — a lead is
    identified by employee_id, which is what construction addresses."""
    validated = validate_profile(lead())
    assert validated.company_id == "C-1"
    assert validated.industry == "Software"
    assert validated.employee_id and validated.email_id


def test_a_lead_with_no_email_is_rejected_with_a_reason_not_dropped():
    with pytest.raises(SchemaValidationError) as exc:
        validate_profile(lead(email=None))
    assert "bad-data" in str(exc.value)
    assert "no email ID" in str(exc.value)


def test_a_profile_with_no_contact_id_is_rejected():
    with pytest.raises(SchemaValidationError):
        validate_profile(lead(hubspot_contact_id=None))


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


def test_the_render_context_offers_no_identifier_it_does_not_have():
    """An absent employee_id must not become a placeholder. It is simply
    not offered, and DRAFTING_RULES tells the model to write around it."""
    context = validate_profile(lead(external_employee_id=None)).as_context()
    assert context["employee_id"] == ""
    assert context["job_title"] == "VP Engineering"


def test_the_context_offers_only_the_five_named_construction_fields():
    """The model must not be handed a field the confirmed HubSpot schema
    does not carry — no contact company NAME, no location — or it writes a
    blank or invents one. email_id addresses the message, it is not body
    content."""
    context = validate_profile(lead()).as_context()
    assert set(context) == {"employee_id", "company_id", "job_title", "industry"}


# --------------------------------------------------------------- write side
def test_a_valid_writeback_normalises_to_strings():
    written = validate_writeback({"lqabr_email_status": "delivered", "probability": 12})
    assert written == {"lqabr_email_status": "DELIVERED", "probability": "12"}


def test_every_allowed_status_value_passes():
    for value in EMAIL_STATUS_VALUES:
        assert validate_writeback({"lqabr_email_status": value})


def test_an_out_of_vocabulary_status_is_caught_before_the_hop():
    with pytest.raises(SchemaValidationError) as exc:
        validate_writeback({"lqabr_email_status": "CLICKED"})
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
