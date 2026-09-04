"""The deterministic run, end to end, with every boundary faked.

Fetch, summarise, write — and the thing these tests care about most is that
each step's failure is reported as ITS OWN failure, so an operator reading a
response knows which hop to look at.
"""

from __future__ import annotations

import json

import pytest

from helpers import FakeMCPSession, FakeResponse, FakeSession, html_page
from pipeline import run_summary
from schema import HubSpotTarget, SummaryRequest
from summary_core.mcp.client import MCPClient
from summary_core.mcp.hubspot import HubSpotMCP
from summary_core.settings import Settings

GOOD = {"title": "Spring Boot 4", "topic": "Java", "summary": "It shipped.",
        "key_points": ["virtual threads"], "technologies": ["Spring Boot"],
        "industry": "Software"}


def model(answer=None):
    payload = json.dumps(answer or GOOD)
    return lambda **kwargs: {"choices": [{"message": {"content": payload}}]}


@pytest.fixture
def settings():
    return Settings(allowed_hosts=["example.com", "svc.internal"],
                    mcp_base_url="http://mcp.local/mcp")


def hubspot_for(server, settings):
    return HubSpotMCP(MCPClient(settings, session=server), settings=settings)


class TestHappyPath:
    def test_url_to_hubspot(self, settings):
        http = FakeSession([FakeResponse(text=html_page("Spring Boot 4"),
                                         headers={"Content-Type": "text/html"})])
        mcp = FakeMCPSession()
        response = run_summary(
            SummaryRequest(source="https://example.com/post",
                           hubspot=HubSpotTarget(object_id="ticket-1")),
            settings=settings, session=http, hubspot=hubspot_for(mcp, settings),
            completion=model())

        assert response.status == "completed"
        assert response.summary.summary == "It shipped."
        assert response.source.kind == "url"
        assert response.hubspot.status == "written"
        properties = mcp.tool_calls[0]["params"]["arguments"]["properties"]
        assert "It shipped." in properties["blog_summary"]
        assert properties["blog_industry"] == "SOFTWARE"   # normalised to the portal enum form

    def test_raw_json_input_takes_the_same_path(self, settings):
        mcp = FakeMCPSession()
        response = run_summary(
            SummaryRequest(source={"kind": "json", "payload": {"title": "Q3", "rows": [1, 2]}},
                           hubspot=HubSpotTarget(object_id="ticket-2")),
            settings=settings, hubspot=hubspot_for(mcp, settings), completion=model())
        assert response.status == "completed"
        assert response.source.kind == "json"
        assert response.hubspot.status == "written"

    def test_api_input_takes_the_same_path(self, settings):
        http = FakeSession([FakeResponse(headers={"Content-Type": "application/json"},
                                         _json={"article": {"body": "content"}})])
        response = run_summary(
            SummaryRequest(source={"kind": "api", "endpoint": "https://svc.internal/a",
                                   "select": "$.article.body"}),
            settings=settings, session=http, completion=model())
        assert response.status == "completed"
        assert response.source.kind == "api"

    def test_text_input_needs_no_network_at_all(self, settings):
        response = run_summary(SummaryRequest(source="just some prose to summarise"),
                               settings=settings, completion=model())
        assert response.status == "completed" and response.source.kind == "text"

    def test_run_id_is_returned_for_correlation(self, settings):
        response = run_summary(SummaryRequest(source="prose"), settings=settings,
                               completion=model())
        assert response.run_id.startswith("sum-")


class TestNoWrite:
    def test_without_an_object_id_nothing_is_written_and_it_says_so(self, settings):
        response = run_summary(SummaryRequest(source="prose"), settings=settings,
                               completion=model())
        assert response.status == "completed"
        assert response.hubspot.status == "skipped"
        assert "nothing was written" in response.hubspot.error

    def test_dry_run_reaches_no_server(self):
        settings = Settings(dry_run=True, mcp_base_url="http://mcp.local/mcp")
        mcp = FakeMCPSession()
        response = run_summary(
            SummaryRequest(source="prose", hubspot=HubSpotTarget(object_id="t-1")),
            settings=settings, hubspot=hubspot_for(mcp, settings), completion=model())
        assert response.hubspot.status == "dry_run"
        assert mcp.tool_calls == []


class TestFailuresAreAttributed:
    def test_a_bad_source_fails_at_fetch_and_never_calls_the_model(self, settings):
        called = []
        response = run_summary(
            SummaryRequest(source="https://blocked.test/x"), settings=settings,
            completion=lambda **kw: called.append(kw))
        assert response.status == "failed"
        assert "not in LQABR_SUMMARY_ALLOWED_HOSTS" in response.error
        assert response.summary is None
        assert called == [], "a source we refused must not be sent to the model"

    def test_a_bad_model_answer_fails_at_summarize_and_never_writes(self, settings):
        mcp = FakeMCPSession()
        response = run_summary(
            SummaryRequest(source="prose", hubspot=HubSpotTarget(object_id="t-1")),
            settings=settings, hubspot=hubspot_for(mcp, settings),
            completion=lambda **kw: {"choices": [{"message": {"content": "sorry"}}]})
        assert response.status == "failed"
        assert "did not return a usable summary" in response.error
        assert mcp.tool_calls == [], "nothing may reach the CRM without a valid summary"

    def test_a_provider_outage_is_a_reported_failure_not_a_crash(self, settings):
        """No key, a rate limit, an outage — the caller gets the named step."""
        def exploding(**kwargs):
            raise RuntimeError("AuthenticationError: missing API key")

        mcp = FakeMCPSession()
        response = run_summary(
            SummaryRequest(source="prose", hubspot=HubSpotTarget(object_id="t-1")),
            settings=settings, hubspot=hubspot_for(mcp, settings), completion=exploding)
        assert response.status == "failed"
        assert "the model call failed (RuntimeError)" in response.error
        assert mcp.tool_calls == []

    def test_a_failed_write_is_reported_as_failed_not_completed(self, settings):
        mcp = FakeMCPSession(tool_results={"post_patch_crm": {"error": "bad-data: unknown property"}})
        response = run_summary(
            SummaryRequest(source="prose", hubspot=HubSpotTarget(object_id="t-1")),
            settings=settings, hubspot=hubspot_for(mcp, settings), completion=model())
        assert response.status == "failed"
        assert response.hubspot.status == "error"
        assert "unknown property" in response.error
        assert response.summary is not None, "the summary is still returned to the caller"

    def test_extra_properties_and_industry_override_reach_the_write(self, settings):
        mcp = FakeMCPSession()
        run_summary(
            SummaryRequest(source="prose",
                           hubspot=HubSpotTarget(object_id="t-1", industry="Fintech",
                                                 properties={"source_url": "https://x"})),
            settings=settings, hubspot=hubspot_for(mcp, settings), completion=model())
        properties = mcp.tool_calls[0]["params"]["arguments"]["properties"]
        assert properties["blog_industry"] == "FINTECH"    # normalised to the portal enum form
        assert properties["source_url"] == "https://x"
