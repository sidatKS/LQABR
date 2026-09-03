"""The MCP wants an IAM token; this is where it comes from.

`lqabr-dev-mcp` runs with Require-authentication, so an anonymous call is
refused by Cloud Run with HTTP 403 before the container sees it — the exact
failure that took a research campaign down on 2026-09-01. These tests pin the
header being there, the cache, and the two ways it is legitimately absent.

Offline: the metadata server is a stub. Nothing here touches the network.
"""

from __future__ import annotations

import base64
import json
import time

import pytest

from research_core.mcp.identity import (IDENTITY_URL, IdentityTokenSource,
                                        audience_for)


def _jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(exp)}).encode("utf-8")).decode("utf-8").rstrip("=")
    return f"header.{payload}.signature"


class _Response:
    def __init__(self, text: str, status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code


class _Metadata:
    """A stand-in metadata server that counts how often it was asked."""

    def __init__(self, response=None, raises: Exception | None = None) -> None:
        self._response = response
        self._raises = raises
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}})
        if self._raises is not None:
            raise self._raises
        return self._response


# ----------------------------------------------------------------- audience
@pytest.mark.parametrize("base_url,expected", [
    ("https://lqabr-dev-mcp-432617526728.us-central1.run.app/mcp",
     "https://lqabr-dev-mcp-432617526728.us-central1.run.app"),
    ("https://host.run.app", "https://host.run.app"),
    ("http://localhost:8091/mcp", "http://localhost:8091"),
    ("", ""),
    ("not-a-url", ""),
])
def test_the_audience_is_the_service_url_without_the_path(base_url, expected):
    """Cloud Run validates against the service URL. `/mcp` in the audience
    does not match, and the resulting 403 is indistinguishable from no token."""
    assert audience_for(base_url) == expected


# -------------------------------------------------------------------- mint
def test_a_token_is_minted_for_the_configured_audience():
    server = _Metadata(_Response(_jwt(time.time() + 3600)))
    source = IdentityTokenSource("https://mcp.example", session=server)

    assert source.token().startswith("header.")
    assert server.calls[0]["url"] == IDENTITY_URL
    assert server.calls[0]["params"] == {"audience": "https://mcp.example"}
    assert server.calls[0]["headers"] == {"Metadata-Flavor": "Google"}


def test_a_live_token_is_reused_rather_than_reminted():
    server = _Metadata(_Response(_jwt(time.time() + 3600)))
    source = IdentityTokenSource("https://mcp.example", session=server)

    first = source.token()
    assert source.token() == first
    assert source.token() == first
    assert len(server.calls) == 1


def test_a_token_inside_the_skew_window_is_replaced():
    """Expiry is read from `exp`; one about to lapse must not be handed out."""
    server = _Metadata(_Response(_jwt(time.time() + 60)))   # inside the 300s skew
    source = IdentityTokenSource("https://mcp.example", session=server)

    source.token()
    source.token()
    assert len(server.calls) == 2


# ------------------------------------------------------- absence, on purpose
def test_no_audience_means_no_call_and_no_token():
    server = _Metadata(_Response(_jwt(time.time() + 3600)))
    assert IdentityTokenSource("", session=server).token() == ""
    assert server.calls == []


def test_an_unreachable_metadata_server_is_empty_not_an_exception():
    """Off Cloud Run there is no metadata host. The client must still run —
    against a local MCP that wants no credential at all."""
    server = _Metadata(raises=OSError("Name or service not known"))
    source = IdentityTokenSource("https://mcp.example", session=server)

    assert source.token() == ""


def test_a_failed_mint_is_not_retried_on_every_call():
    """A DNS timeout per MCP request would be paid on every single call."""
    server = _Metadata(raises=OSError("no metadata server"))
    source = IdentityTokenSource("https://mcp.example", session=server)

    source.token()
    source.token()
    source.token()
    assert len(server.calls) == 1


def test_a_non_200_from_the_metadata_server_yields_no_token():
    server = _Metadata(_Response("nope", status_code=404))
    assert IdentityTokenSource("https://mcp.example", session=server).token() == ""


# ------------------------------------------------------------ never the value
def test_the_token_value_is_never_logged():
    """`Log the credential's name, never its value` — the project rule."""
    emitted: list[tuple[str, dict]] = []

    class _Stream:
        def emit(self, event, **fields):
            emitted.append((event, fields))

    class _Obs:
        system = _Stream()

    token = _jwt(time.time() + 3600)
    source = IdentityTokenSource("https://mcp.example",
                                 session=_Metadata(_Response(token)), obs=_Obs())
    source.token()

    assert emitted, "minting a credential is worth one line"
    for event, fields in emitted:
        assert token not in json.dumps(fields)
        assert "header." not in json.dumps(fields)


# --------------------------------------------------- the header on the wire
def test_the_client_sends_the_minted_token_as_a_bearer_header(monkeypatch):
    """The whole point: an MCP request must carry Authorization, or Cloud Run
    answers 403 and the campaign dies at read_blog."""
    from research_core.mcp.client import MCPClient
    from research_core.settings import get_settings

    monkeypatch.setenv("LQABR_RESEARCH_MCP_BASE_URL",
                       "https://lqabr-dev-mcp-1.us-central1.run.app/mcp")
    client = MCPClient(settings=get_settings(refresh=True))
    token = _jwt(time.time() + 3600)
    client._identity._session = _Metadata(_Response(token))   # noqa: SLF001

    headers = client._headers()                               # noqa: SLF001
    assert headers["Authorization"] == f"Bearer {token}"
    assert client._identity.audience == \
        "https://lqabr-dev-mcp-1.us-central1.run.app"         # noqa: SLF001


def test_an_explicit_auth_token_wins_over_the_minted_one(monkeypatch):
    """A hand-set token is how a local or stand-in MCP is reached; it must not
    be silently replaced by whatever the metadata server hands back."""
    from research_core.mcp.client import MCPClient
    from research_core.settings import get_settings

    monkeypatch.setenv("LQABR_RESEARCH_MCP_BASE_URL", "https://mcp.example/mcp")
    monkeypatch.setenv("LQABR_RESEARCH_MCP_AUTH_TOKEN", "hand-set")
    client = MCPClient(settings=get_settings(refresh=True))
    client._identity._session = _Metadata(_Response(_jwt(time.time() + 3600)))  # noqa: SLF001

    assert client._headers()["Authorization"] == "Bearer hand-set"  # noqa: SLF001


def test_no_credential_anywhere_means_no_authorization_header(monkeypatch):
    """A local MCP wants none, and a missing header must not become the string
    'Bearer ' — which would be a 401 that looks like a bad token."""
    from research_core.mcp.client import MCPClient
    from research_core.settings import get_settings

    monkeypatch.setenv("LQABR_RESEARCH_MCP_BASE_URL", "http://localhost:8091/mcp")
    client = MCPClient(settings=get_settings(refresh=True))
    client._identity._session = _Metadata(raises=OSError("no metadata"))  # noqa: SLF001

    assert "Authorization" not in client._headers()          # noqa: SLF001
