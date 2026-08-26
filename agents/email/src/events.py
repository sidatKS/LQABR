"""The inbound Mailgun event — step 8 (event in) and step 9 (HubSpot write-back).

Mailgun POSTs every engagement event for a message to ``/mailgun/events``
carrying the user-variables the send attached. The only one this needs is
``lqabr_object_id`` — the HubSpot record the event belongs to. The send tagged
every message with it (``outreach.send_one``) and Mailgun echoes it on every
event that message ever produces, so the event names its own lead.

NO RUN STATE. There is no per-run file, no correlation-token lookup and no
message-id table. HubSpot is the system of record: the lead's current
``lqabr_email_status`` is read back and the incoming event is ranked against
it, so a weaker or duplicate event never overwrites a stronger one. The label
travels with the message, not in a local database — which is what makes this
correct on a container that scales to zero between the send and the event.

Write-back lands three things on the HubSpot record, validated against the
same schema used for the read at step 5:

  * ``lqabr_email_status``  — the winning status.
  * ``probability``         — moved only on real engagement, only by the
                              increments ``lqabr_core.probability`` defines.
  * the campaign-complete column — set once the status reaches OPENED; this
                              is the hand-off the text/voice agent reads.
                              Written as a SEPARATE PATCH so a missing or
                              placeholder property name cannot take the
                              status write down with it.

CLICKED collapses to OPENED: the confirmed HubSpot enumeration has no CLICKED
value, and with no run state to remember that a click was already counted, a
click is ranked and scored as an open too. One rung per engagement level
(DELIVERED < OPENED) keeps a retried event idempotent.
"""

from __future__ import annotations

import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from outreach import MCPObservability, RunContext, log_audit, log_process

from lqabr_core.crm import CRMError
from lqabr_core.probability import apply_event
from lqabr_core.types import EventType

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.hubspot.schema import (  # noqa: E402
    SchemaValidationError, campaign_complete_property, last_modified_email_property,
)
from mcp.hubspot.server import MCPSession, build_session  # noqa: E402

EVENTS_ROUTE = "/mailgun/events"


# ------------------------------------------------------ the closed vocabulary
class MailgunEvent(str, Enum):
    """The eight values the agent reasons about. Anything Mailgun sends that
    does not map onto one of these is acknowledged and ignored, never guessed."""

    DELIVERED = "delivered"
    OPENED = "opened"
    CLICKED = "clicked"
    FAILED = "failed"              # transient failure — Mailgun gave up retrying
    BOUNCED = "failed"            # permanent failure — address is dead
    COMPLAINED = "complained"      # recipient marked it as spam
    UNSUBSCRIBED = "unsubscribed"  # recipient opted out
    STOPPED = "stopped"            # send refused before it left Mailgun


#: Engagement — these raise probability.
POSITIVE_EVENTS = frozenset({MailgunEvent.DELIVERED, MailgunEvent.OPENED, MailgunEvent.CLICKED})

#: A terminal status ends the run after the write-back, with no hand-off.
TERMINAL_EVENTS = frozenset({
    MailgunEvent.FAILED, MailgunEvent.BOUNCED, MailgunEvent.COMPLAINED,
    MailgunEvent.UNSUBSCRIBED, MailgunEvent.STOPPED,
})

#: Which scored EventType each engagement event is. Terminal events have no
#: entry: they are recorded but do not move probability. Increments themselves
#: live only in ``lqabr_core.probability``.
SCORED_AS: Dict[MailgunEvent, EventType] = {
    MailgunEvent.DELIVERED: EventType.EMAIL_DELIVERED,
    MailgunEvent.OPENED: EventType.EMAIL_OPENED,
    MailgunEvent.CLICKED: EventType.EMAIL_CLICKED,
}

#: ``lqabr_email_status`` is a confirmed HubSpot enumeration accepting exactly
#: PENDING / SENT / DELIVERED / OPENED / FAILED / BOUNCED. No CLICKED option, so
#: a click records as OPENED; every unworkable-address terminal records as the
#: single "not workable" FAILED (``outreach.FAILED_STATUS`` writes the same).
HUBSPOT_EMAIL_STATUS: Dict[MailgunEvent, str] = {
    MailgunEvent.DELIVERED: "DELIVERED",
    MailgunEvent.OPENED: "OPENED",
    MailgunEvent.CLICKED: "OPENED",
    MailgunEvent.FAILED: "FAILED",
    MailgunEvent.BOUNCED: "FAILED",
    MailgunEvent.COMPLAINED: "FAILED",
    MailgunEvent.UNSUBSCRIBED: "FAILED",
    MailgunEvent.STOPPED: "FAILED",
}

