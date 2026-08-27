"""The ADK tool surface — plain functions, so they are testable without a runner."""

from __future__ import annotations

import json

import pytest

import tools
from helpers import FakeMCPSession
from summary_core.mcp.client import MCPClient
from summary_core.mcp.hubspot import HubSpotMCP
from summary_core.settings import Settings

SUMMARY_JSON = json.dumps({"summary": "It shipped.", "title": "T", "industry": "Software"})


@pytest.fixture
def wired():
    settings = Settings(mcp_base_url="http://mcp.local/mcp", allow_private_hosts=True)
    server = FakeMCPSession()
    tools.configure(settings, HubSpotMCP(MCPClient(settings, session=server), settings=settings))
    yield server
    tools.configure(Settings())


class TestFetchDocument:
    def test_text(self, wired):
        assert tools.fetch_document("text", "hello world")["text"] == "hello world"

    def test_json_payload_from_a_json_string(self, wired):
        result = tools.fetch_document("json", payload_json='{"title": "Q3", "n": 1}')
        assert result["title"] == "Q3"
        assert '"n"' in result["text"]

    def test_select_narrows_it(self, wired):
        result = tools.fetch_document("json", payload_json='{"a": {"b": "inner"}}', select="$.a.b")
        assert result["text"] == "inner"

    def test_a_refused_url_comes_back_as_an_error_not_an_exception(self, wired):
        """ADK tools must return, not raise — the model has to read the reason."""
        result = tools.fetch_document("url", "file:///etc/passwd")
        assert "only http and https" in result["error"]

    def test_malformed_payload_json_is_named(self, wired):
        assert "not valid JSON" in tools.fetch_document("json", payload_json="{oops")["error"]

    def test_unknown_kind_is_named(self, wired):
        assert "unknown source kind" in tools.fetch_document("smoke-signal", "x")["error"]


class TestWrite:
    def test_writes_and_reports_the_status(self, wired):
        result = tools.write_summary_to_hubspot("t-1", SUMMARY_JSON)
        assert result["status"] == "written"
        assert wired.tool_calls[0]["params"]["arguments"]["objectId"] == "t-1"

    def test_empty_summary_is_skipped_not_written(self, wired):
        result = tools.write_summary_to_hubspot("t-1", json.dumps({"summary": "  "}))
        assert result["status"] == "skipped"
        assert wired.tool_calls == []

    def test_malformed_summary_json_is_named(self, wired):
        assert "not valid JSON" in tools.write_summary_to_hubspot("t-1", "{oops")["error"]

    def test_a_rejected_write_is_reported_as_an_error(self):
        settings = Settings(mcp_base_url="http://mcp.local/mcp")
        server = FakeMCPSession(tool_results={"post_patch_crm": {"error": "bad-data: nope"}})
        tools.configure(settings, HubSpotMCP(MCPClient(settings, session=server), settings=settings))
        assert tools.write_summary_to_hubspot("t-1", SUMMARY_JSON)["status"] == "error"


def test_get_lead_profile(wired):
    wired.tool_results["get_lead_profile_details"] = {"industry": "Fintech"}
    assert tools.get_lead_profile("42")["industry"] == "Fintech"


def test_agent_tool_list_is_what_the_agent_advertises():
    names = [t.__name__ for t in tools.AGENT_TOOLS]
    assert names == ["fetch_document", "get_lead_profile", "write_summary_to_hubspot"]
    for tool in tools.AGENT_TOOLS:
        assert tool.__doc__, f"{tool.__name__} has no docstring — the model reads it"
