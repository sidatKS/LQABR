"""The Research Agent's HTTP surface.

    GET  /                 identity + route index
    GET  /health /healthz  what this instance is bound to
    GET  /mcp/tools               what the MCP exposes right now
    POST /research/run            one contact       (hand-driven, curl, tests)
    POST /research/campaign       one post -> its whole industry (synchronous)
    POST /research/a2a            gateway hand-off, the id is a CONTACT
    POST /research/campaign/a2a   gateway hand-off, the id is a POST

The two a2a routes differ by WHAT the id is, not by how they answer: a blog
post sent to the contact route fails at read_lead, because a Ticket is not a
lead. Both acknowledge immediately and work in the background.

Run locally:  uvicorn service_app:app --port 8086 --app-dir agents/research/src
"""

from __future__ import annotations

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

# --- make the agent's own library importable ------------------------------
# packages/research_core lives inside this agent, deliberately NOT installed and
# NOT on the repo's shared path: the agent is standalone (see README).
_AGENT_ROOT = Path(__file__).resolve().parents[1]
for _path in (str(_AGENT_ROOT / "packages"), str(_AGENT_ROOT / "src")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# --- local development: load .env if python-dotenv is installed ------------
# Deployed environments inject real environment variables; `override=False`
# means a real variable always beats a stale local file.
try:  # pragma: no cover - depends on the local environment
    from dotenv import load_dotenv

    if "pytest" not in sys.modules and "PYTEST_CURRENT_TEST" not in os.environ:
        load_dotenv(_AGENT_ROOT / ".env", override=False)
except ImportError:
    pass

from fastapi import BackgroundTasks, FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from research_core.mcp.client import MCPError, MCPToolMissing  # noqa: E402
from research_core.mcp.hubspot import HubSpotMCP  # noqa: E402
from research_core.obs import configure_logging, get_obs, new_run_id  # noqa: E402
from research_core.settings import get_settings  # noqa: E402

from pipeline import run_campaign, run_research  # noqa: E402
from schema import (A2AEnvelope, CampaignRequest, CampaignResponse,  # noqa: E402
                    ResearchRequest, ResearchResponse)

SETTINGS = get_settings()


def _startup_mcp_check() -> Dict[str, Any]:
    """Discover the MCP surface at boot so a config/server mismatch surfaces
    here rather than as a write that quietly did nothing."""
    state: Dict[str, Any] = {"checked": False, "reachable": False, "tools": [], "error": ""}
    if SETTINGS.mcp_startup_check == "off":
        return state
    state["checked"] = True
    try:
        discovered = HubSpotMCP(settings=SETTINGS).ensure_ready()
        state["reachable"] = True
        state["tools"] = sorted(discovered)
    except MCPToolMissing as exc:
        state["error"] = str(exc)
        if SETTINGS.mcp_startup_check == "strict":
            raise
        get_obs().process.emit("mcp_startup_check_failed", reason=str(exc))
    except MCPError as exc:
        state["error"] = str(exc)
        if SETTINGS.mcp_startup_check == "strict":
            raise
        get_obs().process.emit("mcp_startup_check_unreachable", reason=str(exc))
    else:
        get_obs().process.emit("mcp_startup_check_ok", tools=state["tools"])
    return state


_MCP_STATE: Dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(SETTINGS.log_level, SETTINGS.log_file, SETTINGS.log_format)
    obs = get_obs(new_run_id(), refresh=True)
    obs.process.emit("service_start", service="lqabr-research-agent", version="0.1.0",
                     config=SETTINGS.redacted())
    _MCP_STATE.update(_startup_mcp_check())
    yield
    get_obs().process.emit("service_stop", service="lqabr-research-agent")


app = FastAPI(title="lqabr-research-agent", version="0.1.0", lifespan=lifespan)

if SETTINGS.cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=SETTINGS.cors_origins,
                       allow_methods=["*"], allow_headers=["*"])


def _health_payload() -> Dict[str, Any]:
    return {
        "status": "UP",
        "service": "lqabr-research-agent",
        "version": "0.1.0",
        "model": SETTINGS.model,
        "dry_run": SETTINGS.dry_run,
        "search": {"enabled": SETTINGS.search_enabled,
                   "max_uses": SETTINGS.search_max_uses},
        "mcp": {
            "url": SETTINGS.mcp_base_url,
            "checked": _MCP_STATE.get("checked", False),
            "reachable": _MCP_STATE.get("reachable", False),
            "tools": _MCP_STATE.get("tools", []),
            "error": _MCP_STATE.get("error", ""),
            "read_lead": SETTINGS.mcp_tool_read_lead,
            "read_blog": SETTINGS.mcp_tool_read_blog,
            "write": SETTINGS.mcp_tool_write,
            "list_leads": SETTINGS.mcp_tool_list_leads,
        },
        "hubspot": {"context_property": SETTINGS.hubspot_context_property},
    }