#: Precedence when two statuses arrive for the same message. Events are
#: asynchronous and out of order — `opened` can land before `delivered` — so
#: the resolved status is the highest-ranked seen, never simply the latest.
#: Terminals outrank everything: once an address bounced, a stale `delivered`
#: must not walk the record back.
_PRECEDENCE: Dict[MailgunEvent, int] = {
    MailgunEvent.DELIVERED: 10, MailgunEvent.OPENED: 20, MailgunEvent.CLICKED: 30,
    MailgunEvent.FAILED: 40, MailgunEvent.STOPPED: 50, MailgunEvent.UNSUBSCRIBED: 60,
    MailgunEvent.COMPLAINED: 70, MailgunEvent.BOUNCED: 80,
}

#: Mailgun wire event -> closed enum, for the cases needing no extra signal.
#: `failed` is deliberately absent: it needs `severity` to resolve.
_DIRECT: Dict[str, MailgunEvent] = {
    "delivered": MailgunEvent.DELIVERED,
    "opened": MailgunEvent.OPENED,
    "clicked": MailgunEvent.CLICKED,
    "complained": MailgunEvent.COMPLAINED,
    "unsubscribed": MailgunEvent.UNSUBSCRIBED,
    "rejected": MailgunEvent.STOPPED,
    "stopped": MailgunEvent.STOPPED,
    "bounced": MailgunEvent.BOUNCED,
}

#: Real Mailgun events that carry no meaning for this design — acknowledged
#: and skipped, never treated as unknown.
IGNORED_WIRE_EVENTS = frozenset({"accepted", "stored", "list_member_uploaded"})

#: HubSpot's stored status -> the event it represents, for ranking an incoming
#: event against what the record already holds. PENDING and SENT are
#: pre-engagement and map to None, so the first real event always writes.
_STATUS_TO_EVENT: Dict[str, MailgunEvent] = {
    "DELIVERED": MailgunEvent.DELIVERED,
    "OPENED": MailgunEvent.OPENED,
    "FAILED": MailgunEvent.FAILED,
    "BOUNCED": MailgunEvent.BOUNCED,
}


def from_mailgun(event_name: str, severity: str = "") -> Optional[MailgunEvent]:
    """Translate a raw ``event-data.event`` onto the closed enum.

        failed + permanent -> BOUNCED
        failed + anything  -> FAILED

    None for events outside the vocabulary, so the caller acknowledges and
    skips them rather than guessing a status."""
    name = (event_name or "").strip().lower()
    if name == "failed":
        return (MailgunEvent.BOUNCED if (severity or "").strip().lower() == "permanent"
                else MailgunEvent.FAILED)
    return _DIRECT.get(name)


def is_terminal(event: MailgunEvent) -> bool:
    return event in TERMINAL_EVENTS


def resolve_status(current: Optional[MailgunEvent],
                   incoming: MailgunEvent) -> Tuple[MailgunEvent, bool]:
    """Which status won — ``(winner, changed)``. ``changed`` is False when a
    lower-ranked event arrives after a higher one; the caller logs the arrival
    but must not write the weaker value back."""
    if current is None or _PRECEDENCE[incoming] > _PRECEDENCE[current]:
        return incoming, True
    return current, False


# ------------------------------------------------------------- the payload
def _variables(event_data: Dict[str, Any]) -> Dict[str, Any]:
    return event_data.get("user-variables") or event_data.get("user_variables") or {}


def _severity(event_data: Dict[str, Any]) -> str:
    delivery = event_data.get("delivery-status") or event_data.get("delivery_status") or {}
    return str(event_data.get("severity") or delivery.get("severity") or "")


# --------------------------------------------------------------- step 8
def handle_event(event_data: Dict[str, Any],
                 session: Optional[MCPSession] = None) -> Dict[str, Any]:
    """One inbound Mailgun event. Signature verification happens in
    ``service_app.dispatch_mailgun`` before this is called.

    Returns a dict whose ``status`` is ``recorded`` (written back), ``ignored``
    (outside the vocabulary), ``superseded`` (ranked at or below what HubSpot
    already holds) or ``unresolved`` (flagged, never dropped)."""
    variables = _variables(event_data)
    objectId = str(variables.get("lqabr_object_id") or "")
    run_id = str(variables.get("lqabr_run_id") or "")
    # The SEND's run id, echoed back by Mailgun, purely so the logs correlate
    # with the original send — not a lookup key; nothing is read by it.
    ctx = RunContext(objectId=objectId, run_id=run_id) if objectId else None

    raw_event = str(event_data.get("event") or "")
    log_audit(ctx, step=8, direction="inbound", endpoint=EVENTS_ROUTE,
              method="POST", status_code=200, mailgun_event=raw_event)

    event = from_mailgun(raw_event, _severity(event_data))
    if event is None:
        if raw_event.lower() not in IGNORED_WIRE_EVENTS:
            log_process(ctx, step=8, event="event_outside_vocabulary", mailgun_event=raw_event,
                        detail="not in the closed enum — acknowledged and skipped")
        return {"status": "ignored", "event": raw_event}

    if event is MailgunEvent.CLICKED:  # stored, ranked and scored as an open
        event = MailgunEvent.OPENED

    if not objectId:
        log_process(ctx, step=8, event="event_without_objectId", mailgun_event=raw_event,
                    detail="event carried no lqabr_object_id — cannot attribute to a lead")
        return {"status": "unresolved", "reason": "event carried no lqabr_object_id",
                "event": event.value}

    return write_back(ctx, objectId, event, raw_event, session=session)


