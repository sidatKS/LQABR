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
    base = dict(full_name="Jane Smith",
                external_employee_id="E00002", job_title="VP Engineering", company="Acme",
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
    """The email greets by first name; the standard firstname/lastname
    properties are split off full_name and carried into construction."""
    validated = validate_profile(lead())
    assert validated.first_name == "Jane"
    assert validated.last_name == "Smith"
    assert validated.company_id == "C-1"
    assert validated.industry == "Software"
    assert validated.email_id


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


def test_an_absent_name_is_left_empty_not_placeholdered():
    """A lead with no name must not become a placeholder. first_name is
    simply empty, and DRAFTING_RULES tells the model to open with a plain
    nameless greeting rather than invent one or fall back to an id."""
    context = validate_profile(lead(full_name=None)).as_context()
    assert context["first_name"] == ""
    assert context["last_name"] == ""
    assert context["job_title"] == "VP Engineering"


def test_the_internal_employee_id_is_never_offered_to_construction():
    """The greeting is the first name; employee_id is an internal identifier
    and must never reach the prose. The model must not be handed a field the
    schema does not carry (no company NAME, no location) either."""
    context = validate_profile(lead()).as_context()
    assert set(context) == {"first_name", "last_name", "company_id",
                            "job_title", "industry"}
    assert "employee_id" not in context
    assert context["first_name"] == "Jane"


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
