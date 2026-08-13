"""Zoom event webhook — FastAPI app deployed alongside the Scheduling Agent.

Zoom sends Scheduler events here (configure the event subscription in the
Zoom S2S OAuth app; we handle "scheduler booking created"-style events plus
Zoom's endpoint URL validation challenge).

On a booking we look up the invitee's email in HubSpot and record
MEETING_SCHEDULED — probability pins at MEETING_SCHEDULED_PROBABILITY and
the lead moves to the meeting_scheduled stage for human handoff.

Run locally:  uvicorn webhook_app:app --port 8083  (from agents/scheduling/src)
Deployed by:  infra/gcp/05_deploy_agents.sh (Cloud Run service lqabr-scheduling-webhook)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request

from lqabr_core.crm import CRMError, HubSpotClient
from lqabr_core.secrets import get_secret
from lqabr_core.types import EngagementEvent, EventType, LeadStage

logger = logging.getLogger("lqabr.scheduling.webhook")

app = FastAPI(title="LQABR Zoom webhook")


def _crm() -> HubSpotClient:
    return HubSpotClient()


def _secret_token() -> str:
    return get_secret("lqabr-zoom-webhook-secret-token")


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/zoom")
async def zoom_webhook(request: Request) -> Dict[str, Any]:
    payload = await request.json()
    event = payload.get("event", "")

    # Zoom endpoint validation handshake.
    if event == "endpoint.url_validation":
        plain = payload.get("payload", {}).get("plainToken", "")
        encrypted = hmac.new(_secret_token().encode(), plain.encode(), hashlib.sha256).hexdigest()
        return {"plainToken": plain, "encryptedToken": encrypted}

    # Verify Zoom's signature header (v0=HMAC(ts:body)).
    timestamp = request.headers.get("x-zm-request-timestamp", "")
    signature = request.headers.get("x-zm-signature", "")
    body = (await request.body()).decode("utf-8")
    expected = "v0=" + hmac.new(_secret_token().encode(),
                                f"v0:{timestamp}:{body}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="invalid Zoom signature")

    if "scheduler" not in event and "meeting" not in event:
        return {"status": "ignored", "event": event}

    obj = payload.get("payload", {}).get("object", {})
    invitee_email = (obj.get("invitee", {}) or {}).get("email") or obj.get("email")
    if not invitee_email:
        logger.error("Zoom %s event without invitee email: %s", event, obj)
        raise HTTPException(status_code=422, detail="event missing invitee email")

    lead = _crm().find_lead_by_email(invitee_email)
    if lead is None or not lead.hubspot_contact_id:
        logger.error("Zoom booking for unknown lead %s", invitee_email)
        raise HTTPException(status_code=404, detail=f"no HubSpot contact for {invitee_email}")

    try:
        crm = _crm()
        updated = crm.record_event(EngagementEvent(
            event_type=EventType.MEETING_SCHEDULED,
            hubspot_contact_id=lead.hubspot_contact_id,
            detail=obj.get("schedule_id") or obj.get("id"),
        ))
        crm.set_stage(lead.hubspot_contact_id, LeadStage.MEETING_SCHEDULED,
                      reason=f"zoom booking via {event}")
    except CRMError as exc:
        raise HTTPException(status_code=500, detail=f"CRM writeback failed: {exc}") from exc

    return {"status": "recorded", "event": event,
            "contact_id": lead.hubspot_contact_id,
            "probability": updated.probability,
            "stage": LeadStage.MEETING_SCHEDULED.value}