# --------------------------------------------------------------- step 9
def write_back(ctx: Optional[RunContext], objectId: str, event: MailgunEvent,
               raw_event: str, session: Optional[MCPSession] = None) -> Dict[str, Any]:
    """Land engagement state on the HubSpot record, through the central MCP
    and against the same schema used for the read."""
    mcp_session = session or build_session(obs=MCPObservability(ctx))

    # HubSpot is the system of record: read the lead's current status and
    # probability rather than trusting anything held here.
    try:
        profile = mcp_session.crm.get_lead_profile(objectId)
    except (CRMError, SchemaValidationError) as exc:
        log_process(ctx, step=9, event="profile_read_failed", objectId=objectId, error=str(exc))
        return {"status": "unresolved", "reason": f"crm-error: {exc}",
                "objectId": objectId, "event": event.value}

    # Rank against what HubSpot holds: events arrive out of order and Mailgun
    # retries, so a weaker/duplicate event must not overwrite or re-score.
    current = _STATUS_TO_EVENT.get((profile.email_status or "").upper())
    winner, changed = resolve_status(current, event)
    log_process(ctx, step=8, event="status_resolved", objectId=objectId,
                arrived=event.value, previous=profile.email_status or None,
                winner=winner.value, changed=changed, raw=raw_event)
    if not changed:
        return {"status": "superseded", "event": event.value,
                "current_status": profile.email_status, "objectId": objectId}

    email_status = HUBSPOT_EMAIL_STATUS[winner]
    properties: Dict[str, Any] = {"lqabr_email_status": email_status}
    lm_prop = last_modified_email_property()
    if lm_prop:
        properties[lm_prop] = int(time.time() * 1000)
    new_probability = profile.probability
    if winner in POSITIVE_EVENTS:
        new_probability = apply_event(profile.probability, SCORED_AS[winner])
        properties["probability"] = new_probability

    try:
        mcp_session.crm.patch_object(objectId, properties)
    except (CRMError, SchemaValidationError) as exc:
        log_process(ctx, step=9, event="writeback_failed", objectId=objectId,
                    error=str(exc), detail="event not recorded — retry expected")
        return {"status": "unresolved", "reason": f"crm-error: {exc}",
                "objectId": objectId, "event": winner.value}
    log_process(ctx, step=9, event="writeback_applied", objectId=objectId, written=properties)

    complete = _mark_campaign_complete(ctx, mcp_session, objectId, email_status)

    if is_terminal(winner):
        log_process(ctx, step=9, event="run_ended", objectId=objectId,
                    reason=f"terminal status {winner.value}", handoff=False)

    return {"status": "recorded", "event": winner.value, "objectId": objectId,
            "probability": new_probability, "email_status": email_status,
            "campaign_complete": complete, "terminal": is_terminal(winner)}


# -------------------------------------------------------------- step 10
def _mark_campaign_complete(ctx: Optional[RunContext], mcp_session: MCPSession,
                            objectId: str, email_status: str) -> bool:
    """THE HAND-OFF: campaign-complete once the status reaches OPENED.

    A SEPARATE PATCH, deliberately never bundled with the status write —
    HubSpot validates a property bag atomically, and the campaign-complete
    name is an unconfirmed placeholder that 400s where it does not exist;
    bundled, that 400 would take the valid status and probability down with
    it. Idempotent by ranking: once HubSpot holds OPENED the next open ranks
    equal and this is never re-entered."""
    prop = campaign_complete_property()
    if not (prop and email_status == "OPENED"):
        return False
    try:
        mcp_session.crm.patch_object(objectId, {prop: True})
    except (CRMError, SchemaValidationError) as exc:
        log_process(ctx, step=10, event="campaign_complete_write_failed", objectId=objectId,
                    error=str(exc),
                    detail=(f"'{prop}' could not be written — confirm the real property "
                            "name against the HubSpot schema and set "
                            "LQABR_HUBSPOT_CAMPAIGN_COMPLETE_PROPERTY (or clear it to "
                            "disable this column). lqabr_email_status and probability "
                            "were still written successfully."))
        return False
    log_process(ctx, step=10, event="handoff_condition_met", objectId=objectId,
                detail=(f"{prop} set — ownership passes to the text/voice agent; "
                        "the email agent stops acting on this lead"))
    return True


__all__ = [
    "EVENTS_ROUTE", "MailgunEvent", "POSITIVE_EVENTS", "TERMINAL_EVENTS", "SCORED_AS",
    "HUBSPOT_EMAIL_STATUS", "IGNORED_WIRE_EVENTS", "from_mailgun", "is_terminal",
    "resolve_status", "handle_event", "write_back",
]