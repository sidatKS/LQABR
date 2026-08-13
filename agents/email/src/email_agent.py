"""Email Agent — first outreach stage. Contacts leads that have listed email
IDs via Mailgun; engagement (delivered / read / clicked) is recorded back to
HubSpot by the webhook (webhook_app.py) and raises the lead's probability.

    adk web  agents/email/src
    adk run  agents/email/src
    adk api_server agents/email/src     # Cloud Run

Sequence per lead:
    1. orchestrator (or operator) asks this agent to work leads in stage
       `email_outreach`
    2. send_outreach_email pulls the lead's 9-pointer profile from HubSpot,
       personalizes the template, sends via Mailgun with open/click tracking
       and the hubspot_contact_id attached as a message variable
    3. Mailgun events (delivered/opened/clicked) hit the webhook, which
       increments the HubSpot counters and probability
    4. once probability >= TEXT_VOICE_THRESHOLD the lead is promoted to the
       Text/Voice Agent
"""

from __future__ import annotations

import os
from typing import Any, Dict

from google.adk.agents import Agent

from lqabr_core.crm import CRMError, HubSpotClient
from lqabr_core.probability import TEXT_VOICE_THRESHOLD
from lqabr_core.types import LeadStage

from lqabr_core.mailgun import MailgunClient, MailgunError

MODEL = os.environ.get("LQABR_EMAIL_MODEL", "gemini-2.0-flash")

DEFAULT_SUBJECT = "Quick question for {company}"
DEFAULT_HTML = """\
<p>Hi {first_name},</p>
<p>I'm reaching out because {company} came up in our research on {industry}
teams. Given your role as {job_title}, I thought this was worth a short note.</p>
<p>We help teams like yours qualify and engage prospects automatically —
would a quick overview be useful? <a href="{cta_url}">Here's a two-minute
summary</a>.</p>
<p>If now isn't the right time, no problem at all — just let me know.</p>
<p>Best,<br/>{sender_name}</p>
"""


def list_email_queue(limit: int = 25) -> Dict[str, Any]:
    """Leads currently in the email_outreach stage (this agent's work queue),
    with their probability and engagement counters."""
    leads = HubSpotClient().leads_in_stage(LeadStage.EMAIL_OUTREACH, limit=limit)
    return {"count": len(leads), "threshold_to_text_voice": TEXT_VOICE_THRESHOLD,
            "leads": [l.to_dict() for l in leads]}


def send_outreach_email(contact_email: str,
                        subject: str = "",
                        html_body: str = "",
                        cta_url: str = "") -> Dict[str, Any]:
    """Send one personalized outreach email to a lead via Mailgun.

    Args:
        contact_email: the lead's email (must exist as a HubSpot contact).
        subject: optional custom subject; {placeholders} allowed.
        html_body: optional custom HTML body; {placeholders} allowed
            (first_name, full_name, company, job_title, industry, cta_url,
            sender_name).
        cta_url: link for the call-to-action (clicks on it are tracked and
            raise the lead's probability).

    Returns:
        {"status": "sent", "mailgun_id": ..., "contact_id": ...} or
        {"error": ...}. The delivered/opened/clicked engagement is recorded
        asynchronously by the Mailgun webhook — not here.
    """
    crm = HubSpotClient()
    lead = crm.find_lead_by_email(contact_email)
    if lead is None or not lead.hubspot_contact_id:
        return {"error": f"no HubSpot contact for {contact_email} — ingest the lead first"}
    if not lead.email:
        return {"error": f"lead {lead.hubspot_contact_id} has no email listed"}

    context = {
        "first_name": (lead.full_name or "there").split()[0],
        "full_name": lead.full_name or "there",
        "company": lead.company or "your team",
        "job_title": lead.job_title or "your role",
        "industry": lead.industry or "your industry",
        "cta_url": cta_url or os.environ.get("LQABR_CTA_URL", "https://example.com/overview"),
        "sender_name": os.environ.get("LQABR_SENDER_NAME", "The LQABR Team"),
    }
    subject_text = (subject or DEFAULT_SUBJECT).format(**context)
    html = (html_body or DEFAULT_HTML).format(**context)

    try:
        sent = MailgunClient().send_email(
            to=lead.email, subject=subject_text, html=html,
            tags=["lqabr", "email-outreach"],
            variables={"hubspot_contact_id": lead.hubspot_contact_id},
        )
    except MailgunError as exc:
        return {"error": f"mailgun-error: {exc}", "contact_id": lead.hubspot_contact_id}

    try:
        if lead.stage in (LeadStage.PROFILED, LeadStage.INGESTED):
            crm.set_stage(lead.hubspot_contact_id, LeadStage.EMAIL_OUTREACH,
                          reason="first outreach email sent")
    except CRMError as exc:
        # The email went out; the stage writeback must not vanish silently.
        return {"status": "sent", "warning": f"stage writeback failed: {exc}",
                "mailgun_id": sent.get("id"), "contact_id": lead.hubspot_contact_id}

    return {"status": "sent", "mailgun_id": sent.get("id"),
            "contact_id": lead.hubspot_contact_id, "to": lead.email}


root_agent = Agent(
    name="email_agent",
    model=MODEL,
    description=("LQABR Email Agent: sends personalized outreach email via "
                 "Mailgun to leads with listed email IDs; delivery, opens, and "
                 "clicked links are tracked and raise the lead's probability "
                 "in HubSpot."),
    instruction=(
        "You are the LQABR email outreach agent. Your job is to contact lead "
        "profiles that have listed email IDs.\n\n"
        "Use list_email_queue to see which leads are in your stage. Use "
        "send_outreach_email to contact a lead — you may craft a personalized "
        "subject and HTML body per lead using its profile fields (placeholders: "
        "{first_name}, {company}, {job_title}, {industry}, {cta_url}, "
        "{sender_name}). Keep messages short, professional, and honest.\n\n"
        "Never invent an email address: only contact leads returned from "
        "HubSpot. Report Mailgun or CRM errors verbatim — a failed send is "
        "never ignored. Delivery/open/click engagement is recorded by the "
        "webhook automatically; do not fabricate engagement results."
    ),
    tools=[list_email_queue, send_outreach_email],
)
