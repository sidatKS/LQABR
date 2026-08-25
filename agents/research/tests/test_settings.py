"""Settings: the config map is read, and the environment beats it."""

from __future__ import annotations

from research_core.settings import get_settings


def test_config_map_supplies_defaults():
    s = get_settings(refresh=True)
    assert s.mcp_base_url == "http://localhost:8091/mcp"
    assert s.mcp_tool_read_lead == "get_lead_profile"
    assert s.mcp_tool_read_blog == "get_blog_summary"
    assert s.mcp_tool_write == "upsert_lead_profile"
    assert s.hubspot_context_property == "lead_context"
    assert s.model_token_secret == "lqabr-anthropic-api-key"


def test_environment_overrides_config_map(monkeypatch):
    monkeypatch.setenv("LQABR_RESEARCH_MCP_BASE_URL", "http://elsewhere:9/mcp")
    monkeypatch.setenv("LQABR_RESEARCH_HUBSPOT_CONTEXT_PROPERTY", "custom_context")
    monkeypatch.setenv("LQABR_RESEARCH_DRY_RUN", "1")
    s = get_settings(refresh=True)
    assert s.mcp_base_url == "http://elsewhere:9/mcp"
    assert s.hubspot_context_property == "custom_context"
    assert s.dry_run is True


def test_retryable_statuses_are_ints():
    s = get_settings(refresh=True)
    assert all(isinstance(code, int) for code in s.mcp_retryable_statuses)
    assert 429 in s.mcp_retryable_statuses


def test_redacted_never_leaks_the_token(monkeypatch):
    monkeypatch.setenv("LQABR_RESEARCH_MCP_AUTH_TOKEN", "super-secret-value")
    data = get_settings(refresh=True).redacted()
    assert data["mcp_auth_token"] == "set"
    assert "super-secret-value" not in str(data)


def test_relative_log_path_resolves_against_repo_root(monkeypatch):
    monkeypatch.setenv("LQABR_RESEARCH_LOG_FILE", "logs/agents/research/agent.log")
    s = get_settings(refresh=True)
    assert s.log_file.endswith("logs/agents/research/agent.log")
    assert s.log_file.startswith("/")


def test_empty_log_file_disables_file_logging(monkeypatch):
    monkeypatch.setenv("LQABR_RESEARCH_LOG_FILE", "")
    assert get_settings(refresh=True).log_file == ""
