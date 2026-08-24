"""The run: every failure names its step, and status follows the WRITE."""

from __future__ import annotations

from composer import Composer
from conftest import FakeMCPClient, FakeSearch
from research_core.mcp.hubspot import HubSpotMCP
from research_core.settings import get_settings
from research_core.search.base import SearchError
from pipeline import run_research
from schema import ResearchTarget

LEAD = {"employee_id": "E1", "company_id": "C1", "decision_maker_flag": "Yes",
        "industry": "HEALTHCARE", "company": "Axiom Law", "first_name": "Mahesh"}
BLOG = {"found": True, "ticket_hs_id": "T1", "summary": {
    "blog_summary": "Governed AI needs citations and sign-off.",
    "blog_industry": "HEALTHCARE", "blog_published_at": "2026-08-27T09:30:00Z"}}

# summary_ref_id is the BLOG POST's record id — the MCP reads the blog store
# by it (changed 2026-08-24 from the publication timestamp). `object_id` here
# is the CONTACT's, which is a different record entirely.
TARGET = ResearchTarget(object_id="533963448020", summary_ref_id="329605630651")


def _run(results, provider=None, **kw):
    settings = get_settings(refresh=True)
    client = FakeMCPClient(results)
    hubspot = HubSpotMCP(client=client, settings=settings)
    composer = Composer(provider=provider or FakeSearch(), settings=settings)
    response = run_research(TARGET, settings=settings, hubspot=hubspot,
                            composer=composer, **kw)
    return response, client


def test_happy_path_writes_and_completes():
    response, client = _run({"get_lead_profile": LEAD, "get_blog_summary": BLOG,
                             "upsert_lead_profile": {"status": "updated"}})
    assert response.status == "completed"
    assert response.hubspot.status == "written"
    assert response.note
    assert [c[0] for c in client.calls] == ["get_lead_profile", "get_blog_summary",
                                            "upsert_lead_profile"]


def test_missing_object_id_fails_at_input():
    settings = get_settings(refresh=True)
    hubspot = HubSpotMCP(client=FakeMCPClient({}), settings=settings)
    response = run_research(ResearchTarget(object_id="", blog_published_at="x"),
                            settings=settings, hubspot=hubspot,
                            composer=Composer(provider=FakeSearch(), settings=settings))
    assert response.status == "failed" and "bad-data" in response.error


def test_missing_blog_object_id_fails_with_the_reason():
    settings = get_settings(refresh=True)
    hubspot = HubSpotMCP(client=FakeMCPClient({"get_lead_profile": LEAD}),
                         settings=settings)
    response = run_research(ResearchTarget(object_id="1", summary_ref_id=""),
                            settings=settings, hubspot=hubspot,
                            composer=Composer(provider=FakeSearch(), settings=settings))
    assert response.status == "failed"
    assert "summary_ref_id" in response.error


def test_unknown_lead_is_a_crm_error():
    response, _ = _run({"get_lead_profile": {"found": False}})
    assert response.status == "failed" and "crm-error" in response.error


def test_missing_blog_is_a_crm_error():
    response, _ = _run({"get_lead_profile": LEAD, "get_blog_summary": {"found": False}})
    assert response.status == "failed" and "crm-error" in response.error


def test_search_failure_is_reported_not_raised():
    response, client = _run({"get_lead_profile": LEAD, "get_blog_summary": BLOG},
                            provider=FakeSearch(raises=SearchError("no key")))
    assert response.status == "failed" and "no key" in response.error
    assert "upsert_lead_profile" not in [c[0] for c in client.calls]


def test_rejected_write_makes_the_run_fail_but_keeps_the_note():
    response, _ = _run({"get_lead_profile": LEAD, "get_blog_summary": BLOG,
                        "upsert_lead_profile": {"status": "halted",
                                                "reasons": ["token unreadable"]}})
    assert response.status == "failed"
    assert response.hubspot.status == "error"
    assert response.note          # the work is not lost


def test_skip_when_context_present(monkeypatch):
    monkeypatch.setenv("LQABR_RESEARCH_SKIP_IF_CONTEXT_PRESENT", "1")
    lead = dict(LEAD, lead_context="already here")
    response, client = _run({"get_lead_profile": lead})
    assert response.status == "completed"
    assert response.hubspot.status == "skipped"
    assert [c[0] for c in client.calls] == ["get_lead_profile"]
