"""STEP A — get_hubspot_token(): M2M auth utility."""

from __future__ import annotations

import time

import pytest

from lqabr_core.leadgen.hubspot import auth as auth_module
from lqabr_core.leadgen.hubspot.auth import (
    AuthConfigError,
    PrivateAppTokenProvider,
    RefreshTokenProvider,
    TokenCache,
    TokenError,
    build_provider,
    get_auth_header,
    get_hubspot_token,
    reset_token_cache,
)


class FakeTokenResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.posts = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append({"url": url, "data": data})
        return self.payloads.pop(0)


# --- mode selection --------------------------------------------------------


def test_private_app_mode_is_selected_from_env(monkeypatch):
    monkeypatch.setenv("HUBSPOT_AUTH_MODE", "private_app")
    assert isinstance(build_provider(), PrivateAppTokenProvider)


def test_refresh_token_mode_is_selected_from_env(monkeypatch):
    monkeypatch.setenv("HUBSPOT_AUTH_MODE", "refresh_token")
    assert isinstance(build_provider(), RefreshTokenProvider)


def test_client_credentials_is_refused_with_an_explanation():
    with pytest.raises(AuthConfigError) as exc:
        build_provider("client_credentials")
    assert "webhooks-journal" in str(exc.value)


def test_unknown_mode_is_refused():
    with pytest.raises(AuthConfigError):
        build_provider("magic")


# --- private app (interim) -------------------------------------------------


def test_private_app_returns_the_static_token(monkeypatch):
    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "pat-123")
    token = PrivateAppTokenProvider().fetch()
    assert token.value == "pat-123"
    assert token.expires_at is None
    assert token.is_fresh(skew_seconds=120)


def test_private_app_without_a_token_is_a_config_error(monkeypatch):
    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "")
    with pytest.raises(AuthConfigError):
        PrivateAppTokenProvider().fetch()


# --- refresh token (target) ------------------------------------------------


def _refresh_provider(session, **kwargs):
    return RefreshTokenProvider(
        client_id="cid",
        client_secret="secret",
        refresh_token="rt",
        token_url="https://token.test/oauth/v1/token",
        session=session,
        **kwargs,
    )


def test_refresh_token_mints_a_short_lived_token():
    session = FakeSession([FakeTokenResponse(200, {"access_token": "at-1", "expires_in": 1800})])
    token = _refresh_provider(session).fetch()
    assert token.value == "at-1"
    assert 1700 < (token.expires_at - time.time()) <= 1800
    assert session.posts[0]["data"]["grant_type"] == "refresh_token"


def test_refresh_token_never_sends_or_stores_an_access_token():
    session = FakeSession([FakeTokenResponse(200, {"access_token": "at-1", "expires_in": 60})])
    _refresh_provider(session).fetch()
    sent = session.posts[0]["data"]
    assert set(sent) == {"grant_type", "client_id", "client_secret", "refresh_token"}


def test_missing_credentials_is_a_config_error(monkeypatch):
    for name in ("HUBSPOT_CLIENT_ID", "HUBSPOT_CLIENT_SECRET", "HUBSPOT_REFRESH_TOKEN"):
        monkeypatch.setenv(name, "")
    with pytest.raises(AuthConfigError) as exc:
        RefreshTokenProvider().fetch()
    assert "HUBSPOT_CLIENT_ID" in str(exc.value)


def test_token_endpoint_failure_raises():
    session = FakeSession([FakeTokenResponse(401, {"message": "bad refresh token"})])
    with pytest.raises(TokenError):
        _refresh_provider(session).fetch()


# --- caching + auto-refresh ------------------------------------------------


class CountingProvider:
    mode = "counting"

    def __init__(self, ttl_seconds):
        self.ttl = ttl_seconds
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return auth_module.AccessToken(
            value=f"tok-{self.calls}", expires_at=time.time() + self.ttl, mode=self.mode
        )


def test_a_fresh_token_is_reused_from_cache():
    provider = CountingProvider(ttl_seconds=1800)
    cache = TokenCache(provider=provider, skew_seconds=120)
    assert cache.get().value == "tok-1"
    assert cache.get().value == "tok-1"
    assert provider.calls == 1


def test_a_token_inside_the_skew_window_is_refreshed():
    provider = CountingProvider(ttl_seconds=30)  # 30s left, skew is 120s
    cache = TokenCache(provider=provider, skew_seconds=120)
    assert cache.get().value == "tok-1"
    assert cache.get().value == "tok-2"
    assert provider.calls == 2


def test_force_refresh_bypasses_the_cache():
    provider = CountingProvider(ttl_seconds=1800)
    cache = TokenCache(provider=provider, skew_seconds=120)
    cache.get()
    assert cache.get(force_refresh=True).value == "tok-2"


def test_get_auth_header_is_a_bearer_header(monkeypatch):
    monkeypatch.setenv("HUBSPOT_AUTH_MODE", "private_app")
    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "pat-xyz")
    reset_token_cache()
    header = get_auth_header()
    assert header["Authorization"] == "Bearer pat-xyz"
    assert header["Content-Type"] == "application/json"
    assert get_hubspot_token() == "pat-xyz"
    reset_token_cache()
