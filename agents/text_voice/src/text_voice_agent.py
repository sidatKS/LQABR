"""Text/Voice Agent — second outreach stage. Works leads whose probability
was raised by the Email Agent past TEXT_VOICE_THRESHOLD, contacting them by
SMS and voice via Twilio.

    adk web  agents/text_voice/src
    adk run  agents/text_voice/src
    adk api_server agents/text_voice/src    # Cloud Run

Two flows (handled by the webhook service once a call connects):
    Flow A — lead doesn't answer: a customized voicemail is delivered, then a
             customized SMS follow-up (VOICEMAIL_LEFT / SMS_DELIVERED events).
    Flow B — lead answers: a specific conversational question-and-answer
             pattern runs (conversation.py). CALL_ANSWERED is recorded, and
             completing the flow with interest adds CALL_ENGAGED — the
             counters increment in HubSpot and probability rises toward
             SCHEDULING_THRESHOLD, promoting the lead to the Scheduling Agent.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from google.adk.agents import Agent

from lqabr_core.crm import HubSpotClient
from lqabr_core.probability import SCHEDULING_THRESHOLD, TEXT_VOICE_THRESHOLD
from lqabr_core.types import LeadStage

try:
    from . import conversation
    from .twilio_client import TwilioClient, TwilioError
except ImportError:  # pragma: no cover
    import conversation  # type: ignore
    from twilio_client import TwilioClient, TwilioError  # type: ignore

MODEL = os.environ.get("LQABR_TEXT_VOICE_MODEL", "gemini-2.0-flash")
BASE_URL = os.environ.get("LQABR_WEBHOOK_BASE_URL", "http://localhost:8082")


def list_text_voice_queue(limit: int = 25) -> Dict[str, Any]:
    """Leads promoted to the text_voice_outreach stage (probability >=
    TEXT_VOICE_THRESHOLD) — this agent's work queue."""
    leads = HubSpotClient().leads_in_stage(LeadStage.TEXT_VOICE_OUTREACH, limit=limit)
    return {"count": len(leads),
            "entry_threshold": TEXT_VOICE_THRESHOLD,
            "threshold_to_scheduling": SCHEDULING_THRESHOLD,
            "leads": [l.to_dict() for l in leads]}


def send_text(contact_email: str, body: str = "") -> Dict[str, Any]:
    """Send a customized SMS to a lead via Twilio.

    Args:
        contact_email: the lead's email (HubSpot lookup key).
        body: optional custom message; {placeholders} allowed (first_name,
            company, industry, sender_name). Defaults to the follow-up
            template.

    Returns {"status": "queued", ...} — the delivery receipt arrives on the
    webhook and records SMS_DELIVERED (+probability).
    """
    crm = HubSpotClient()
    lead = crm.find_lead_by_email(contact_email)
    if lead is None or not lead.hubspot_contact_id:
        return {"error": f"no HubSpot contact for {contact_email}"}
    if not lead.phone:
        return {"error": f"lead {lead.hubspot_contact_id} has no phone listed"}

    context = {
        "first_name": (lead.full_name or "there").split()[0],
        "company": lead.company or "your team",
        "industry": lead.industry or "your industry",
        "sender_name": os.environ.get("LQABR_SENDER_NAME", "the LQABR team"),
    }
    text = (body or conversation.FOLLOWUP_SMS_TEMPLATE).format(**context)
    try:
        message = TwilioClient().send_sms(
            to=lead.phone, body=text,
            status_callback=f"{BASE_URL}/sms/status?contact_id={lead.hubspot_contact_id}",
        )
    except TwilioError as exc:
        return {"error": f"twilio-error: {exc}", "contact_id": lead.hubspot_contact_id}
    return {"status": "queued", "message_sid": message.get("sid"),
            "contact_id": lead.hubspot_contact_id, "to": lead.phone}


def place_outreach_call(contact_email: str) -> Dict[str, Any]:
    """Place an outbound call to a lead via Twilio.

    Answering-machine detection routes the call automatically:
    a human answer starts the conversational Q&A pattern; voicemail gets the
    customized voicemail message plus an SMS follow-up. All engagement
    (answered / voicemail / engaged) is recorded on the HubSpot contact by
    the webhook and increments the lead's probability counters.
    """
    crm = HubSpotClient()
    lead = crm.find_lead_by_email(contact_email)
    if lead is None or not lead.hubspot_contact_id:
        return {"error": f"no HubSpot contact for {contact_email}"}
    if not lead.phone:
        return {"error": f"lead {lead.hubspot_contact_id} has no phone listed"}

    answer_url = f"{BASE_URL}/voice/answer?contact_id={lead.hubspot_contact_id}"
    try:
        call = TwilioClient().place_call(to=lead.phone, answer_url=answer_url)
    except TwilioError as exc:
        return {"error": f"twilio-error: {exc}", "contact_id": lead.hubspot_contact_id}
    return {"status": "initiated", "call_sid": call.get("sid"),
            "contact_id": lead.hubspot_contact_id, "to": lead.phone}


root_agent = Agent(
    name="text_voice_agent",
    model=MODEL,
    description=("LQABR Text/Voice Agent: contacts probability-incremented "
                 "leads by SMS and voice call via Twilio. Handles voicemail + "
                 "text delivery when unanswered, and a conversational Q&A "
                 "pattern when the lead answers."),
    instruction=(
        "You are the LQABR text/voice outreach agent. You only work leads that "
        "the Email Agent has already warmed past the probability threshold — "
        "use list_text_voice_queue to see them.\n\n"
        "Use place_outreach_call to call a lead (voicemail and answered-call "
        "flows are automatic) and send_text for SMS. You may customize the SMS "
        "body with the lead's profile fields; keep it short, honest, and "
        "include an opt-out (Reply STOP).\n\n"
        "Never invent a phone number: only contact leads returned from "
        "HubSpot. Report Twilio or CRM errors verbatim — a failed contact "
        "attempt is never ignored. Engagement (answered, voicemail, engaged) "
        "is recorded by the webhook; do not fabricate call outcomes."
    ),
    tools=[list_text_voice_queue, send_text, place_outreach_call],
)
