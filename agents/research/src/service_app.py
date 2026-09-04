"""The Research Agent's HTTP surface — one door in, two ways to look at it.

    POST /research/campaign/a2a   the gateway's hand-off: one published POST,
                                  acknowledged in milliseconds, then every lead
                                  in that post's industry researched in the
                                  background
    GET  /health                  what this instance is bound to
    GET  /mcp/tools               what the MCP exposes right now
    GET  /                        identity + route index (HEAD too: tunnels and
                                  uptime monitors probe with it, and a 405
                                  there reads as "service unhealthy")

That is the whole surface. The gateway's `agents_registry.yaml` has exactly one
route to this agent — `R-blog-summary`, `ticket.propertyChange` on
`blog_summary` — so a single write path is all there is to serve. One lead on
its own is the CLI (`agent.py`), which is hand-driven and needs both ids.

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
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from research_core import SERVICE_NAME, __version__  # noqa: E402
from research_core.mcp.client import MCPError, MCPToolMissing  # noqa: E402
from research_core.mcp.hubspot import HubSpotMCP  # noqa: E402
from research_core.research_logging_otel import (configure_logging,  # noqa: E402
                                                 get_obs, new_run_id, sink_state)
from research_core import SERVICE_NAME, __version__  # noqa: E402
from research_core.settings import get_settings  # noqa: E402

from pipeline import run_campaign  # noqa: E402
from schema import A2AEnvelope  # noqa: E402

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
        get_obs().system.emit("mcp_startup_check_failed", reason=str(exc))
    except MCPError as exc:
        state["error"] = str(exc)
        if SETTINGS.mcp_startup_check == "strict":
            raise
        get_obs().system.emit("mcp_startup_check_unreachable", reason=str(exc))
    else:
        get_obs().system.emit("mcp_startup_check_ok", tools=state["tools"])
    return state


_MCP_STATE: Dict[str, Any] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(SETTINGS.log_level, SETTINGS.log_dir, SETTINGS.log_format,
                      max_bytes=SETTINGS.log_max_bytes,
                      backups=SETTINGS.log_backups, log_file=SETTINGS.log_file,
                      mode=SETTINGS.log_mode,
                      retention_days=SETTINGS.log_retention_days)
    if SETTINGS.log_detail_deprecated:
        get_obs().system.emit(
            "log_detail_deprecated", mode=SETTINGS.log_mode,
            detail="LQABR_RESEARCH_LOG_DETAIL still works and resolved to this "
                   "mode. Use LQABR_RESEARCH_LOG_MODE=terse|normal|debug.")
    obs = get_obs(new_run_id(), refresh=True)
    obs.system.emit("service_start", service=SERVICE_NAME, version=__version__,
                    config=SETTINGS.redacted())
    _MCP_STATE.update(_startup_mcp_check())
    yield
    get_obs().system.emit("service_stop", service=SERVICE_NAME)


app = FastAPI(title=SERVICE_NAME, version=__version__, lifespan=lifespan)

if SETTINGS.cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=SETTINGS.cors_origins,
                       allow_methods=["*"], allow_headers=["*"])


def _health_payload() -> Dict[str, Any]:
    return {
        "status": "UP",
        "service": SERVICE_NAME,
        "version": __version__,
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
        "logging": {"mode": SETTINGS.log_mode, **sink_state()},
    }


@app.api_route("/", methods=["GET", "HEAD"])
async def index() -> Dict[str, Any]:
    """Identity + route index. HEAD is accepted because tunnels and uptime
    monitors probe with it — a 405 there reads as "service unhealthy"."""
    return {"service": SERVICE_NAME, "version": __version__,
            "routes": ["/health", "/mcp/tools", SETTINGS.route_campaign_a2a]}


@app.get("/health")
async def health() -> Dict[str, Any]:
    return _health_payload()


# NOT `async def`: every one of these does blocking I/O — `requests` to the MCP
# and to HubSpot, the Anthropic SDK for the model. Starlette offloads a plain
# `def` handler to a threadpool; an `async def` one runs ON the event loop, so a
# single research pass would freeze the whole process — /health included, and
# the two a2a acknowledgements the gateway is waiting on inside ~5s.
@app.get("/mcp/tools")
def mcp_tools() -> Dict[str, Any]:
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


def _guarded(runner: Any, route: str) -> Any:
    """Run in the background, but never disappear.

    The gateway already holds `{"status": "accepted"}`. Anything the pipeline
    does not catch — a ValueError out of get_settings on a bad env var, a
    SecretError while building the Composer — would otherwise propagate out of
    the background task with no `run_failed` anywhere, and the run simply never
    happened. Flagged with a named reason, then re-raised for the server log.
    """
    def _run(target: Any, *, run_id: str) -> None:
        try:
            runner(target, run_id=run_id)
        except BaseException as exc:  # noqa: BLE001 - named, then re-raised
            get_obs().process.emit(
                "run_crashed", route=route, run_id=run_id,
                objectId=getattr(target, "objectId", ""),
                reason=f"{type(exc).__name__}: {exc}")
            raise
    return _run


def _rejected(envelope: A2AEnvelope, route: str, reason: str,
              run_id: str) -> Any:
    """A refusal, correlatable.

    It used to log under whatever id `get_obs()` happened to mint for the
    request context — an id that appeared on exactly one line and led nowhere.
    A rejection is the line you most want to find later, so it carries the
    SAME id the run would have had: the gateway's when it sent one, and the
    one echoed back to the caller either way.
    """
    get_obs().audit.emit("http_in", route=route, status=400, reason=reason,
                         run_id=run_id, **envelope.source())
    body: Dict[str, Any] = {"status": "rejected", "reason": reason,
                            "run_id": run_id}
    if envelope.jsonrpc == "2.0":
        # A TOP-LEVEL `error`, not a rejection buried in `result`. The gateway
        # reads exactly this: `parsed.get("error")` makes the dispatch ok=False
        # and terminal, with the whole reason carried into its audit line. A
        # 2xx whose refusal sits under `result` is recorded there as a
        # SUCCESSFUL hand-off, which is how a refused campaign disappears.
        # (A bare 4xx would also flip ok, but the gateway keeps only
        # `HTTP 400: ` plus the first 200 characters of the body.)
        return {"jsonrpc": "2.0", "id": envelope.id,
                "error": {"code": -32602, "message": reason, "data": body}}
    # A plain HTTP caller — a hand-written curl — gets the status it expects.
    return JSONResponse(status_code=400, content=body)


def _accept(envelope: A2AEnvelope, background: BackgroundTasks, *, route: str,
            target: Any, runner: Any, logged: Dict[str, Any],
            result: Dict[str, Any], expects: str) -> Any:
    """Acknowledge, then work in the background.

    The gateway answers HubSpot inside its ~5s delivery budget, and a research
    pass (search + model + two CRM hops) is far longer than that. The outcome
    lands on the log streams under the gateway's run id, which is how the two
    are tied together.
    """
    # Resolved BEFORE the guards, so an accepted run and a refused one are
    # findable by the same key.
    run_id = envelope.run_id() or new_run_id()

    if not target.objectId:
        return _rejected(envelope, route, "payload carries no objectId", run_id)

    # HubSpot names the record kind in the event. When it disagrees with the
    # route, say so HERE — the alternative is a read that fails three steps
    # later with a CRM error that reads like a record went missing.
    kind = envelope.record_kind()
    if kind and kind != expects:
        return _rejected(
            envelope, route,
            f"bad-data: this route takes a {expects}, but the hand-off is a "
            f"HubSpot {envelope.source()['subscription_type']} — id "
            f"{target.objectId} is a {kind}. This agent researches a post's "
            "whole industry; one lead on its own is the CLI (agent.py).",
            run_id)

    get_obs().audit.emit("http_in", route=route, status=200,
                         objectId=target.objectId, run_id=run_id,
                         **envelope.source(), **logged)
    background.add_task(_guarded(runner, route), target, run_id=run_id)

    body = {"status": "accepted", "objectId": target.objectId,
            "run_id": run_id, **result}
    if envelope.jsonrpc == "2.0":
        return {"jsonrpc": "2.0", "id": envelope.id, "result": body}
    return body


@app.post(SETTINGS.route_campaign_a2a)
async def research_campaign_a2a(envelope: A2AEnvelope,
                                background: BackgroundTasks) -> Any:
    """The gateway's blog-summary hand-off — one post, the whole industry.

    The ONLY route the gateway drives. Its `agents_registry.yaml` has exactly
    one entry for this agent (`R-blog-summary`, `ticket.propertyChange` on
    `blog_summary`); every contact event it sees goes to the Email or Voice
    agent. `objectId` here is therefore always a published POST, never a
    contact — a Ticket is not a lead, and `read_lead` would fail on one.

    One lead on its own is the CLI (`agent.py`), which is hand-driven and
    needs both ids. There is no single-contact HTTP route, because nothing
    dispatches to one and a contact event carries no blog-post id to research
    against.
    """
    target = envelope.campaign_target()
    return _accept(envelope, background, route=SETTINGS.route_campaign_a2a,
                   target=target, runner=run_campaign,
                   logged={"limit": target.limit},
                   result={"mode": "campaign"}, expects="post")
