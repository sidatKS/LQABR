"""The HubSpot surface: reads shape correctly, writes fail loudly."""

from __future__ import annotations

import pytest

from research_core.mcp.client import MCPError
from research_core.mcp.hubspot import HubSpotMCP
from research_core.settings import get_settings
from research_core.types import ResearchNote
from conftest import FakeMCPClient


def _mcp(results):
    settings = get_settings(refresh=True)
    return HubSpotMCP(client=FakeMCPClient(results), settings=settings), settings


def test_read_lead_maps_the_fields():
    mcp, _ = _mcp({"get_lead_profile": {
        "employee_id": "E1", "company_id": "C1", "decision_maker_flag": "Yes",
        "industry": "HEALTHCARE", "company": "Axiom Law", "job_title": "President",
        "first_name": "Mahesh", "last_name": "Puliganti"}})
    lead = mcp.read_lead("533963448020")
    assert lead is not None
    assert lead.company == "Axiom Law"
    assert lead.industry == "HEALTHCARE"
    assert lead.display_name == "Mahesh Puliganti"
    assert lead.writable == []          # all three write ids present


def test_read_lead_reads_nested_payloads():
    mcp, _ = _mcp({"get_lead_profile": {"found": True, "lead": {
        "employee_id": "E1", "company_id": "C1", "decision_maker_flag": "No",
        "industry": "LEGAL_SERVICES", "company_name": "Acme"}}})
    lead = mcp.read_lead("1")
    assert lead is not None and lead.company == "Acme"


def test_read_lead_returns_none_when_not_found():
    mcp, _ = _mcp({"get_lead_profile": {"found": False}})
    assert mcp.read_lead("nope") is None


def test_read_lead_returns_none_on_transport_error():
    mcp, _ = _mcp({"get_lead_profile": MCPError("boom")})
    assert mcp.read_lead("1") is None


def test_read_blog_not_found_is_not_an_error():
    mcp, _ = _mcp({"get_blog_summary": {"found": False, "summary": None}})
    assert mcp.read_blog("2026-08-27T09:30:00Z") is None


def test_read_blog_maps_the_summary():
    mcp, _ = _mcp({"get_blog_summary": {"found": True, "ticket_hs_id": "T1", "summary": {
        "blog_summary": "text", "blog_industry": "HEALTHCARE",
        "blog_published_at": "2026-08-27T09:30:00Z"}}})
    blog = mcp.read_blog("2026-08-27T09:30:00Z")
    assert blog is not None and blog.usable and blog.ticket_id == "T1"


def test_write_sends_the_three_required_ids(lead):
    client = FakeMCPClient({"upsert_lead_profile": {"status": "updated"}})
    mcp = HubSpotMCP(client=client, settings=get_settings(refresh=True))
    result = mcp.write_context(lead, ResearchNote(text="note", sources=[]))
    assert result.status == "written"
    name, args = client.calls[-1]
    assert name == "upsert_lead_profile"
    for key in ("employee_id", "company_id", "decision_maker_flag", "lead_context"):
        assert key in args


def test_write_is_skipped_when_ids_are_missing(lead):
    lead.company_id = ""
    client = FakeMCPClient({})
    mcp = HubSpotMCP(client=client, settings=get_settings(refresh=True))
    result = mcp.write_context(lead, ResearchNote(text="note"))
    assert result.status == "skipped"
    assert "bad-data" in result.error and "company_id" in result.error
    assert client.calls == []           # nothing was sent


def test_halted_status_is_a_failure_not_a_success(lead):
    """The central MCP reports a systemic failure as a BODY with status=halted.
    Regression: an earlier version read that as 'written'."""
    client = FakeMCPClient({"upsert_lead_profile": {
        "status": "halted", "failure_kind": "systemic",
        "reasons": ["AuthConfigError: could not read the HubSpot token"]}})
    mcp = HubSpotMCP(client=client, settings=get_settings(refresh=True))
    result = mcp.write_context(lead, ResearchNote(text="note"))
    assert result.status == "error"
    assert not result.ok
    assert "AuthConfigError" in result.error


def test_dry_run_computes_but_never_sends(lead, monkeypatch):
    monkeypatch.setenv("LQABR_RESEARCH_DRY_RUN", "1")
    client = FakeMCPClient({})
    mcp = HubSpotMCP(client=client, settings=get_settings(refresh=True))
    result = mcp.write_context(lead, ResearchNote(text="note"))
    assert result.status == "dry_run" and result.ok
    assert client.calls == []


def test_empty_note_is_skipped(lead):
    client = FakeMCPClient({})
    mcp = HubSpotMCP(client=client, settings=get_settings(refresh=True))
    result = mcp.write_context(lead, ResearchNote(text="   "))
    assert result.status == "skipped" and client.calls == []
