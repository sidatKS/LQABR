"""Scheduling Agent — final outreach stage. Leads whose probability was
raised past SCHEDULING_THRESHOLD by the Text/Voice Agent are contacted via
email with available schedules across EST / CST / PST / IST, using Zoom
Scheduler booking links.

    adk web  agents/scheduling/src
    adk run  agents/scheduling/src
    adk api_server agents/scheduling/src    # Cloud Run

When the lead books through the Zoom Scheduler link, Zoom's event webhook
(webhook_app.py) records MEETING_SCHEDULED on the HubSpot contact — pinning
probability at MEETING_SCHEDULED_PROBABILITY and moving the lead to the
meeting_scheduled stage for rep handoff.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from google.adk.agents import Agent

from lqabr_core.crm import HubSpotClient
from lqabr_core.mailgun import MailgunClient, MailgunError
from lqabr_core.probability import MEETING_SCHEDULED_PROBABILITY, SCHEDULING_THRESHOLD
from lqabr_core.timezones import zone_options
from lqabr_core.types import LeadStage

try:
    from .zoom_client import ZoomError, ZoomSchedulerClient
except ImportError:  # pragma: no cover
    from zoom_client import ZoomError, ZoomSchedulerClient  # type: ignore

MODEL = os.environ.get("LQABR_SCHEDULING_MODEL", "gemini-2.0-flash")

INVITE_SUBJECT = "Pick a time that works for you, {first_name}"
INVITE_HTML = """\
<p>Hi {first_name},</p>
<p>Great connecting with you! As promised, here's a link to grab a time with
our team about {company}:</p>
<p><a href="{booking_url}"><strong>Choose a time &rarr;</strong></a></p>
<p>Times are available in whichever zone suits you best:</p>
<ul>
{zone_rows}
</ul>
<p>Looking forward to it,<br/>{sender_name}</p>
"""


def list_scheduling_queue(limit: int = 25) -> Dict[str, Any]:
    """Leads promoted to the scheduling stage (probability >=
    SCHEDULING_THRESHOLD) — this agent's work queue."""
    leads = HubSpotClient().leads_in_stage(LeadStage.SCHEDULING, limit=limit)
    return {"count": len(leads),
            "entry_threshold": SCHEDULING_THRESHOLD,
            "meeting_scheduled_probability": MEETING_SCHEDULED_PROBABILITY,
            "leads": [l.to_dict() for l in leads]}


def send_schedule_invite(contact_email: str, schedule_id: str = "") -> Dict[str, Any]:
    """Email a lead the Zoom Scheduler booking link with EST/CST/PST/IST options.

    Args:
        contact_email: the lead's email (HubSpot lookup key).
        schedule_id: optional specific Zoom Scheduler schedule to use;
            defaults to the account's configured/first schedule.

    Returns {"status": "sent", ...} or {"error": ...}. The actual booking is
    recorded asynchronously by the Zoom event webhook (MEETING_SCHEDULED).
    """
    crm = HubSpotClient()
    lead = crm.find_lead_by_email(contact_email)
    if lead is None or not lead.hubspot_contact_id:
        return {"error": f"no HubSpot contact for {contact_email}"}
    if not lead.email:
        return {"error": f"lead {lead.hubspot_contact_id} has no email listed"}

    try:
        booking_url = ZoomSchedulerClient().booking_link(schedule_id or None)
    except ZoomError as exc:
        return {"error": f"zoom-error: {exc}", "contact_id": lead.hubspot_contact_id}

    zones = zone_options()
    zone_rows = "\n".join(
        f"<li><strong>{z.label}</strong> ({z.iana}) — e.g. {z.local_time}</li>" for z in zones
    )
    context = {
        "first_name": (lead.full_name or "there").split()[0],
        "company": lead.company or "your team",
        "booking_url": booking_url,
        "zone_rows": zone_rows,
        "sender_name": os.environ.get("LQABR_SENDER_NAME", "The LQABR Team"),
    }
    try:
        sent = MailgunClient().send_email(
            to=lead.email,
            subject=INVITE_SUBJECT.format(**context),
            html=INVITE_HTML.format(**context),
            tags=["lqabr", "scheduling"],
            variables={"hubspot_contact_id": lead.hubspot_contact_id},
        )
    except MailgunError as exc:
        return {"error": f"mailgun-error: {exc}", "contact_id": lead.hubspot_contact_id}

    return {"status": "sent", "mailgun_id": sent.get("id"),
            "contact_id": lead.hubspot_contact_id, "to": lead.email,
            "booking_url": booking_url,
            "timezones_offered": [z.label for z in zones]}


root_agent = Agent(
    name="scheduling_agent",
    model=MODEL,
    description=("LQABR Scheduling Agent: emails probability-incremented leads "
                 "Zoom Scheduler booking links with EST/CST/PST/IST options; "
                 "booked meetings are recorded on the HubSpot contact."),
    instruction=(
        "You are the LQABR scheduling agent — the final outreach stage. You "
        "only work leads the Text/Voice Agent has warmed past the scheduling "
        "threshold; use list_scheduling_queue to see them.\n\n"
        "Use send_schedule_invite to email a lead the booking link. The invite "
        "always offers EST, CST, PST, and IST time zones. Keep any extra copy "
        "brief and courteous.\n\n"
        "Never invent contact details: only email leads returned from HubSpot. "
        "Report Zoom, Mailgun, or CRM errors verbatim — a failed invite is "
        "never ignored. Bookings are recorded by the Zoom webhook; do not "
        "fabricate meeting confirmations."
    ),
    tools=[list_scheduling_queue, send_schedule_invite],
)
