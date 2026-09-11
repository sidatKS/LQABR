"""The exact wire shape of upsert_lead_profile.

The server takes FLAT keyword arguments — the identity fields and the
properties side by side at the top level:

    {"employee_id": ..., "company_id": ..., "decision_maker_flag": ...,
     "voice_status": "INITIATED", "last_modfied_voice": "..."}

It rejects an objectId/properties wrapper with five validation errors:
objectId and properties unexpected, the three identity fields
missing-required. text_voice shipped the wrapper anyway, so every write it
made failed against the real server while every mocked test passed — the
fakes were asserted at the client boundary and never saw the wire.

Confirmed twice: live against the MCP server on 2026-08-21, and on
2026-09-08 against the research agent's working call, which lands.

These tests pin the shape itself, which is the only thing that was ever
wrong.
"""

from __future__ import annotations

import pytest

from mcp_client import StepFiveMCPClient
from text_voice_core.types import CRMError, VoiceLead


def _lead(**overrides):
    base = dict(object_id="123", employee_id="E1", company_id="C1",
                decision_maker=True, probability=30, voice_status="PENDING",
                phone_number="+15550001111")
    base.update(overrides)
    return VoiceLead(**base)


def _client():
    """A client whose only faked seam is the JSON-RPC call itself, so the
    argument dict under test is exactly what would go on the wire."""
    client = StepFiveMCPClient()
    sent = []
    client._call_tool = lambda name, arguments: (
        sent.append((name, dict(arguments))), {"status": "ok"})[1]
    return client, sent


def test_write_arguments_are_flat_with_no_wrapper():
    client, sent = _client()
    client.upsert_lead("123", "INITIATED", lead=_lead())
    name, args = sent[0]
    assert name == "upsert_lead_profile"
    assert "objectId" not in args, "the wrapper shape is what the server rejects"
    assert "object_id" not in args
    assert "properties" not in args, "properties go FLAT, not nested"


def test_write_carries_the_three_required_identity_fields():
    client, sent = _client()
    client.upsert_lead("123", "INITIATED", lead=_lead())
    _, args = sent[0]
    assert args["employee_id"] == "E1"
    assert args["company_id"] == "C1"
    assert args["decision_maker_flag"] is True


def test_the_property_rides_at_the_top_level_beside_the_identity():
    client, sent = _client()
    client.upsert_lead("123", "CALL_PLACED", lead=_lead())
    _, args = sent[0]
    assert args["voice_status"] == "CALL_PLACED"


def test_the_hubspot_typo_is_preserved():
    """last_modfied_voice is the real property name; last_modified_voice does
    not exist on the portal (verified live). Spelling it correctly writes to
    nothing."""
    client, sent = _client()
    client.upsert_lead("123", "INITIATED", lead=_lead())
    _, args = sent[0]
    assert "last_modfied_voice" in args
    assert "last_modified_voice" not in args


def test_a_decision_maker_of_false_is_a_value_not_a_missing_field():
    """decision_maker is a HubSpot bool. Testing it for falsiness rather than
    absence blocked every write for a contact who simply is not the decision
    maker — and with the claim fail-closed, those leads were never called."""
    client, sent = _client()
    client.upsert_lead("123", "INITIATED", lead=_lead(decision_maker=False))
    _, args = sent[0]
    assert args["decision_maker_flag"] is False


def test_a_blank_identity_field_refuses_the_write_loudly():
    client, sent = _client()
    with pytest.raises(CRMError, match="employee_id"):
        client.upsert_lead("123", "INITIATED", lead=_lead(employee_id=None))
    assert not sent, "nothing may go on the wire once identity is known bad"


def test_record_call_outcome_reuses_its_pre_write_read():
    """Step 8 already reads the lead for its probability; the write needs the
    same record. Reading twice is a wasted round trip per outcome."""
    client, sent = _client()
    reads = []
    client.get_lead = lambda oid: (reads.append(oid), _lead())[1]
    client.record_call_outcome("123", "answered_and_engaged", lead=_lead())
    assert reads == [], "a lead was supplied; no read should have happened"
    _, args = sent[0]
    assert args["employee_id"] == "E1"


def test_write_without_a_lead_fetches_the_identity_rather_than_guessing():
    client, sent = _client()
    reads = []
    client.get_lead = lambda oid: (reads.append(oid), _lead())[1]
    client.upsert_lead("123", "INITIATED")
    assert reads == ["123"], "identity is required, so it must be fetched"
    _, args = sent[0]
    assert args["company_id"] == "C1"
