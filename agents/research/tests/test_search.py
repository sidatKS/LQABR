"""The Anthropic search provider: block parsing, prefix handling, failures."""

from __future__ import annotations

import pytest

from research_core.search.anthropic_search import (
    AnthropicWebSearch, _strip_provider_prefix)
from research_core.search.base import SearchError
from research_core.settings import get_settings


class FakeMessages:
    def __init__(self, payload=None, raises=None):
        self.payload = payload
        self.raises = raises
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.raises:
            raise self.raises
        return self.payload


class FakeClient:
    def __init__(self, payload=None, raises=None):
        self.messages = FakeMessages(payload, raises)


RESPONSE = {"content": [
    {"type": "server_tool_use", "name": "web_search"},
    {"type": "web_search_tool_result",
     "content": [{"url": "https://a.example/1"}, {"url": "https://b.example/2"}]},
    {"type": "text", "text": "Axiom Law operates in regulated legal services.",
     "citations": [{"url": "https://a.example/1"}]},
]}


def test_strip_provider_prefix():
    assert _strip_provider_prefix("anthropic/claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert _strip_provider_prefix("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_collects_text_sources_and_search_count():
    client = FakeClient(RESPONSE)
    provider = AnthropicWebSearch(settings=get_settings(refresh=True), client=client)
    findings = provider.research("prompt", system="sys")
    assert "Axiom Law" in findings.text
    assert findings.sources == ["https://a.example/1", "https://b.example/2"]
    assert findings.searches == 1


def test_sends_the_web_search_tool_and_system():
    client = FakeClient(RESPONSE)
    provider = AnthropicWebSearch(settings=get_settings(refresh=True), client=client)
    provider.research("prompt", system="sys")
    kwargs = client.messages.kwargs
    assert kwargs["tools"][0]["name"] == "web_search"
    assert kwargs["system"] == "sys"
    assert kwargs["model"] == "claude-sonnet-4-6"


def test_allowed_domains_are_passed_through(monkeypatch):
    monkeypatch.setenv("LQABR_RESEARCH_SEARCH_ALLOWED_DOMAINS", "reuters.com,ft.com")
    client = FakeClient(RESPONSE)
    provider = AnthropicWebSearch(settings=get_settings(refresh=True), client=client)
    provider.research("prompt")
    assert client.messages.kwargs["tools"][0]["allowed_domains"] == ["reuters.com", "ft.com"]


def test_empty_answer_raises_rather_than_returning_nothing():
    provider = AnthropicWebSearch(settings=get_settings(refresh=True),
                                  client=FakeClient({"content": []}))
    with pytest.raises(SearchError):
        provider.research("prompt")


def test_provider_error_becomes_search_error():
    provider = AnthropicWebSearch(settings=get_settings(refresh=True),
                                  client=FakeClient(raises=RuntimeError("429 rate limit")))
    with pytest.raises(SearchError) as excinfo:
        provider.research("prompt")
    assert "429" in str(excinfo.value)


def test_missing_api_key_is_named(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = AnthropicWebSearch(settings=get_settings(refresh=True))
    with pytest.raises(SearchError) as excinfo:
        provider.research("prompt")
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)


# --- the model credential comes from Secret Manager, like HubSpot's ---------

def _provider(monkeypatch, **env):
    for key in ("ANTHROPIC_API_KEY", "LQABR_RESEARCH_SECRETS_SOURCE",
                "LQABR_RESEARCH_GCP_PROJECT", "LQABR_ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return AnthropicWebSearch(settings=get_settings(refresh=True))


def test_an_injected_key_wins(monkeypatch):
    """Tests must never reach a secret store."""
    provider = _provider(monkeypatch)
    provider._api_key = "injected"
    assert provider._resolve_key() == "injected"


def test_the_environment_still_overrides(monkeypatch):
    """ANTHROPIC_API_KEY is the SDK's own convention and the local override."""
    provider = _provider(monkeypatch, ANTHROPIC_API_KEY="from-env")
    assert provider._resolve_key() == "from-env"


def test_it_falls_back_to_the_named_secret(monkeypatch):
    """No env var: resolve by NAME, exactly like the HubSpot token."""
    provider = _provider(monkeypatch, LQABR_RESEARCH_SECRETS_SOURCE="env")
    # source=env with nothing set makes resolve_secret raise without any
    # network call — what matters is that the failure names the SECRET, which
    # proves the lookup was attempted rather than skipped.
    with pytest.raises(SearchError) as caught:
        provider._resolve_key()
    assert "lqabr-anthropic-api-key" in str(caught.value)


def test_a_missing_credential_is_a_search_error_not_a_crash(monkeypatch):
    """The caller reports it against the lead; it must not escape as SecretError."""
    provider = _provider(monkeypatch, LQABR_RESEARCH_SECRETS_SOURCE="env")
    with pytest.raises(SearchError):
        provider._resolve_key()


def test_the_secret_name_is_configurable(monkeypatch):
    provider = _provider(monkeypatch,
                         LQABR_RESEARCH_MODEL_TOKEN_SECRET="some-other-secret",
                         LQABR_RESEARCH_SECRETS_SOURCE="env")
    with pytest.raises(SearchError) as caught:
        provider._resolve_key()
    assert "some-other-secret" in str(caught.value)
