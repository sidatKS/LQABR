"""The runtime connection to the HubSpot MCP container.

These tests are the reason the agent can claim "a rename is a config
change": they run the client against a fake server whose tool names the test
chooses, and assert the client discovers, binds, and complains by name.
"""

from __future__ import annotations

import pytest

from helpers import FakeMCPSession, FakeResponse
from summary_core.mcp.client import MCPClient, MCPError, MCPToolMissing, unwrap_result
from summary_core.settings import Settings


@pytest.fixture
def settings():
    return Settings(mcp_base_url="http://mcp.local/mcp", max_retries=3)


@pytest.fixture
def server():
    return FakeMCPSession()


def client_for(server, settings) -> MCPClient:
    return MCPClient(settings, session=server)


class TestHandshake:
    def test_initialize_then_notify_then_list(self, server, settings):
        client = client_for(server, settings)
        client.list_tools()
        assert [c["method"] for c in server.calls] == [
            "initialize", "notifications/initialized", "tools/list"]

    def test_session_id_is_echoed_on_later_calls(self, server, settings):
        client = client_for(server, settings)
        client.list_tools()
        assert server.calls[-1]["headers"]["Mcp-Session-Id"] == "mcp-session-1"

    def test_auth_token_is_sent_as_bearer(self, server):
        client = client_for(server, Settings(mcp_base_url="http://mcp.local/mcp",
                                             mcp_auth_token="t0ken"))
        client.list_tools()
        assert server.calls[0]["headers"]["Authorization"] == "Bearer t0ken"

    def test_handshake_happens_once(self, server, settings):
        client = client_for(server, settings)
        client.list_tools()
        client.call_tool("post_patch_crm", {"object_id": "1"})
        assert server.initialize_count == 1

    def test_unreachable_mcp_is_a_named_error(self, settings):
        import requests

        server = FakeMCPSession(fail_with=requests.ConnectionError("connection refused"))
        with pytest.raises(MCPError, match="connection refused"):
            client_for(server, settings).list_tools()


class TestDiscovery:
    def test_tools_are_read_from_the_server_not_assumed(self, server, settings):
        client = client_for(server, settings)
        assert sorted(client.list_tools()) == [
            "get_lead_profile_details", "list_trigger_leads", "post_patch_crm"]

    def test_ensure_ready_passes_when_configured_tools_exist(self, server, settings):
        client = client_for(server, settings)
        assert client.ensure_ready(["post_patch_crm", "get_lead_profile_details"])

    def test_missing_tool_fails_loudly_and_names_both_sides(self, settings):
        """The startup assertion — a config/server mismatch must not be silent."""
        server = FakeMCPSession(tools=["patch_ticket", "get_profile"])
        client = client_for(server, settings)
        with pytest.raises(MCPToolMissing) as excinfo:
            client.ensure_ready(["post_patch_crm"])
        message = str(excinfo.value)
        assert "post_patch_crm" in message          # what we wanted
        assert "patch_ticket" in message            # what it actually has
        assert "LQABR_SUMMARY_MCP_TOOL" in message  # how to fix it, without code

    def test_a_renamed_server_works_once_config_points_at_it(self):
        """The flexibility promise, end to end."""
        server = FakeMCPSession(tools=["patch_ticket"], tool_results={"patch_ticket": {"ok": True}})
        settings = Settings(mcp_base_url="http://mcp.local/mcp", mcp_tool_write="patch_ticket",
                            mcp_tool_read="patch_ticket")
        client = client_for(server, settings)
        client.ensure_ready([settings.mcp_tool_write])
        assert client.call_tool("patch_ticket", {"object_id": "1"}) == {"ok": True}

    def test_assertion_can_be_switched_off_for_offline_dev(self):
        server = FakeMCPSession(tools=[])
        settings = Settings(mcp_base_url="http://mcp.local/mcp", mcp_assert_tools=False)
        assert client_for(server, settings).ensure_ready(["post_patch_crm"]) == []


class TestCalls:
    def test_content_blocks_are_unwrapped_to_the_value(self, server, settings):
        server.tool_results["post_patch_crm"] = {"status": "written", "object_id": "42"}
        result = client_for(server, settings).call_tool("post_patch_crm", {"object_id": "42"})
        assert result == {"status": "written", "object_id": "42"}

    def test_arguments_reach_the_server_unchanged(self, server, settings):
        client_for(server, settings).call_tool(
            "post_patch_crm", {"object_id": "42", "properties": {"blog_summary": "text"}})
        params = server.tool_calls[0]["params"]
        assert params["name"] == "post_patch_crm"
        assert params["arguments"]["properties"] == {"blog_summary": "text"}

    def test_is_error_result_raises_rather_than_reading_as_success(self, server, settings):
        with pytest.raises(MCPError, match="unknown tool"):
            client_for(server, settings).call_tool("no_such_tool", {})

    def test_a_dropped_session_is_re_initialized_once(self, server, settings):
        """Cloud Run scales the MCP to zero; a 404 is normal, not fatal."""
        client = client_for(server, settings)
        client.list_tools()
        server.status_queue = [404]
        result = client.call_tool("post_patch_crm", {"object_id": "1"})
        assert server.initialize_count == 2
        assert result == {"status": "written"}

    def test_503_is_retried(self, server, settings):
        client = client_for(server, settings)
        server.status_queue = [503]
        assert client.call_tool("post_patch_crm", {"object_id": "1"}) == {"status": "written"}

    def test_persistent_500_gives_up_with_a_reason(self, server, settings):
        server.status_queue = [500, 500, 500]
        with pytest.raises(MCPError, match="HTTP 500"):
            client_for(server, settings).call_tool("post_patch_crm", {"object_id": "1"})


class TestUnwrap:
    def test_sse_transport_is_understood(self, settings):
        """A server that answers with an SSE frame is not a failure."""
        class SSESession:
            def request(self, method, url, headers=None, json=None, timeout=None):
                if json.get("method") == "notifications/initialized":
                    return FakeResponse(status_code=202, text="")
                body = '{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"post_patch_crm"}]}}'
                return FakeResponse(status_code=200,
                                    text=f"event: message\ndata: {body}\n\n",
                                    headers={"Content-Type": "text/event-stream"})

        assert MCPClient(settings, session=SSESession()).list_tools() == ["post_patch_crm"]

    @pytest.mark.parametrize("result,expected", [
        ({"content": [{"type": "text", "text": '{"a": 1}'}]}, {"a": 1}),
        ({"content": [{"type": "text", "text": "plain words"}]}, "plain words"),
        ({"structuredContent": {"a": 1}}, {"a": 1}),
        ({"status": "written"}, {"status": "written"}),
        ("already a string", "already a string"),
    ])
    def test_shapes(self, result, expected):
        assert unwrap_result(result) == expected