@app.api_route("/", methods=["GET", "HEAD"])
async def index() -> Dict[str, Any]:
    """Identity + route index. HEAD is accepted because tunnels and uptime
    monitors probe with it — a 405 there reads as "service unhealthy"."""
    return {"service": "lqabr-research-agent", "version": "0.1.0",
            "routes": ["/health", "/healthz", "/mcp/tools",
                       SETTINGS.route_run, "/research/campaign",
                       SETTINGS.route_a2a, SETTINGS.route_campaign_a2a]}


@app.get("/health")
async def health() -> Dict[str, Any]:
    return _health_payload()


@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    return _health_payload()


@app.get("/mcp/tools")
async def mcp_tools() -> Dict[str, Any]:
    """What the MCP exposes right now, and whether our names are on it.

    `list_leads` is campaign-only: single-lead runs work without it, so it
    shows up under `missing` rather than making this endpoint an error.
    """
    configured = {"read_lead": SETTINGS.mcp_tool_read_lead,
                  "read_blog": SETTINGS.mcp_tool_read_blog,
                  "write": SETTINGS.mcp_tool_write,
                  "list_leads": SETTINGS.mcp_tool_list_leads}
    try:
        tools = HubSpotMCP(settings=SETTINGS).client.list_tools()
    except MCPError as exc:
        return {"url": SETTINGS.mcp_base_url, "error": str(exc),
                "configured": configured, "tools": [], "missing": list(configured.values())}
    return {"url": SETTINGS.mcp_base_url, "tools": sorted(tools),
            "configured": configured,
            "missing": [name for name in configured.values() if name not in tools]}


@app.post(SETTINGS.route_run, response_model=ResearchResponse)
async def research_run(request: ResearchRequest) -> ResearchResponse:
    """The domain entry point. Synchronous: the caller wants the outcome."""
    return run_research(request.resolved())


@app.post("/research/campaign", response_model=CampaignResponse)
async def research_campaign(request: CampaignRequest) -> CampaignResponse:
    """One published post -> lead_context for every lead in its industry.

    Synchronous on purpose: the caller asked how many leads matched and what
    happened to each, so it waits for the counts. A campaign over many leads
    is long — drive it from a job or a background caller, not a 5s webhook.
    """
    return run_campaign(request.resolved())


@app.post(SETTINGS.route_a2a)
async def research_a2a(envelope: A2AEnvelope, background: BackgroundTasks) -> Dict[str, Any]:
    """The gateway's hand-off.

    Acknowledge immediately and do the work in the background: the gateway
    answers HubSpot inside its ~5s delivery budget, and a research pass (search
    + model + two CRM hops) is far longer than that. The outcome lands on the
    log streams under the gateway's run id, which is how the two are tied.
    """
    target = envelope.target()
    run_id = envelope.run_id() or new_run_id()
    obs = get_obs()

    if not target.object_id:
        obs.audit.emit("http_in", route=SETTINGS.route_a2a, status=400,
                       error="payload carries no object_id")
        return {"jsonrpc": "2.0", "id": envelope.id,
                "result": {"status": "rejected", "reason": "payload carries no object_id"}}

    obs.audit.emit("http_in", route=SETTINGS.route_a2a, status=200,
                   object_id=target.object_id,
                   summary_ref_id=target.summary_ref_id, run_id=run_id)

    background.add_task(run_research, target, run_id=run_id)
    result = {"status": "accepted", "object_id": target.object_id, "run_id": run_id}
    if envelope.jsonrpc == "2.0":
        return {"jsonrpc": "2.0", "id": envelope.id, "result": result}
    return result


@app.post(SETTINGS.route_campaign_a2a)
async def research_campaign_a2a(envelope: A2AEnvelope,
                                background: BackgroundTasks) -> Dict[str, Any]:
    """The gateway's blog-summary hand-off — one post, the whole industry.

    The sibling of /research/a2a, and the distinction matters: there the
    `object_id` is a CONTACT, here it is the published POST. Sending a post to
    the contact route fails at read_lead, because a Ticket is not a lead.

    Acknowledge first for the same reason as the sibling — the gateway answers
    HubSpot inside ~5s, and a campaign over N leads runs for minutes. The
    outcome lands on the log streams under this run id.
    """
    target = envelope.campaign_target()
    run_id = envelope.run_id() or new_run_id()
    obs = get_obs()

    if not target.object_id:
        obs.audit.emit("http_in", route=SETTINGS.route_campaign_a2a, status=400,
                       error="payload carries no object_id")
        return {"jsonrpc": "2.0", "id": envelope.id,
                "result": {"status": "rejected",
                           "reason": "payload carries no object_id"}}

    obs.audit.emit("http_in", route=SETTINGS.route_campaign_a2a, status=200,
                   object_id=target.object_id, industry=target.industry,
                   limit=target.limit, run_id=run_id)

    background.add_task(run_campaign, target, run_id=run_id)
    result = {"status": "accepted", "object_id": target.object_id,
              "run_id": run_id, "mode": "campaign"}
    if envelope.jsonrpc == "2.0":
        return {"jsonrpc": "2.0", "id": envelope.id, "result": result}
    return result
