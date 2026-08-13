import os

import pytest

from lqabr_core.model import build_model


def test_gemini_model_passes_through_unwrapped():
    assert build_model("gemini-2.0-flash") == "gemini-2.0-flash"


def test_non_gemini_model_wraps_in_litellm(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-already-set")
    result = build_model("anthropic/claude-3-5-sonnet-20241022")
    from google.adk.models.lite_llm import LiteLlm
    assert isinstance(result, LiteLlm)


def test_bridges_provider_secret_when_env_var_missing(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("lqabr_core.model.get_secret", lambda name: "fetched-from-secret-manager")
    build_model("anthropic/claude-3-5-sonnet-20241022")
    assert os.environ["ANTHROPIC_API_KEY"] == "fetched-from-secret-manager"


def test_does_not_overwrite_existing_provider_env_var(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "already-here")

    def _boom(name):
        raise AssertionError("should not fetch when env var already set")

    monkeypatch.setattr("lqabr_core.model.get_secret", _boom)
    build_model("anthropic/claude-3-5-sonnet-20241022")
    assert os.environ["ANTHROPIC_API_KEY"] == "already-here"


def test_missing_provider_secret_does_not_crash(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from lqabr_core.secrets import SecretNotFoundError

    def _not_found(name):
        raise SecretNotFoundError("no secret here")

    monkeypatch.setattr("lqabr_core.model.get_secret", _not_found)
    # should not raise — LiteLlm/the provider SDK is left to complain instead
    build_model("anthropic/claude-3-5-sonnet-20241022")
