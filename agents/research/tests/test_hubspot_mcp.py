"""The HubSpot surface: reads shape correctly, writes fail loudly."""

from __future__ import annotations


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
    # `not_writable`, NOT `skipped`: the note could not be landed. `skipped`
    # means nothing needed doing, and reporting this as a success is what
    # "status follows the WRITE" forbids.
    assert result.status == "not_writable"
    assert result.ok is False
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
    assert result.status == "not_writable" and result.ok is False
    assert client.calls == []


# --- "could not ask" is not "nobody matched", on the READS too -------------
# Seen live 2026-08-25: the MCP could not read its own HubSpot token from
# Secret Manager and answered `{"found": false, "status": "halted",
# "failure_kind": "systemic", "reasons": ["AuthConfigError: ..."]}`. The agent
# reported "no blog summary found for objectId 329473274558" — sending whoever
# read that looking for a record that was sitting in the CRM the whole time.

HALTED = {"found": False, "status": "halted", "failure_kind": "systemic",
          "reasons": ["AuthConfigError: SecretAccessError: could not read "
                      "projects/ldqfingsrv-dev/secrets/lqabr-hubspot-access-token/"
                      "versions/latest: RetryError: Timeout of 60.0s exceeded"]}


def test_a_halted_blog_read_reports_the_mcps_reason_not_a_missing_record():
    mcp, _ = _mcp({"get_blog_summary": HALTED})
    assert mcp.read_blog("329473274558") is None
    assert "AuthConfigError" in mcp.last_error
    assert "lqabr-hubspot-access-token" in mcp.last_error


def test_a_halted_lead_read_reports_the_mcps_reason_too():
    mcp, _ = _mcp({"get_lead_profile": HALTED})
    assert mcp.read_lead("533963448020") is None
    assert "AuthConfigError" in mcp.last_error


def test_a_genuine_not_found_carries_no_reason():
    """The distinction has to survive: an absent record is still just absent."""
    mcp, _ = _mcp({"get_blog_summary": {"found": False},
                   "get_lead_profile": {"found": False}})
    assert mcp.read_blog("1") is None and mcp.last_error == ""
    assert mcp.read_lead("2") is None and mcp.last_error == ""


def test_a_transport_error_is_kept_as_the_reason():
    mcp, _ = _mcp({"get_blog_summary": MCPError("MCP initialize failed: refused")})
    assert mcp.read_blog("1") is None
    assert "refused" in mcp.last_error


# --- the MCP lead-listing path: config-reachable, so it must be tested -----
# `use_direct_lead_lookup` defaults TRUE because the MCP has no listing tool
# yet. The day it grows one this branch is what runs, and PROJECT_CONTEXT
# promises the switch costs one config flip and no code edit. An untested
# branch cannot honour that promise.

def _mcp_via_tool(results, monkeypatch):
    monkeypatch.setenv("LQABR_RESEARCH_USE_DIRECT_LEAD_LOOKUP", "0")
    settings = get_settings(refresh=True)
    return HubSpotMCP(client=FakeMCPClient(results), settings=settings), settings


def test_the_mcp_tool_lists_leads_when_the_direct_lookup_is_off(monkeypatch):
    mcp, settings = _mcp_via_tool(
        {"list_leads_by_industry": {"leads": ["533990588137", "533994194677"]}},
        monkeypatch)
    assert mcp.list_leads_by_industry("HEALTHCARE") == ["533990588137", "533994194677"]
    assert settings.mcp_tool_list_leads == "list_leads_by_industry"


def test_lead_rows_are_read_whatever_the_id_is_called(monkeypatch):
    """The reply shape has drifted before; read defensively, not by one spelling."""
    mcp, _ = _mcp_via_tool({"list_leads_by_industry": {"results": [
        {"contact_hs_id": "1"}, {"object_id": "2"}, {"id": "3"}, "4"]}}, monkeypatch)
    assert mcp.list_leads_by_industry("HEALTHCARE") == ["1", "2", "3", "4"]


def test_a_rejected_listing_is_none_not_an_empty_industry(monkeypatch):
    """None means "could not ask"; [] means "asked, nobody matched"."""
    mcp, _ = _mcp_via_tool(
        {"list_leads_by_industry": {"error": "tool unavailable"}}, monkeypatch)
    assert mcp.list_leads_by_industry("HEALTHCARE") is None
    mcp, _ = _mcp_via_tool({"list_leads_by_industry": {"leads": []}}, monkeypatch)
    assert mcp.list_leads_by_industry("HEALTHCARE") == []
