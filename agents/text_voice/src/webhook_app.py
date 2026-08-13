"""Twilio webhooks — FastAPI app deployed alongside the Text/Voice Agent.

Endpoints (all signed by Twilio; X-Twilio-Signature is validated):

    POST /voice/answer?contact_id=..   TwiML for a connecting call. Branches
                                       on AnsweredBy:
                                         human    -> Q&A flow step 1 (Flow B)
                                         machine  -> voicemail message, then
                                                     customized SMS (Flow A)
                                       Records CALL_ANSWERED / VOICEMAIL_LEFT.
    POST /voice/qa?contact_id=..&step=N  Gather callback per Q&A step; on flow
                                       completion with interest records
                                       CALL_ENGAGED (counter incremented in
                                       HubSpot -> probability rises; crossing
                                       SCHEDULING_THRESHOLD promotes the lead
                                       to the Scheduling Agent).
    POST /sms/status?contact_id=..     SMS delivery receipts -> SMS_DELIVERED.
    GET  /healthz

Run locally:  uvicorn webhook_app:app --port 8082  (from agents/text_voice/src)
Deployed by:  infra/gcp/05_deploy_agents.sh (Cloud Run service lqabr-text-voice-webhook)
"""

from __future__ import annotations

import logging
import os
from typing import Dict

from fastapi import FastAPI, HTTPException, Request, Response

from lqabr_core.crm import CRMError, HubSpotClient
from lqabr_core.types import EngagementEvent, EventType

try:
    from . import conversation
    from .twilio_client import TwilioClient, TwilioError, validate_twilio_signature
except ImportError:  # pragma: no cover
    import conversation  # type: ignore
    from twilio_client import TwilioClient, TwilioError, validate_twilio_signature  # type: ignore

logger = logging.getLogger("lqabr.text_voice.webhook")

app = FastAPI(title="LQABR Twilio webhook")

BASE_URL = os.environ.get("LQABR_WEBHOOK_BASE_URL", "http://localhost:8082")


def _crm() -> HubSpotClient:
    return HubSpotClient()


def _context_for(contact_id: str) -> Dict[str, str]:
    lead = _crm().get_lead(contact_id)
    return {
        "first_name": (lead.full_name or "there").split()[0],
        "company": lead.company or "your team",
        "industry": lead.industry or "your industry",
        "sender_name": os.environ.get("LQABR_SENDER_NAME", "the LQABR team"),
    }


async def _verified_form(request: Request) -> Dict[str, str]:
    form = {k: str(v) for k, v in (await request.form()).items()}
    signature = request.headers.get("X-Twilio-Signature", "")
    if os.environ.get("LQABR_SKIP_TWILIO_SIGNATURE") != "1":
        if not validate_twilio_signature(str(request.url), form, signature):
            raise HTTPException(status_code=401, detail="invalid Twilio signature")
    return form


def _record(event_type: EventType, contact_id: str, detail: str | None = None) -> None:
    try:
        _crm().record_event(EngagementEvent(event_type=event_type,
                                            hubspot_contact_id=contact_id, detail=detail))
    except CRMError:
        # TwiML must still be returned to keep the call alive; log loudly for
        # replay. The event is recoverable from Twilio logs — never lost quietly.
        logger.exception("CRM writeback failed for %s on contact %s", event_type, contact_id)


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/voice/answer")
async def voice_answer(request: Request, contact_id: str) -> Response:
    form = await _verified_form(request)
    answered_by = form.get("AnsweredBy", "unknown")
    context = _context_for(contact_id)

    if answered_by.startswith("machine"):
        # Flow A: voicemail + customized SMS follow-up.
        _record(EventType.VOICEMAIL_LEFT, contact_id, detail=form.get("CallSid"))
        try:
            lead = _crm().get_lead(contact_id)
            if lead.phone:
                TwilioClient().send_sms(
                    to=lead.phone,
                    body=conversation.FOLLOWUP_SMS_TEMPLATE.format(**context),
                    status_callback=f"{BASE_URL}/sms/status?contact_id={contact_id}",
                )
        except (TwilioError, CRMError):
            logger.exception("voicemail SMS follow-up failed for contact %s", contact_id)
        twiml = conversation.twiml_voicemail(context)
    else:
        # Flow B: a human answered — start the Q&A pattern.
        _record(EventType.CALL_ANSWERED, contact_id, detail=form.get("CallSid"))
        twiml = conversation.twiml_question(
            1, context, f"{BASE_URL}/voice/qa?contact_id={contact_id}&step=1")

    return Response(content=twiml, media_type="application/xml")


@app.post("/voice/qa")
async def voice_qa(request: Request, contact_id: str, step: int) -> Response:
    form = await _verified_form(request)
    speech = form.get("SpeechResult")
    context = _context_for(contact_id)

    twiml, result = conversation.next_action(
        step, speech, context,
        action_url_for=lambda s: f"{BASE_URL}/voice/qa?contact_id={contact_id}&step={s}",
    )
    if result and result.engaged:
        _record(EventType.CALL_ENGAGED, contact_id, detail=f"qa completed at step {result.last_step}")
    return Response(content=twiml, media_type="application/xml")


@app.post("/sms/status")
async def sms_status(request: Request, contact_id: str) -> Dict[str, str]:
    form = await _verified_form(request)
    if form.get("MessageStatus") == "delivered":
        _record(EventType.SMS_DELIVERED, contact_id, detail=form.get("MessageSid"))
        return {"status": "recorded"}
    return {"status": "ignored", "message_status": form.get("MessageStatus", "")}
