"""Shared fixtures for the lead_profile agent tests. No test touches the real HubSpot API."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# This agent's src/ is imported by bare module name (agent, tools, ...). Those
# names collide across the six agents when the WHOLE repo runs in one pytest
# process: whichever agent's src/ was inserted last shadows the others, and
# sys.modules caches the loser. Guard: evict modules loaded from a sibling
# agent's src/ (already-collected sibling tests keep their references) and put
# THIS agent's src/ at the front, so bare imports below always resolve locally.
# A no-op when the agent's tests run alone.
SRC = Path(__file__).resolve().parents[1] / "src"
_AGENTS_DIR = SRC.parents[1]
_OWN_MODULES: dict = {}


def _module_src_dir(mod):
    file = getattr(mod, "__file__", None)
    if not file:
        return None
    path = Path(file)
    if _AGENTS_DIR not in path.parents:
        return None
    rel = path.relative_to(_AGENTS_DIR).parts
    if len(rel) >= 2 and rel[1] == "src":
        return _AGENTS_DIR / rel[0] / "src"
    return None


def _activate_src() -> None:
    """Make bare imports (agent, tools, ...) resolve to THIS agent's src/,
    preserving module identity across re-activations.

    1. Capture every module already loaded from our src/ (so a class imported
       at collection time stays the SAME object at run time).
    2. Evict modules loaded from a sibling agent's src/ — their bare names
       (agent, webhook_app, ...) collide with ours. The sibling's own
       activation restores its captured modules before its tests run.
    3. Restore our captured modules and put our src/ at the path front.
    """
    for name, mod in list(sys.modules.items()):
        if _module_src_dir(mod) == SRC:
            _OWN_MODULES[name] = mod
    for name in [n for n, m in list(sys.modules.items()) if _module_src_dir(m) not in (None, SRC)]:
        del sys.modules[name]
    sys.modules.update(_OWN_MODULES)
    while str(SRC) in sys.path:
        sys.path.remove(str(SRC))
    sys.path.insert(0, str(SRC))


# Conftest import time: needed for this conftest's own bare imports below, and
# sufficient when this agent's tests run alone.
_activate_src()


# Test-module import time: in a whole-repo run every conftest has already
# executed before ANY test module imports, so whichever src/ activated last
# would win. Re-activate ours just before each of our modules is imported.
def pytest_collectstart(collector):
    # Fires immediately before a collector runs — for a Module, that is the
    # moment the test file is imported (pytest may build all Module nodes
    # first and import them in a later pass, so pytest_pycollect_makemodule
    # is too early). Not directory-scoped: every conftest's impl runs for
    # every collector, so act only on modules in this directory.
    path = getattr(collector, "path", None)
    if (
        path is not None
        and Path(str(path)).suffix == ".py"
        and Path(str(path)).is_relative_to(Path(__file__).parent)
    ):
        _activate_src()


def pytest_collection_finish(session):
    # Every test module is imported by the end of collection. Capture our
    # loaded modules NOW, before any sibling agent's run-phase activation
    # evicts them — restoring these same objects preserves class identity
    # for isinstance checks against classes bound at collection time.
    for _name, _mod in list(sys.modules.items()):
        if _module_src_dir(_mod) == SRC:
            _OWN_MODULES[_name] = _mod


# Test RUN time: a test body may import bare names lazily (the discovery
# tests do `import agent` inside the test), and sibling collections may have
# evicted our modules since. Re-activate around every test here.
@pytest.fixture(autouse=True)
def _agent_src_active():
    _activate_src()
    yield

import json

import pytest

from lqabr_core.obs import Observability, RunContext, reset_obs, set_current_run, set_obs
from lqabr_core.leadgen.secrets import reset_resolver
from lqabr_core.leadgen.hubspot import auth as auth_module
from lqabr_core.leadgen.hubspot import crm as crm_module

from load_csv import COMPANIES_FILE, CONTACTS_FILE, EMPLOYEES_FILE


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


@pytest.fixture
def fake_response_cls():
    """Hand tests the FakeResponse class via a fixture.

    `from conftest import FakeResponse` inside a test body is unsafe in a
    whole-repo run: several agents' test dirs each have a bare-named
    `conftest` module, and sys.modules holds only one of them.
    """
    return FakeResponse


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
    # HERMETICITY: agent.py loads the developer's real .env at import time (adk
    # needs the orchestrator choice before root_agent is built). Whatever that
    # file contains must never leak into a test — it made the suite pass on one
    # machine and fail on another (31 Jul).
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
def seed_dir(tmp_path: Path) -> Path:
    """Three small CSVs exercising every join outcome.

    5 employees: 3 decision-makers that fully resolve, 1 decision-maker whose
    company is missing (-> unresolved), 1 non-decision-maker (-> filtered out).
    """
    directory = tmp_path / "incoming"
    directory.mkdir()

    (directory / EMPLOYEES_FILE).write_text(
        "Employee_ID,Company_ID,Job_Title,Decision_Maker_Flag\n"
        "E1,C1,Head of Ops,Yes\n"
        "E2,C1,VP Sales,yes\n"
        "E3,C2,Director,YES\n"
        "E4,C9,Chief,Yes\n"
        "E5,C1,Analyst,No\n",
        encoding="utf-8",
    )
    (directory / CONTACTS_FILE).write_text(
        "Employee_ID,Company_ID,Job_Title,Email,Phone\n"
        "E1,C1,Head of Ops,lead@example.com,555-0001\n"
        "E2,C1,VP Sales,lead@example.com,555-0002\n"
        "E3,C2,Director,lead@example.com,\n"
        "E4,C9,Chief,lead@example.com,555-0004\n"
        "E5,C1,Analyst,lead@example.com,555-0005\n",
        encoding="utf-8",
    )
    (directory / COMPANIES_FILE).write_text(
        "Company_ID,Industry,Annual_Revenue (M),Frequency_of_Purchase\n"
        "C1, manufacturing ,12.5,Quarterly\n"
        "C2,Retail,3,Monthly\n",
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def errors_file(tmp_path) -> Path:
    return tmp_path / "errors" / "schema_mismatch.jsonl"
