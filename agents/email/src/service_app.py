"""The Email Agent's HTTP surface — ONE image, ONE service, ONE endpoint.

Reading those together: the objection is to a SEPARATE always-on listener
service and to any scheduler we run. An inbound push from Mailgun to the
agent's own endpoint is what he specified — it is the second image, the
polling and the keep-alive that were wrong. So:

  * one image, one Cloud Run service, one endpoint;
  * Mailgun is configured with THIS service's URL — the same one the gateway
    reaches — and what happens next is business logic inside the agent;
  * message state is not pulled. Every engagement event Mailgun raises is
    pushed to /mailgun/events carrying its own lqabr_object_id, so the agent
    never asks Mailgun for status and holds no run state to reconcile;
  * nothing in this process polls, sleeps, schedules, or stays warm. The
    container exists for the length of a run and returns to zero instances.

    GET  /                     identity + route index (never a bare 404)
    GET  /health               }  identical payloads, so nothing downstream
    GET  /healthz              }  has to know which spelling we chose
    POST /email/campaign       STEP 2 — the gateway's entry point
    POST /mailgun/events       STEP 8 — Mailgun's inbound event call lands here

CONTAINER LIFECYCLE (Swaroop, 8:09 / 21:29)
-------------------------------------------
Zero instances at rest. A trigger reaches the gateway, the sandbox spins an
instance (~32s cold start today), this app binds 0.0.0.0:$PORT — 8080 on
Cloud Run — and answers /health as soon as the process is up. The run does
its work and the instance scales back to zero. Nothing here extends that
window on purpose.

ADK is still available for local development: `adk web|run|api_server
agents/email/src` works because `agent.py`/`root_agent` are untouched. Set
`LQABR_EMAIL_MOUNT_ADK=1` to mount the ADK runner under /adk in a pinch.

WHICH ROUTES A DEPLOYMENT SERVES
--------------------------------
`LQABR_EMAIL_ROUTES` — default `all`, which is the deployed shape: one
service serving both the gateway entry and the Mailgun push. `campaign` and
`webhook` exist only to narrow it if the auth boundary ever has to split
again; they are not the intended configuration.

AUTHENTICATION
--------------
Mailgun must be able to POST here, so the service is reachable without an
IAM token and each entry proves itself:

  * `/mailgun/events` — the Mailgun HMAC on every event. Not optional, no
    bypass flag.
  * `/email/campaign` — currently UNAUTHENTICATED. The LQABR_EMAIL_GATEWAY_TOKEN
    / X-LQABR-Gateway-Token check was removed 2026-08-05 (the gateway wasn't
    sending the header); nothing replaces it yet. Rev 3's short-lived scoped
    JWTs are the intended fix once the gateway track defines them.

Run locally:  uvicorn service_app:app --port 8080   (from agents/email/src)
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional


def _load_local_env() -> None:
    """Local dev: load agents/email/.env before any secret is read (uvicorn
    does not do this itself). `override=False` so Cloud Run's --set-secrets
    injection stays authoritative in production, where no .env file exists.

    Skipped under pytest, deliberately. This module is imported at test
    COLLECTION time, and loading a developer's real `.env` there would put
    their live credentials and model choice into every test's environment —
    including subprocesses the suite spawns. A test run must depend on
    nothing but its own fixtures."""
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv absent — rely on the ambient environment
        return
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


_load_local_env()

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from pydantic import BaseModel

import events as events_module
# --------------------------------------------------------------- logging
# One JSON line per event, stamped with object_id + run_id so a whole run
# greps back together. Inlined per module (no shared observability file); the
# MCP is handed `MCPObservability` because mcp/hubspot/ cannot import agent code.
import json as _json
import logging as _logging
import hashlib as _hashlib
from dataclasses import dataclass as _dataclass
from datetime import datetime as _datetime, timezone as _timezone

_LOG = _logging.getLogger("lqabr.email")


@_dataclass(frozen=True)
class RunContext:
    object_id: str
    run_id: str


def _emit(stream, ctx, **fields):
    _LOG.info(_json.dumps(
        {"stream": stream, "ts": _datetime.now(_timezone.utc).isoformat(),
         "agent": "email_agent",
         "object_id": ctx.object_id if ctx else None,
         "run_id": ctx.run_id if ctx else None, **fields}, default=str))


def _log_process(ctx, *, step=None, event, **f):
    _emit("process_log", ctx, step=step, event=event, **f)


def _log_audit(ctx, *, step=None, direction, endpoint, method="", status_code=None,
               bearer=None, **f):
    fp = _hashlib.sha256(bearer.encode()).hexdigest()[:12] if bearer else "none"
    _emit("audit_log", ctx, step=step, direction=direction, endpoint=endpoint,
          method=method, status_code=status_code, bearer_fingerprint=fp, **f)



def _log_system(**f):
    f.setdefault("host", os.environ.get("K_REVISION") or os.environ.get("HOSTNAME", "local"))
    _emit("system_log", None, **f)


def _configure_logging(level=_logging.INFO):
    _LOG.setLevel(level)
    if not _LOG.handlers:
        h = _logging.StreamHandler(); h.setFormatter(_logging.Formatter("%(message)s"))
        _LOG.addHandler(h)
    _LOG.propagate = False


import outreach

from lqabr_core.crm import CRMError
from lqabr_core.mailgun import verify_webhook_signature
from lqabr_core.secrets import SecretNotFoundError

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.hubspot.auth import TokenError  # noqa: E402

logger = logging.getLogger("lqabr.email.service")

SERVICE_NAME = "email_agent"

CAMPAIGN_ROUTE = "/email/campaign"
MAILGUN_ROUTE = "/mailgun/events"


def routes_mode() -> str:
    mode = os.environ.get("LQABR_EMAIL_ROUTES", "all").strip().lower()
    if mode not in ("all", "campaign", "webhook"):
        raise RuntimeError(
            f"LQABR_EMAIL_ROUTES={mode!r} is not one of all|campaign|webhook")
    return mode


# --------------------------------------------------------------------- models
class CampaignRequest(BaseModel):
    """STEP 2's payload. The trigger carries an id and nothing more — the
    lead profiles stay in HubSpot and are fetched back at step 5.

    `object_id` is the field this agent uses internally. `trigger_id` is
    accepted as an alias because the design document, the gateway and the
    HubSpot property all call it that; rejecting one spelling over a rename
    would be a pointless integration failure."""

    object_id: Optional[str] = None
    trigger_id: Optional[str] = None
    limit: int = 0
    dry_run: bool = False
    run_id: Optional[str] = None

    def resolved_id(self) -> str:
        value = (self.object_id or self.trigger_id or "").strip()
        if not value:
            raise HTTPException(
                status_code=400,
                detail="object_id (or its alias trigger_id) is required")
        return value


# ------------------------------------------------------------------- handlers
def _handle(event_data: Dict[str, Any]) -> Dict[str, Any]:
    """Indirection so tests can substitute the step 8/9 handler."""
    return events_module.handle_event(event_data)


def dispatch_mailgun(payload: Dict[str, Any], handler, endpoint: str = MAILGUN_ROUTE
                     ) -> Dict[str, Any]:
    """Verify the HMAC, then dispatch to steps 8–9.

    The single implementation of the Mailgun push contract. There is no
    second app and no second image — that was the point Platform
    Engineering made on 2026-08-04."""
    signature = payload.get("signature", {}) or {}
    try:
        authentic = verify_webhook_signature(str(signature.get("timestamp", "")),
                                             str(signature.get("token", "")),
                                             str(signature.get("signature", "")))
    except SecretNotFoundError as exc:
        # The signing key could not be resolved, so authenticity cannot be
        # established and the event MUST NOT be processed. 503 rather than a
        # bare 500: this is a credential/config fault, it is transient from
        # Mailgun's point of view, and a 5xx makes Mailgun retry — so the
        # event is deferred, not lost.
        _log_audit(None, step=8, direction="inbound", endpoint=endpoint,
                  method="POST", status_code=503, error=str(exc))
        logger.error("Mailgun signing key unavailable, event deferred: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=f"secret-error: cannot verify the Mailgun signature: {exc}") from exc

    if not authentic:
        _log_audit(None, step=8, direction="inbound", endpoint=endpoint,
                  method="POST", status_code=401, error="invalid Mailgun signature")
        raise HTTPException(status_code=401, detail="invalid Mailgun signature")

    result = handler(payload.get("event-data", {}) or {})

    if result.get("status") == "unresolved":
        # Never silently dropped. A CRM failure gets a 500 so Mailgun
        # retries the event; anything we cannot attribute to a lead gets a
        # 422, because retrying it would not help.
        reason = str(result.get("reason", ""))
        logger.error("Mailgun event unresolved: %s", result)
        if reason.startswith("crm-error"):
            raise HTTPException(status_code=500, detail=reason)
        raise HTTPException(status_code=422, detail=reason or "event could not be attributed")

    return result


# ----------------------------------------------------------------- app factory
def create_app(routes: Optional[str] = None) -> FastAPI:
    mode = routes or routes_mode()
    serves_campaign = mode in ("all", "campaign")
    serves_webhook = mode in ("all", "webhook")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        """system_log — container activity, the one stream that is meaningful
        when no run is in flight."""
        _configure_logging()
        _log_system(event="container_started", component=SERVICE_NAME, routes=mode)
        yield
        _log_system(event="container_stopped", component=SERVICE_NAME)

    app = FastAPI(title="LQABR Email Agent", version="2", lifespan=lifespan)

    published = ["GET /", "GET /health", "GET /healthz"]
    if serves_campaign:
        published += [f"POST {CAMPAIGN_ROUTE}"]
    if serves_webhook:
        published += [f"POST {MAILGUN_ROUTE}"]

    # ------------------------------------------------------------- liveness
    def _health() -> Dict[str, Any]:
        return {"status": "ok", "service": SERVICE_NAME, "version": "2", "routes": mode}

    # Both spellings, deliberately. The ADK runner answers /health and this
    # agent's webhook historically answered /healthz; serving both means no
    # probe, gateway or reviewer has to remember which.
    app.add_api_route("/health", _health, methods=["GET"])
    app.add_api_route("/healthz", _health, methods=["GET"])

    @app.get("/")
    def index() -> Dict[str, Any]:
        """A smoke-test curl should describe the service, not 404 the way the
        ADK runner did."""
        return {"service": SERVICE_NAME, "version": "2", "routes": published}

    # -------------------------------------------------------------- step 2
    if serves_campaign:

        @app.post(CAMPAIGN_ROUTE)
        def hubspot_campaign(body: CampaignRequest) -> Dict[str, Any]:
            """STEP 2 → STEPS 3-7. The gateway's entry point.

            Synchronous by design. Cloud Run throttles CPU to near zero once
            a response is returned, so answering 202 and finishing the batch
            in a background task would silently strand half the leads. The
            batch size (`limit`, default `LQABR_EMAIL_BATCH_LIMIT`) is what
            keeps the run inside the request timeout — size it against the
            service's configured timeout, not against the queue depth.

            Defined with `def`, not `async def`, so FastAPI runs this
            blocking work in a threadpool and the event loop stays free to
            answer health probes while a campaign is in flight.

            UNAUTHENTICATED as of 2026-08-05 — the gateway-token check was
            removed (gateway wasn't sending it). No boundary here yet."""
            object_id = body.resolved_id()

            _log_audit(None, step=2, direction="inbound", endpoint=CAMPAIGN_ROUTE,
                      method="POST", status_code=200, object_id=object_id,
                      dry_run=body.dry_run)
            try:
                return outreach.run_campaign(object_id, limit=body.limit,
                                             dry_run=body.dry_run, run_id=body.run_id)
            except SecretNotFoundError as exc:
                # A credential that cannot be resolved is configuration, not a
                # bug: 503 so the caller retries after it is fixed, with the
                # secret name in the message.
                logger.error("credential unavailable: %s", exc)
                raise HTTPException(status_code=503, detail=f"secret-error: {exc}") from exc
            except TokenError as exc:
                logger.error("HubSpot bearer unavailable: %s", exc)
                raise HTTPException(status_code=502, detail=f"auth-error: {exc}") from exc
            except CRMError as exc:
                logger.error("HubSpot unavailable: %s", exc)
                raise HTTPException(status_code=502, detail=f"crm-error: {exc}") from exc

    # -------------------------------------------------------------- step 8
    if serves_webhook:

        async def mailgun_events(request: Request) -> Dict[str, Any]:
            """STEP 8 — Mailgun makes an inbound API call here, to the SAME
            service the gateway reaches. Configure this URL in Mailgun."""
            payload = await request.json()
            return dispatch_mailgun(payload, _handle)

        app.add_api_route(MAILGUN_ROUTE, mailgun_events, methods=["POST"])

    # --------------------------------------------------- optional ADK mount
    if os.environ.get("LQABR_EMAIL_MOUNT_ADK", "").strip().lower() in ("1", "true", "yes"):
        _mount_adk(app)

    return app


def _mount_adk(app: FastAPI) -> None:
    """Mount the ADK runner under /adk for anything still speaking that
    contract during a transition.

    Guarded: ADK's app factory is version-coupled, and a signature change
    must not take the whole service down at import time. A failure here is
    logged and the domain surface still serves."""
    try:
        from google.adk.cli.fast_api import get_fast_api_app

        adk_app = get_fast_api_app(agents_dir=str(Path(__file__).resolve().parent), web=False)
        app.mount("/adk", adk_app)
        logger.info("ADK runner mounted at /adk")
    except Exception as exc:  # noqa: BLE001 - never fatal
        logger.error("LQABR_EMAIL_MOUNT_ADK set but the ADK app could not be built: %s", exc)


app = create_app()
