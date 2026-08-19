"""The Email Agent's HTTP surface — ONE image, ONE service, ONE endpoint.

    GET  /                 identity + route index (never a bare 404)
    GET  /health           }  identical payloads, so nothing downstream has
    GET  /healthz          }  to know which spelling we chose
    POST /email/campaign   the gateway's entry point: a trigger with object_id
    POST /mailgun/events   Mailgun's inbound event call (HMAC-verified)

Zero instances at rest. A trigger reaches the gateway, Cloud Run spins an
instance, this app binds $PORT and answers /health as soon as it is up; the
run does its work synchronously and the instance scales back to zero. Nothing
here polls, sleeps, schedules or stays warm — every engagement event Mailgun
raises is PUSHED to /mailgun/events carrying its own lqabr_object_id, so the
agent never asks Mailgun for status and holds no run state to reconcile.

WHICH ROUTES A DEPLOYMENT SERVES — ``LQABR_EMAIL_ROUTES``:
    all       everything (default; local dev, single-service deployment)
    campaign  POST /email/campaign only
    webhook   POST /mailgun/events only

AUTHENTICATION
    /mailgun/events   the Mailgun HMAC on every event. No bypass flag.
    /email/campaign   currently UNAUTHENTICATED (the gateway-token check was
                      removed 2026-08-05; scoped JWTs are the intended fix).

ADK stays available for local development (`adk web|run|api_server
agents/email/src` — see agent.py); set LQABR_EMAIL_MOUNT_ADK=1 to also mount
the ADK runner under /adk.

Run locally:  uvicorn service_app:app --port 8080   (from agents/email/src)
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Optional


def _load_local_env() -> None:
    """Local dev: load agents/email/.env before any secret is read (uvicorn does
    not). ``override=False`` so Cloud Run's injected environment stays
    authoritative. Skipped under pytest so a developer's real credentials never
    reach a test run."""
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv absent — rely on the ambient environment
        return
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


_load_local_env()

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import events as events_module  # noqa: E402
import outreach  # noqa: E402
from outreach import configure_logging, log_audit, log_system  # noqa: E402

from lqabr_core.crm import CRMError  # noqa: E402
from lqabr_core.mailgun import verify_webhook_signature  # noqa: E402
from lqabr_core.secrets import SecretNotFoundError  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.hubspot.auth import TokenError  # noqa: E402

logger = logging.getLogger("lqabr.email.service")

SERVICE_NAME = "email_agent"
SERVICE_VERSION = "2"
CAMPAIGN_ROUTE = "/email/campaign"
MAILGUN_ROUTE = events_module.EVENTS_ROUTE
_ROUTE_MODES = ("all", "campaign", "webhook")


def routes_mode() -> str:
    mode = os.environ.get("LQABR_EMAIL_ROUTES", "all").strip().lower()
    if mode not in _ROUTE_MODES:
        raise RuntimeError(f"LQABR_EMAIL_ROUTES={mode!r} is not one of all|campaign|webhook")
    return mode


# ------------------------------------------------------------------- models
class CampaignRequest(BaseModel):
    """The trigger's payload: an id and nothing more — the lead profiles stay
    in HubSpot and are fetched back by it. ``trigger_id`` is accepted as an
    alias because the design document, the gateway and the HubSpot property
    all call it that."""

    object_id: Optional[str] = None
    trigger_id: Optional[str] = None
    limit: int = 0
    dry_run: bool = False
    run_id: Optional[str] = None

    def resolved_id(self) -> str:
        value = (self.object_id or self.trigger_id or "").strip()
        if not value:
            raise HTTPException(status_code=400,
                                detail="object_id (or its alias trigger_id) is required")
        return value


# ----------------------------------------------------------------- handlers
def run_campaign(body: CampaignRequest) -> Dict[str, Any]:
    """The gateway's entry point -> ``outreach.run_campaign``.

    Synchronous by design: Cloud Run throttles CPU once a response is
    returned, so a 202-and-background-task would strand half the batch. The
    batch size (``limit`` / LQABR_EMAIL_BATCH_LIMIT) is what keeps the run
    inside the request timeout. Upstream faults map to distinguishable
    statuses so the caller can tell config from auth from CRM."""
    object_id = body.resolved_id()
    log_audit(None, step=2, direction="inbound", endpoint=CAMPAIGN_ROUTE, method="POST",
              status_code=200, object_id=object_id, dry_run=body.dry_run)
    try:
        return outreach.run_campaign(object_id, limit=body.limit, dry_run=body.dry_run,
                                     run_id=body.run_id)
    except SecretNotFoundError as exc:   # configuration — retry after it is fixed
        logger.error("credential unavailable: %s", exc)
        raise HTTPException(status_code=503, detail=f"secret-error: {exc}") from exc
    except TokenError as exc:
        logger.error("HubSpot bearer unavailable: %s", exc)
        raise HTTPException(status_code=502, detail=f"auth-error: {exc}") from exc
    except CRMError as exc:
        logger.error("HubSpot unavailable: %s", exc)
        raise HTTPException(status_code=502, detail=f"crm-error: {exc}") from exc


def _handle(event_data: Dict[str, Any]) -> Dict[str, Any]:
    """Indirection so tests can substitute the step 8/9 handler."""
    return events_module.handle_event(event_data)


def dispatch_mailgun(payload: Dict[str, Any],
                     handler: Callable[[Dict[str, Any]], Dict[str, Any]],
                     endpoint: str = MAILGUN_ROUTE) -> Dict[str, Any]:
    """Verify the Mailgun HMAC, then hand ``event-data`` to steps 8-9.

    A signing key that cannot be resolved is a 503 (Mailgun retries, the event
    is deferred not lost); a bad signature is a 401. An ``unresolved`` result
    is never swallowed: a CRM failure is a 500 so Mailgun retries, anything
    that cannot be attributed to a lead is a 422 because retrying will not
    help."""
    signature = payload.get("signature") or {}
    try:
        authentic = verify_webhook_signature(str(signature.get("timestamp", "")),
                                             str(signature.get("token", "")),
                                             str(signature.get("signature", "")))
    except SecretNotFoundError as exc:
        log_audit(None, step=8, direction="inbound", endpoint=endpoint, method="POST",
                  status_code=503, error=str(exc))
        logger.error("Mailgun signing key unavailable, event deferred: %s", exc)
        raise HTTPException(status_code=503,
                            detail=f"secret-error: cannot verify the Mailgun signature: {exc}"
                            ) from exc
    if not authentic:
        log_audit(None, step=8, direction="inbound", endpoint=endpoint, method="POST",
                  status_code=401, error="invalid Mailgun signature")
        raise HTTPException(status_code=401, detail="invalid Mailgun signature")

    result = handler(payload.get("event-data") or {})
    if result.get("status") == "unresolved":
        reason = str(result.get("reason", ""))
        logger.error("Mailgun event unresolved: %s", result)
        if reason.startswith("crm-error"):
            raise HTTPException(status_code=500, detail=reason)
        raise HTTPException(status_code=422, detail=reason or "event could not be attributed")
    return result


# -------------------------------------------------------------- app factory
def create_app(routes: Optional[str] = None) -> FastAPI:
    mode = routes or routes_mode()
    serves_campaign = mode in ("all", "campaign")
    serves_webhook = mode in ("all", "webhook")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        configure_logging()
        log_system(event="container_started", component=SERVICE_NAME, routes=mode)
        yield
        log_system(event="container_stopped", component=SERVICE_NAME)

    app = FastAPI(title="LQABR Email Agent", version=SERVICE_VERSION, lifespan=lifespan)

    published = ["GET /", "GET /health", "GET /healthz"]
    if serves_campaign:
        published.append(f"POST {CAMPAIGN_ROUTE}")
    if serves_webhook:
        published.append(f"POST {MAILGUN_ROUTE}")

    def _health() -> Dict[str, Any]:
        return {"status": "ok", "service": SERVICE_NAME, "version": SERVICE_VERSION,
                "routes": mode}

    # Both spellings: the ADK runner answers /health and the old webhook
    # answered /healthz; serving both means no probe has to remember which.
    app.add_api_route("/health", _health, methods=["GET"])
    app.add_api_route("/healthz", _health, methods=["GET"])

    @app.get("/")
    def index() -> Dict[str, Any]:
        return {"service": SERVICE_NAME, "version": SERVICE_VERSION, "routes": published}

    if serves_campaign:
        # `def`, not `async def`: FastAPI runs blocking work in a threadpool so
        # the event loop stays free to answer health probes mid-campaign.
        app.add_api_route(CAMPAIGN_ROUTE, run_campaign, methods=["POST"])

    if serves_webhook:
        async def mailgun_events(request: Request) -> Dict[str, Any]:
            return dispatch_mailgun(await request.json(), _handle)

        app.add_api_route(MAILGUN_ROUTE, mailgun_events, methods=["POST"])

    if os.environ.get("LQABR_EMAIL_MOUNT_ADK", "").strip().lower() in ("1", "true", "yes"):
        _mount_adk(app)

    return app


def _mount_adk(app: FastAPI) -> None:
    """Mount the ADK runner under /adk. Guarded: ADK's app factory is
    version-coupled and must not take the domain surface down at import."""
    try:
        from google.adk.cli.fast_api import get_fast_api_app

        app.mount("/adk", get_fast_api_app(agents_dir=str(Path(__file__).resolve().parent),
                                           web=False))
        logger.info("ADK runner mounted at /adk")
    except Exception as exc:  # noqa: BLE001 - never fatal
        logger.error("LQABR_EMAIL_MOUNT_ADK set but the ADK app could not be built: %s", exc)


app = create_app()
