"""The write that makes the agent worth running.

A summary landing on the configured property is the campaign trigger the
gateway's blog-summary route waits for, so these tests care about two things
above all: that the write carries the right properties, and that a write
which did NOT happen never reads as one that did.
"""

from __future__ import annotations

import pytest

from helpers import FakeMCPSession
from summary_core.mcp.client import MCPClient
from summary_core.mcp.hubspot import HubSpotMCP
from summary_core.settings import Settings
from summary_core.types import SummaryResult


@pytest.fixture
def summary():
    return SummaryResult(
        title="Spring Boot 4 arrives",
        summary="A release summary.",
        key_points=["virtual threads", "GraalVM"],
        technologies=["Spring Boot", "GraalVM"],
        industry="Software",
    )


def build(server, **overrides) -> HubSpotMCP:
    settings = Settings(mcp_base_url="http://mcp.local/mcp", **overrides)
    return HubSpotMCP(MCPClient(settings, session=server), settings=settings)


class TestWrite:
    def test_writes_summary_and_industry_to_the_configured_properties(self, summary):
        server = FakeMCPSession(tool_results={"post_patch_crm": {"status": "written"}})
        result = build(server).write_summary("ticket-1", summary)
        assert result.status == "written"
        assert result.ok

        arguments = server.tool_calls[0]["params"]["arguments"]
        assert arguments["object_id"] == "ticket-1"
        properties = arguments["properties"]
        assert "Spring Boot 4 arrives" in properties["blog_summary"]
        assert "virtual threads" in properties["blog_summary"]
        assert properties["blog_industry"] == "SOFTWARE"   # normalised to the portal enum form

    def test_property_and_tool_names_follow_configuration(self, summary):
        server = FakeMCPSession(tools=["patch_ticket"], tool_results={"patch_ticket": {"ok": 1}})
        result = build(server, mcp_tool_write="patch_ticket",
                       hubspot_summary_property="post_summary",
                       hubspot_industry_property="post_industry",
                       mcp_arg_object_id="objectId").write_summary("t-9", summary)
        assert result.status == "written"
        arguments = server.tool_calls[0]["params"]["arguments"]
        assert arguments["objectId"] == "t-9"
        assert set(arguments["properties"]) == {"post_summary", "post_industry"}

    def test_dry_run_computes_the_write_without_sending_it(self, summary):
        server = FakeMCPSession()
        result = build(server, dry_run=True).write_summary("ticket-1", summary)
        assert result.status == "dry_run"
        assert result.ok
        assert result.properties == ["blog_industry", "blog_summary"]
        assert server.tool_calls == [], "dry run must not reach the server"

    def test_no_object_id_is_skipped_with_a_reason_not_written_blindly(self, summary):
        server = FakeMCPSession()
        result = build(server).write_summary("", summary)
        assert result.status == "skipped"
        assert "nothing to write to" in result.error
        assert server.tool_calls == []

    def test_a_rejected_write_is_an_error_not_a_success(self, summary):
        server = FakeMCPSession(tool_results={
            "post_patch_crm": {"error": "bad-data: unknown property blog_summary"}})
        result = build(server).write_summary("ticket-1", summary)
        assert result.status == "error"
        assert not result.ok
        assert "unknown property" in result.error

    def test_transport_failure_is_reported_not_swallowed(self, summary):
        server = FakeMCPSession(status_queue=[500, 500, 500])
        result = build(server).write_summary("ticket-1", summary)
        assert result.status == "error"
        assert "HTTP 500" in result.error

    def test_summary_text_is_capped_below_the_hubspot_field_limit(self):
        big = SummaryResult(summary="x" * 100_000, title="T")
        assert len(big.as_hubspot_text()) <= 60_000

    def test_extra_properties_are_merged(self, summary):
        server = FakeMCPSession()
        build(server).write_summary("t-1", summary, extra_properties={"source_url": "https://x"})
        properties = server.tool_calls[0]["params"]["arguments"]["properties"]
        assert properties["source_url"] == "https://x"


class TestRead:
    def test_profile_is_returned_when_the_mcp_has_one(self):
        server = FakeMCPSession(tool_results={
            "get_lead_profile_details": {"email_id": "a@b.com", "industry": "Fintech"}})
        assert build(server).get_lead_profile("42")["industry"] == "Fintech"

    def test_a_failed_read_degrades_to_no_profile(self):
        """The document is the job; a profile is a nice-to-have."""
        server = FakeMCPSession(status_queue=[500, 500, 500])
        assert build(server).get_lead_profile("42") == {}

    def test_an_error_payload_degrades_too(self):
        server = FakeMCPSession(tool_results={
            "get_lead_profile_details": {"error": "bad-data: no email"}})
        assert build(server).get_lead_profile("42") == {}


def test_ensure_ready_checks_the_tools_this_agent_actually_uses():
    server = FakeMCPSession(tools=["get_lead_profile_details", "post_patch_crm"])
    assert build(server).ensure_ready()
