"""STEP 4 — the machine-to-machine bearer."""

import pytest

from mcp_fakes import FakeResponse, FakeSession, RecordingObs
from mcp.hubspot.auth import (
    OAuth2ClientCredentialsProvider,
    RunTokenCache,
    SecretManagerTokenProvider,
    TokenError,
    build_provider,
)


class StubProvider:
    def __init__(self, token="tok-1", ttl=3600):
        self.token, self.ttl, self.calls = token, ttl, 0

    name = "stub"

    def fetch(self):
        self.calls += 1
        return f"{self.token}-{self.calls}", self.ttl


def test_secret_manager_is_the_default_backend(monkeypatch):
    monkeypatch.delenv("LQABR_HUBSPOT_AUTH_MODE", raising=False)
    assert isinstance(build_provider(), SecretManagerTokenProvider)


def test_backend_selection_is_config_not_code(monkeypatch):
    monkeypatch.setenv("LQABR_HUBSPOT_AUTH_MODE", "oauth2")
    monkeypatch.setenv("LQABR_HUBSPOT_TOKEN_URL", "https://auth.example/token")
    monkeypatch.setenv("LQABR_HUBSPOT_CLIENT_ID", "client-1")
    assert isinstance(build_provider(), OAuth2ClientCredentialsProvider)


def test_an_unknown_mode_fails_loudly(monkeypatch):
    monkeypatch.setenv("LQABR_HUBSPOT_AUTH_MODE", "whatever")
    with pytest.raises(TokenError):
        build_provider()


def test_secret_manager_backend_reads_the_provisioned_secret(monkeypatch):
    monkeypatch.setenv("LQABR_HUBSPOT_ACCESS_TOKEN", "pat-from-secret-manager")
    token, ttl = SecretManagerTokenProvider().fetch()
    assert token == "pat-from-secret-manager"
    assert ttl > 0


def test_a_missing_credential_is_a_token_error_not_an_empty_bearer(monkeypatch):
    monkeypatch.delenv("LQABR_HUBSPOT_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(TokenError):
        SecretManagerTokenProvider().fetch()


def test_the_bearer_is_acquired_once_and_cached_for_the_run():
    provider = StubProvider()
    cache = RunTokenCache(provider=provider)
    assert cache.get() == cache.get() == "tok-1-1"
    assert provider.calls == 1


@pytest.fixture
def clock(monkeypatch):
    """A movable clock, so expiry is tested without sleeping."""
    from mcp.hubspot import auth

    state = {"now": 1_000_000.0}
    monkeypatch.setattr(auth.time, "time", lambda: state["now"])
    return state


def test_the_bearer_is_refreshed_on_expiry(clock):
    provider = StubProvider(ttl=300)
    cache = RunTokenCache(provider=provider)
    first = cache.get()
    clock["now"] += 600          # past the TTL
    second = cache.get()
    assert provider.calls == 2 and first != second


def test_invalidate_forces_a_re_acquire_after_a_401():
    provider = StubProvider()
    cache = RunTokenCache(provider=provider)
    cache.get()
    cache.invalidate()
    cache.get()
    assert provider.calls == 2


def test_the_token_call_is_audited_and_the_value_is_never_in_the_record():
    obs = RecordingObs()
    RunTokenCache(provider=StubProvider(token="super-secret"), obs=obs).get()

    assert obs.audits[0]["step"] == 4
    assert obs.audits[0]["direction"] == "outbound"
    # the raw value is handed to the sink under `bearer`, which the agent's
    # observability fingerprints — it is never a plain field of its own
    assert "token" not in obs.audits[0]
    assert obs.processes[0]["event"] == "token_acquired"
    assert "super-secret" not in str(obs.processes[0])


def test_a_refresh_is_logged_as_a_refresh_not_a_first_acquire(clock):
    obs = RecordingObs()
    cache = RunTokenCache(provider=StubProvider(ttl=300), obs=obs)
    cache.get()
    clock["now"] += 600
    cache.get()
    assert [p["event"] for p in obs.processes] == ["token_acquired", "token_refreshed"]


def test_oauth2_backend_needs_its_config(monkeypatch):
    monkeypatch.delenv("LQABR_HUBSPOT_TOKEN_URL", raising=False)
    monkeypatch.delenv("LQABR_HUBSPOT_CLIENT_ID", raising=False)
    with pytest.raises(TokenError):
        OAuth2ClientCredentialsProvider()


def test_oauth2_backend_honours_expires_in(monkeypatch):
    monkeypatch.setenv("LQABR_HUBSPOT_CLIENT_SECRET", "shh")
    session = FakeSession([FakeResponse(200, {"access_token": "oauth-tok", "expires_in": 900})])
    provider = OAuth2ClientCredentialsProvider(
        token_url="https://auth.example/token", client_id="c1", session=session)
    assert provider.fetch() == ("oauth-tok", 900)
    assert session.calls[0]["data"]["grant_type"] == "client_credentials"


def test_oauth2_backend_surfaces_a_rejected_grant(monkeypatch):
    monkeypatch.setenv("LQABR_HUBSPOT_CLIENT_SECRET", "shh")
    session = FakeSession([FakeResponse(401, text="unauthorized_client")])
    provider = OAuth2ClientCredentialsProvider(
        token_url="https://auth.example/token", client_id="c1", session=session)
    with pytest.raises(TokenError):
        provider.fetch()
