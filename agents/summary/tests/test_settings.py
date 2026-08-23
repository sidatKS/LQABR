"""Settings: defaults, overrides, and the promise that nothing is hard-coded."""

from __future__ import annotations

import pytest

from summary_core.settings import Settings, get_settings


def test_defaults_match_the_documented_env_example():
    settings = Settings.from_env()
    assert settings.model == "anthropic/claude-sonnet-5"
    assert settings.mcp_base_url == "http://localhost:8080/mcp"
    assert settings.mcp_tool_write == "post_patch_crm"
    assert settings.hubspot_summary_property == "blog_summary"
    assert settings.hubspot_object_type == "ticket"
    assert settings.max_chars == 50_000
    assert settings.dry_run is False


@pytest.mark.parametrize(
    "env_var,attribute,value,expected",
    [
        ("LQABR_SUMMARY_MCP_TOOL_WRITE", "mcp_tool_write", "patch_ticket", "patch_ticket"),
        ("LQABR_SUMMARY_MCP_TOOL_READ", "mcp_tool_read", "get_profile", "get_profile"),
        ("LQABR_SUMMARY_HUBSPOT_SUMMARY_PROPERTY", "hubspot_summary_property", "post_summary", "post_summary"),
        ("LQABR_SUMMARY_HUBSPOT_OBJECT_TYPE", "hubspot_object_type", "contact", "contact"),
        ("LQABR_SUMMARY_MODEL", "model", "gemini/gemini-2.0-flash", "gemini/gemini-2.0-flash"),
        ("LQABR_SUMMARY_MAX_CHARS", "max_chars", "1200", 1200),
        ("LQABR_SUMMARY_DRY_RUN", "dry_run", "1", True),
        ("LQABR_SUMMARY_ROUTES", "routes", "API", "api"),
    ],
)
def test_every_external_name_is_overridable(monkeypatch, env_var, attribute, value, expected):
    """The flexibility promise: renaming a tool or a property is config."""
    monkeypatch.setenv(env_var, value)
    assert getattr(Settings.from_env(), attribute) == expected


def test_bad_integer_fails_loudly_and_names_the_variable(monkeypatch):
    monkeypatch.setenv("LQABR_SUMMARY_MAX_CHARS", "lots")
    with pytest.raises(ValueError, match="LQABR_SUMMARY_MAX_CHARS"):
        Settings.from_env()


def test_route_flags():
    assert Settings(routes="all", enable_agui=True).serves_api
    assert Settings(routes="all", enable_agui=True).serves_chat
    assert not Settings(routes="api", enable_agui=True).serves_chat
    assert not Settings(routes="all", enable_agui=False).serves_chat
    assert not Settings(routes="chat").serves_api


def test_redacted_never_carries_the_token():
    settings = Settings(mcp_auth_token="super-secret")
    dumped = settings.redacted()
    assert dumped["mcp_auth_token"] == "set"
    assert "super-secret" not in str(dumped)


def test_get_settings_is_cached_until_refreshed(monkeypatch):
    first = get_settings(refresh=True)
    monkeypatch.setenv("LQABR_SUMMARY_MODEL", "anthropic/claude-haiku-5")
    assert get_settings().model == first.model
    assert get_settings(refresh=True).model == "anthropic/claude-haiku-5"
