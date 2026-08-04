"""Shared fixtures for lqabr_core.leadgen tests. No test touches the real HubSpot API."""

from __future__ import annotations

import json

import pytest

from lqabr_core.obs import Observability, RunContext, reset_obs, set_current_run, set_obs
from lqabr_core.leadgen.secrets import reset_resolver
from lqabr_core.leadgen.hubspot import auth as auth_module
from lqabr_core.leadgen.hubspot import crm as crm_module


# --------------------------------------------------------------------------
# fake HubSpot
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeHubSpot:
    """A tiny stateful stand-in for HubSpot CRM.

    Records every call so tests can assert on PATCH-vs-POST and association.
    """

    def __init__(self):
        self.contacts: dict[str, dict] = {}   # hs_id -> properties
        self.companies: dict[str, dict] = {}
        self.associations: list[tuple[str, str]] = []
        self.calls: list[tuple[str, str]] = []
        self._next_id = 1000
        self.reject_company_400 = False
        self.reject_contact_400 = False
        self.fail_search_times = 0

    def _new_id(self) -> str:
        self._next_id += 1
        return str(self._next_id)

    def request(self, method, url, headers=None, json=None, params=None, timeout=None):
        path = url.split("hubapi.com", 1)[-1] if "hubapi.com" in url else url
        for prefix in ("http://testserver", "https://testserver"):
            if path.startswith(prefix):
                path = path[len(prefix):]
        self.calls.append((method, path))

        # --- search ---
        if method == "POST" and path.endswith("/search"):
            if self.fail_search_times > 0:
                self.fail_search_times -= 1
                return FakeResponse(503, {"message": "throttled"})
            object_type = "contacts" if "/contacts/" in path else "companies"
            store = self.contacts if object_type == "contacts" else self.companies
            flt = json["filterGroups"][0]["filters"][0]
            prop, value = flt["propertyName"], flt["value"]
            limit = json.get("limit", 1)
            # HubSpot enforces no uniqueness on a custom text property, so the
            # fake must be able to return more than one match — that is exactly
            # the duplicate-dedup-key case the caller has to notice.
            matches = [
                {"id": hs_id, "properties": props}
                for hs_id, props in store.items()
                if str(props.get(prop)) == str(value)
            ]
            return FakeResponse(200, {"results": matches[:limit]})

        # --- create ---
        if method == "POST" and path in ("/crm/v3/objects/contacts", "/crm/v3/objects/companies"):
            is_contact = path.endswith("contacts")
            if is_contact and self.reject_contact_400:
                return FakeResponse(400, {"message": "invalid contact property"})
            if not is_contact and self.reject_company_400:
                return FakeResponse(400, {"message": "PROPERTY_DOESNT_EXIST: invalid industry option"})
            hs_id = self._new_id()
            (self.contacts if is_contact else self.companies)[hs_id] = dict(json["properties"])
            return FakeResponse(201, {"id": hs_id, "properties": json["properties"]})

        # --- update ---
        if method == "PATCH" and path.startswith("/crm/v3/objects/"):
            is_contact = "/contacts/" in path
            if not is_contact and self.reject_company_400:
                return FakeResponse(400, {"message": "invalid industry option"})
            hs_id = path.rsplit("/", 1)[-1]
            store = self.contacts if is_contact else self.companies
            store.setdefault(hs_id, {}).update(json["properties"])
            return FakeResponse(200, {"id": hs_id, "properties": store[hs_id]})

        # --- associate ---
        if method == "PUT" and "/associations/default/companies/" in path:
            parts = path.split("/")  # ['', 'crm', 'v4', 'objects', 'contacts', '<id>', ...]
            contact_id, company_id = parts[5], parts[-1]
            self.associations.append((contact_id, company_id))
            return FakeResponse(200, {"status": "COMPLETE"})

        # --- read associations ---
        if method == "GET" and path.endswith("/associations/companies"):
            contact_id = path.split("/")[5]
            for c_id, co_id in self.associations:
                if c_id == contact_id:
                    return FakeResponse(200, {"results": [{"toObjectId": co_id}]})
            return FakeResponse(200, {"results": []})

        # --- read company ---
        if method == "GET" and path.startswith("/crm/v3/objects/companies/"):
            hs_id = path.rsplit("/", 1)[-1]
            if hs_id in self.companies:
                return FakeResponse(200, {"id": hs_id, "properties": self.companies[hs_id]})
            return FakeResponse(404, {"message": "not found"})

        return FakeResponse(404, {"message": f"unhandled {method} {path}"})


class StubTokenProvider:
    mode = "stub"

    def __init__(self):
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return auth_module.AccessToken(value=f"stub-token-{self.calls}", expires_at=None, mode=self.mode)


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _obs(tmp_path, monkeypatch):
    """Fresh run context + obs per test; error file redirected to tmp."""
    monkeypatch.setenv("LQABR_ERRORS_DIR", str(tmp_path / "errors"))
    monkeypatch.setenv("LQABR_LOG_TO_FILE", "false")
    monkeypatch.setenv("HUBSPOT_AUTH_MODE", "private_app")
    monkeypatch.setenv("HUBSPOT_PRIVATE_APP_TOKEN", "test-token")
    # Secrets: the env backend is the documented local/CI path. Production
    # defaults to the Secret Manager API and no test may reach it.
    monkeypatch.setenv("LQABR_SECRET_BACKEND", "env")
    for name in (
        "LQABR_SECRET_PROJECT",
        "LQABR_SECRET_TTL_SECONDS",
        "LQABR_SECRET_ANTHROPIC_API_KEY",
        "LQABR_SECRET_HUBSPOT_PRIVATE_APP_TOKEN",
        "LQABR_SECRET_HUBSPOT_CLIENT_ID",
        "LQABR_SECRET_HUBSPOT_CLIENT_SECRET",
        "LQABR_SECRET_HUBSPOT_REFRESH_TOKEN",
        "LQABR_ORCHESTRATOR",
        "LQABR_INCOMING_DIR",
        "LQABR_CONSECUTIVE_TRANSPORT_LIMIT",
        "ANTHROPIC_API_KEY",
        "GOOGLE_CLOUD_PROJECT",
    ):
        monkeypatch.delenv(name, raising=False)
    ctx = RunContext()
    set_current_run(ctx)
    obs = set_obs(Observability(ctx))
    reset_resolver()
    yield obs
    reset_resolver()
    reset_obs()


@pytest.fixture
def fake_hubspot(monkeypatch):
    fake = FakeHubSpot()
    auth_module.reset_token_cache(provider=StubTokenProvider())
    http = crm_module.HubSpotHttp(
        base_url="https://testserver", timeout=5, max_retries=2, session=fake
    )
    crm_module.set_http(http)
    yield fake
    crm_module.set_http(None)
    auth_module.reset_token_cache()


@pytest.fixture
def errors_file(tmp_path):
    return tmp_path / "errors" / "schema_mismatch.jsonl"
