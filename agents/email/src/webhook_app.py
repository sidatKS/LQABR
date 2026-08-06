"""Mailgun event webhook — FastAPI app deployed alongside the Email Agent.

Mailgun POSTs signed JSON events here. We verify the signature, map the
event to an EngagementEvent, and record it on the HubSpot contact:

    delivered -> EMAIL_DELIVERED (+2)
    opened    -> EMAIL_OPENED    (+5)   (also updates "last opened")
    clicked   -> EMAIL_CLICKED   (+10)  (internal link clicks)

record_event() increments the lqabr_* counter, recomputes probability, and
promotes the lead's stage when it crosses TEXT_VOICE_THRESHOLD — at which
point the orchestrator hands it to the Text/Voice Agent.

Run locally:  uvicorn webhook_app:app --port 8081  (from agents/email/src)
Deployed by:  infra/gcp/05_deploy_agents.sh (Cloud Run service lqabr-email-webhook)
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request

from lqabr_core.crm import CRMError, HubSpotClient
from lqabr_core.types import EngagementEvent, EventType

from lqabr_core.mailgun import verify_webhook_signature

logger = logging.getLogger("lqabr.email.webhook")

EVENT_MAP = {
    "delivered": EventType.EMAIL_DELIVERED,
    "opened": EventType.EMAIL_OPENED,
    "clicked": EventType.EMAIL_CLICKED,
}

app = FastAPI(title="LQABR Mailgun webhook")


def _crm() -> HubSpotClient:
    # Factory (overridable in tests via app.dependency_overrides-style patching).
    return HubSpotClient()


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/mailgun")
async def mailgun_webhook(request: Request) -> Dict[str, Any]:
    payload = await request.json()

    sig = payload.get("signature", {})
    if not verify_webhook_signature(str(sig.get("timestamp", "")), str(sig.get("token", "")),
                                    str(sig.get("signature", ""))):
        raise HTTPException(status_code=401, detail="invalid Mailgun signature")

    event_data = payload.get("event-data", {})
    event_name = event_data.get("event")
    event_type = EVENT_MAP.get(event_name)
    if event_type is None:
        # Not an event we score (e.g. accepted, failed) — acknowledge and skip.
        return {"status": "ignored", "event": event_name}

    variables = event_data.get("user-variables", {}) or {}
    contact_id = variables.get("contact_id")
    if not contact_id:
        # Never silently drop: surface loudly so the send path gets fixed.
        logger.error("Mailgun %s event without contact_id: %s",
                     event_name, event_data.get("message", {}))
        raise HTTPException(status_code=422, detail="event missing contact_id")

    detail = None
    if event_type is EventType.EMAIL_CLICKED:
        detail = event_data.get("url")

    try:
        lead = _crm().record_event(EngagementEvent(
            event_type=event_type,
            contact_id=str(contact_id),
            occurred_at=str(event_data.get("timestamp") or "") or None,
            detail=detail,
        ))
    except CRMError as exc:
        # 500 so Mailgun retries — the event must not be lost.
        raise HTTPException(status_code=500, detail=f"CRM writeback failed: {exc}") from exc

    return {"status": "recorded", "event": event_name,
            "contact_id": contact_id, "probability": lead.probability,
            "stage": lead.stage.value}
