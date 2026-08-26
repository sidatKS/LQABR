"""The Summary Agent's HTTP surface — one image, one service, one port.

    GET  /                    identity + route index (never a bare 404)
    GET  /health              }  identical payloads, so nothing downstream
    GET  /healthz             }  has to know which spelling we chose
    GET  /mcp/tools           what the MCP container actually exposes, live
    POST /summary/run         the domain entry point
    POST /summary/a2a         the gateway / orchestrator A2A envelope
    POST /chat                AG-UI streaming (mounted when enabled)

WHICH ROUTES A DEPLOYMENT SERVES
    LQABR_SUMMARY_ROUTES = all (default) | api | chat
    LQABR_SUMMARY_ENABLE_AGUI = 1 (default)

THE MCP AT STARTUP
The HubSpot MCP is a separate container that scales to zero, so finding it
asleep at boot is normal operation. The startup check therefore DISCOVERS
(tools/list) and records the outcome on /health rather than killing the
service — unless LQABR_SUMMARY_MCP_STARTUP_CHECK=strict, which is what a
production deployment should set once the MCP is always-on. Either way the
result is visible: /health carries it and /mcp/tools shows the live surface.

Run locally:  uvicorn service_app:app --port 8080   (from src/)
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

HERE = Path(__file__).resolve().parent
AGENT_DIR = HERE.parent
for path in (str(HERE), str(AGENT_DIR / "packages")):
    if path not in sys.path:
        sys.path.insert(0, path)


def _load_local_env() -> None:
    """Local dev only. `override=False` keeps Cloud Run's injection
    authoritative; skipped under pytest so a developer's real credentials
    never enter a test run."""
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(AGENT_DIR / ".env", override=False)


_load_local_env()

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import tools  # noqa: E402
from pipeline import run_summary  # noqa: E402
from schema import HubSpotTarget, SummaryRequest, SummaryResponse  # noqa: E402
from summary_core.mcp.client import MCPError  # noqa: E402
from summary_core.mcp.hubspot import HubSpotMCP  # noqa: E402
from summary_core.obs import (configure_logging, get_obs,  # noqa: E402
                              new_run_id, sink_state)
from summary_core.settings import get_settings  # noqa: E402

SERVICE = "lqabr-summary-agent"
VERSION = (AGENT_DIR / "VERSION").read_text(encoding="utf-8").strip() \
    if (AGENT_DIR / "VERSION").exists() else "0.0.0"

#: Filled by the startup check; reported on /health so an operator can see
#: whether this instance ever reached the MCP, and why not.
MCP_STATE: Dict[str, Any] = {"checked": False, "ok": False, "tools": [], "error": ""}


def _startup_mcp_check() -> None:
    settings = get_settings()
    if settings.mcp_startup_check == "off":
        MCP_STATE.update(checked=False, ok=False, error="startup check disabled")
        return
    hubspot = HubSpotMCP(settings=settings)
    try:
        discovered = hubspot.ensure_ready()
    except MCPError as exc:
        MCP_STATE.update(checked=True, ok=False, tools=[], error=str(exc))
        # system, not process: a startup probe is a boot fact, and research
        # emits its equivalent on the same stream.
        get_obs().system.emit("mcp_startup_check_failed", reason=str(exc),
                              mode=settings.mcp_startup_check)
        if settings.mcp_startup_check == "strict":
            # Deliberate: a strict deployment would rather not serve at all
            # than accept work it cannot land on the CRM.
            raise
        return
    MCP_STATE.update(checked=True, ok=True, tools=sorted(discovered), error="")
    get_obs().system.emit("mcp_startup_check_ok", tools=sorted(discovered))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings(refresh=True)
    configure_logging(settings.log_level, settings.log_dir,
                      settings.log_format,
                      max_bytes=settings.log_max_bytes,
                      backups=settings.log_backups,
                      log_file=settings.log_file, mode=settings.log_mode)
    obs = get_obs(new_run_id(), refresh=True)
    tools.configure(settings)
    # RE-HOMED from process to system (phase 4). Startup and shutdown are
    # system-stream facts by definition, and after the sink split they were the
    # reason summary_system.log would otherwise hold no records at all.
    obs.system.emit("service_start", service=SERVICE, version=VERSION,
                     routes=settings.routes, **{"config": settings.redacted()})
    _startup_mcp_check()
    yield
    obs.system.emit("service_stop", service=SERVICE)


app = FastAPI(
    title="LQABR Summary Agent",
    description=("Summarises a web page, a raw JSON payload, another service's "
                 "HTTP response or plain text, and writes the summary to HubSpot "
                 "through the HubSpot MCP."),
    version=VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------- identity
def _health_payload() -> Dict[str, Any]:
    settings = get_settings()
    return {
        "status": "UP",
        "service": SERVICE,
        "version": VERSION,
        "routes": settings.routes,
        "model": settings.model,
        "dry_run": settings.dry_run,
        # Where the three log streams are actually landing. `degraded` is the
        # field that matters: empty on a healthy agent, and naming a stream
        # whose file could not be opened or rotated otherwise.
        "logging": {"mode": settings.log_mode, **sink_state()},
        "mcp": {
            "url": settings.mcp_base_url,
            "checked": MCP_STATE["checked"],
            "reachable": MCP_STATE["ok"],
            "tools": MCP_STATE["tools"],
            "error": MCP_STATE["error"],
            "write_tool": settings.mcp_tool_write,
        },
        "hubspot": {
            "object_type": settings.hubspot_object_type,
            "summary_property": settings.hubspot_summary_property,
            "industry_property": settings.hubspot_industry_property,
        },
    }


@app.get("/")
async def index() -> Dict[str, Any]:
    """Identity and the route index — a wrong URL should never be a bare 404."""
    settings = get_settings()
    routes = ["GET /", "GET /health", "GET /healthz", "GET /mcp/tools"]
    if settings.serves_api:
        routes += ["POST /summary/run", "POST /summary/a2a"]
    if settings.serves_chat:
        routes += ["POST /chat"]
    return {"service": SERVICE, "version": VERSION, "routes": routes}


@app.get("/health")
async def health() -> Dict[str, Any]:
    return _health_payload()


@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    return _health_payload()


@app.get("/mcp/tools")
async def mcp_tools() -> Dict[str, Any]:
    """What the MCP container exposes RIGHT NOW.

    The one-call answer to "are we bound to the right tool names?" — no repo
    reading, no guessing, no redeploy.
    """
    settings = get_settings()
    try:
        discovered = HubSpotMCP(settings=settings).client.list_tools()
    except MCPError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    configured = {
        "read": settings.mcp_tool_read,
        "write": settings.mcp_tool_write,
        "list_leads": settings.mcp_tool_list_leads,
    }
    return {
        "url": settings.mcp_base_url,
        "tools": sorted(discovered),
        "configured": configured,
        "missing": sorted(name for name in configured.values()
                          if name and name not in discovered),
    }


# ---------------------------------------------------------------- domain
@app.post("/summary/run", response_model=SummaryResponse)
async def summary_run(request: SummaryRequest) -> SummaryResponse:
    """Summarise one source and, when an object id is given, write it to HubSpot.

    A refused source or an unusable model answer comes back as HTTP 200 with
    `status="failed"` and the reason: the caller asked a valid question and
    deserves the outcome, not a stack trace. Only an unexpected fault is a 500.
    """
    if not get_settings().serves_api:
        raise HTTPException(status_code=404, detail="this deployment serves LQABR_SUMMARY_ROUTES=chat")
    try:
        return run_summary(request)
    except Exception as exc:  # pragma: no cover - genuine faults only
        get_obs().process.emit("run_unhandled_error", reason=str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc


class A2AEnvelope(BaseModel):
    """The gateway / orchestrator hand-off, JSON-RPC 2.0 `message/send`.

    The gateway sends ids, never lead data. For this agent the message text
    carries WHAT to summarise (a URL, or a full request object as JSON) and
    `params.metadata.object_id` (mirrored at the top level by the gateway's
    compat shim) says WHERE the summary should land.
    """

    jsonrpc: str = "2.0"
    id: Optional[str] = None
    method: str = "message/send"
    params: Dict[str, Any] = {}
    # The gateway's top-level compat mirror.
    object_id: Optional[str] = None
    objectId: Optional[str] = None  # noqa: N815 - the wire name, not ours
    trigger_id: Optional[str] = None

    def message_text(self) -> str:
        message = (self.params or {}).get("message") or {}
        for part in message.get("parts") or []:
            if isinstance(part, dict) and part.get("text"):
                return str(part["text"])
        return ""

    def target_object_id(self) -> str:
        metadata = (self.params or {}).get("metadata") or {}
        for candidate in (metadata.get("object_id"), metadata.get("summary_ref_id"),
                          self.object_id, self.objectId):
            if candidate:
                return str(candidate)
        return ""


@app.post("/summary/a2a", response_model=SummaryResponse)
async def summary_a2a(envelope: A2AEnvelope) -> SummaryResponse:
    """The same run, reached through the A2A envelope the gateway speaks."""
    if envelope.method != "message/send":
        raise HTTPException(status_code=400,
                            detail=f"unsupported A2A method {envelope.method!r}")
    text = envelope.message_text().strip()
    if not text:
        raise HTTPException(status_code=400,
                            detail="the A2A message carried no text to summarise")

    # The text is either a full request object or a plain source (URL/prose).
    source: Any = text
    if text.startswith("{"):
        import json

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400,
                                detail=f"the A2A message text is not valid JSON: {exc.msg}") from exc
        source = parsed.get("source", parsed)

    object_id = envelope.target_object_id()
    request = SummaryRequest(
        source=source,
        hubspot=HubSpotTarget(object_id=object_id) if object_id else None,
    )
    return run_summary(request)


# ---------------------------------------------------------------- AG-UI
def _mount_agui(application: FastAPI) -> bool:
    """Mount /chat, but never let its absence take the service down.

    ag-ui-adk pulls in google-adk and a model. A deployment that serves only
    the domain API should not fail to boot because the UI stack is missing.
    """
    settings = get_settings()
    if not settings.serves_chat:
        return False
    try:
        from ag_ui_adk import ADKAgent, add_adk_fastapi_endpoint

        from agent import root_agent
    except ImportError as exc:
        get_obs().process.emit("agui_not_mounted", reason=str(exc))
        return False

    add_adk_fastapi_endpoint(
        application,
        ADKAgent(adk_agent=root_agent, app_name="lqabr-summary", user_id="summary_user",
                 session_timeout_seconds=3600, use_in_memory_services=True),
        path="/chat",
    )
    return True


AGUI_MOUNTED = _mount_agui(app)
