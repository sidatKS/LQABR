"""Shared fixtures for the Agent Gateway tests.

The fixtures load the **real** ``config/config.yaml`` and
``config/agents_registry.yaml`` rather than inventing test doubles for them.
Those files are deliverables: a typo in a route value or a missing
``endpoint_env`` is exactly the kind of defect that reaches production silently,
so the test suite is the thing that reads them first.

Transports are faked; decisions are not.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

GATEWAY_ROOT = Path(__file__).resolve().parents[1]
SRC = GATEWAY_ROOT / "src"
LIB = GATEWAY_ROOT / "lib"
CONFIG_DIR = GATEWAY_ROOT / "config"

for path in (str(LIB), str(SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _load(name: str, filename: str):
    """Load a gateway module under a unique name.

    Several agents in this repo ship modules called ``server``/``router``, so
    the gateway's are namespaced to keep pytest's shared module table honest.
    """
    spec = importlib.util.spec_from_file_location(name, SRC / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Order matters: audit and dispatch import router / audit by flat name.
gw_router = _load("gw_router", "router.py")
sys.modules["router"] = gw_router
gw_audit = _load("gw_audit", "audit.py")
sys.modules["audit"] = gw_audit
gw_dispatch = _load("gw_dispatch", "dispatch.py")
sys.modules["dispatch"] = gw_dispatch
# server.py imports call_report by flat name, same as the others.
gw_call_report = _load("gw_call_report", "call_report.py")
sys.modules["call_report"] = gw_call_report
gw_server = _load("gw_server", "server.py")


# --------------------------------------------------------------------- config
@pytest.fixture(scope="session")
def config_dir() -> Path:
    return CONFIG_DIR


@pytest.fixture()
def config():
    from soloai import load_config
    return load_config(CONFIG_DIR / "config.yaml")


@pytest.fixture()
def registry_document() -> Dict[str, Any]:
    from soloai import load_registry_document
    return load_registry_document(CONFIG_DIR / "agents_registry.yaml")


@pytest.fixture()
def agent_env() -> Dict[str, str]:
    """Resolvable endpoints for all three agents."""
    return {
        "LQABR_EMAIL_AGENT_URL": "https://email-agent.example.test/a2a",
        "LQABR_TEXT_VOICE_AGENT_URL": "https://voice-agent.example.test/a2a",
    }


@pytest.fixture()
def registry(registry_document, agent_env):
    return gw_router.AgentRegistry.from_document(registry_document, environ=agent_env)


@pytest.fixture()
def hooks():
    """Audit hooks that keep every record and write nowhere noisy."""
    from soloai.audit_hooks import AuditHooks
    return AuditHooks(sink="file", file_path="/dev/null", keep_records=True,
                      level="verbose")


@pytest.fixture()
def audit(hooks):
    return gw_audit.GatewayAudit(hooks)


@pytest.fixture()
def router(registry):
    return gw_router.Router(registry=registry)


# ----------------------------------------------------------------- transports
class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Optional[Dict[str, Any]] = None,
                 text: str = "", raise_exc: Optional[Exception] = None) -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {"result": {"accepted": True}}
        self.text = text or "{}"
        self._raise = raise_exc

    def json(self) -> Dict[str, Any]:
        if self._raise:
            raise self._raise
        return self._payload


class FakeSession:
    """Stand-in for ``requests.Session`` that records what was posted.

    ``responses`` may be a single response reused for every call, or a list
    consumed in order (to script a retry).
    """

    def __init__(self, responses: Any = None) -> None:
        self.calls: List[Dict[str, Any]] = []
        if responses is None:
            responses = FakeResponse()
        self._queue = list(responses) if isinstance(responses, list) else None
        self._single = None if self._queue else responses

    def post(self, url: str, json: Any = None, timeout: Any = None,
             headers: Any = None, **kw: Any):
        self.calls.append({"url": url, "json": json, "timeout": timeout,
                           "headers": headers or {}})
        item = self._queue.pop(0) if self._queue else self._single
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def last_body(self) -> Dict[str, Any]:
        return self.calls[-1]["json"]


@pytest.fixture()
def fake_session_factory():
    return FakeSession


@pytest.fixture()
def fake_response_factory():
    return FakeResponse


# --------------------------------------------------------------------- events
def make_event(
    property_name: Optional[str] = "lqabr_email_status",
    property_value: Optional[str] = "OPENED",
    *,
    object_id: str = "701",
    event_id: str = "evt-1",
    subscription_type: Optional[str] = "contact.propertyChange",
    attempt_number: int = 0,
    change_source: str = "CRM",
    portal_id: int = 246777241,
    occurred_at: int = 1753876800000,
) -> Dict[str, Any]:
    """One HubSpot event, shaped exactly as Rev 3 page 3 documents it."""
    event: Dict[str, Any] = {
        "objectId": object_id,
        "subscriptionType": subscription_type,
        "portalId": portal_id,
        "eventId": event_id,
        "occurredAt": occurred_at,
        "attemptNumber": attempt_number,
        "changeSource": change_source,
    }
    if property_name is not None:
        event["propertyName"] = property_name
    if property_value is not None:
        event["propertyValue"] = property_value
    return event


@pytest.fixture()
def event_factory():
    return make_event
