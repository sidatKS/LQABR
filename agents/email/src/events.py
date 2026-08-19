"""Business logic 2 — the inbound Mailgun event.

Mailgun makes an inbound API call to ``POST /mailgun/events`` carrying the
event and the user-variables the send attached. The only one this needs is
``lqabr_object_id`` — the HubSpot record the event belongs to. The send tagged
every message with it (``outreach.send_one``), and Mailgun echoes it back on
every event that message ever produces, so the event names its own lead.

NO RUN STATE. There is no per-run file, no correlation-token lookup, and no
message-id table. HubSpot is the system of record: the lead's current
``lqabr_email_status`` is read back and is what the incoming event is ranked
against, so a weaker or duplicate event never overwrites a stronger one. The
label travels with the message, not in a local database — which is what makes
this correct on a container that scales to zero between the send and the event.

Write-back lands three things on the HubSpot record, validated against the
same schema used for the read at step 5:

  * ``lqabr_email_status`` — the winning status.
  * ``probability``        — moved only on real engagement, only by the
                             increments ``lqabr_core.probability`` defines.
  * the campaign-complete column — set once the status reaches OPENED; this is
                             the hand-off the text/voice agent reads. Written
                             as a SEPARATE PATCH so a missing/placeholder
                             property name cannot take the status write down
                             with it.

CLICKED collapses to OPENED. The confirmed HubSpot enumeration has no CLICKED
value, so a click is stored as OPENED — and because there is no run state to
remember that a click was already counted, a click is treated as an open for
ranking and scoring too. One rung per engagement level (DELIVERED < OPENED)
keeps a retried event idempotent: the second copy ranks equal to what HubSpot
already holds and writes nothing.
"""

from __future__ import annotations

import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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


class MCPObservability:
    def __init__(self, ctx=None): self.ctx = ctx
    def process(self, **f): _log_process(self.ctx, **f)
    def audit(self, **f): _log_audit(self.ctx, **f)

from lqabr_core.crm import CRMError
from lqabr_core.probability import apply_event
from lqabr_core.types import EventType


# --------------------------------------------------------- Mailgun vocabulary
# The closed Mailgun event vocabulary and its translation onto HubSpot's stored
# status. This lives with the inbound-event logic that ranks and records these;
# outreach.py imports the two names it needs (MailgunEvent, HUBSPOT_EMAIL_STATUS)
# from this module for its terminal-status writes.
#
# Mailgun does not literally emit every one of these on the wire: a hard bounce
# arrives as `failed` + severity:permanent, a refused send as `rejected`.
# `from_mailgun` collapses the wire vocabulary onto the eight values below so the
# rest of the agent reasons about exactly eight. Probability increments are NOT
# defined here — `lqabr_core.probability` is the single source of truth.


class MailgunEvent(str, Enum):
    """The eight-value closed vocabulary. Anything Mailgun sends that does not
    map onto one of these is acknowledged and ignored, never guessed at."""

    DELIVERED = "delivered"
    OPENED = "opened"
    CLICKED = "clicked"
    FAILED = "failed"              # transient failure — Mailgun gave up retrying
    BOUNCED = "bounced"            # permanent failure — address is dead
    COMPLAINED = "complained"      # recipient marked it as spam
    UNSUBSCRIBED = "unsubscribed"  # recipient opted out
    STOPPED = "stopped"            # send refused before it left Mailgun


#: Engagement — these raise probability and keep the run alive.
POSITIVE_EVENTS = frozenset({
    MailgunEvent.DELIVERED, MailgunEvent.OPENED, MailgunEvent.CLICKED,
})

#: A terminal status ends the run after the write-back, with no hand-off.
TERMINAL_EVENTS = frozenset({
    MailgunEvent.FAILED, MailgunEvent.BOUNCED, MailgunEvent.COMPLAINED,
    MailgunEvent.UNSUBSCRIBED, MailgunEvent.STOPPED,
})

#: Which scored EventType each engagement event is. Terminal events have no
#: entry: they are recorded, but they do not move probability.
SCORED_AS: Dict[MailgunEvent, EventType] = {
    MailgunEvent.DELIVERED: EventType.EMAIL_DELIVERED,
    MailgunEvent.OPENED: EventType.EMAIL_OPENED,
    MailgunEvent.CLICKED: EventType.EMAIL_CLICKED,
}

#: `lqabr_email_status` is a confirmed HubSpot enumeration accepting exactly
#: PENDING / SENT / DELIVERED / OPENED / FAILED / BOUNCED. There is no CLICKED
#: option, so a click records as OPENED (a click implies an open). Every
#: unworkable-address terminal — hard bounce, spam complaint, unsubscribe, or a
#: send Mailgun refused — records as FAILED. The internal MailgunEvent still
#: distinguishes BOUNCED (permanent) from FAILED (transient) for precedence;
#: only the HubSpot column collapses them to the single "not workable" FAILED.
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

#: Precedence when two statuses arrive for the same message. Engagement events
#: are asynchronous and out of order — `opened` can land before `delivered` — so
#: the resolved status is the highest-ranked seen so far, never simply the
#: latest. Terminals outrank everything: once an address bounced, a stale
#: `delivered` must not walk the record back.
_PRECEDENCE: Dict[MailgunEvent, int] = {
    MailgunEvent.DELIVERED: 10,
    MailgunEvent.OPENED: 20,
    MailgunEvent.CLICKED: 30,
    MailgunEvent.FAILED: 40,
    MailgunEvent.STOPPED: 50,
    MailgunEvent.UNSUBSCRIBED: 60,
    MailgunEvent.COMPLAINED: 70,
    MailgunEvent.BOUNCED: 80,
}

#: Mailgun wire event name -> closed enum, for the cases that need no extra
#: signal. `failed` is deliberately absent: it needs `severity` to resolve.
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

#: Wire events that are real Mailgun events but carry no meaning for this design
#: — acknowledged and skipped, never treated as unknown/erroneous.
IGNORED_WIRE_EVENTS = frozenset({"accepted", "stored", "list_member_uploaded"})


def from_mailgun(event_name: str, severity: str = "") -> Optional[MailgunEvent]:
    """Translate a raw Mailgun ``event-data.event`` onto the closed enum.

        failed + permanent -> BOUNCED
        failed + anything  -> FAILED

    Returns None for events outside the vocabulary so the caller can
    acknowledge and skip them rather than guessing a status."""
    name = (event_name or "").strip().lower()
    if name == "failed":
        return (MailgunEvent.BOUNCED
                if (severity or "").strip().lower() == "permanent"
                else MailgunEvent.FAILED)
    return _DIRECT.get(name)


def is_terminal(event: MailgunEvent) -> bool:
    """Does this status end the run (after the step-9 write-back)?"""
    return event in TERMINAL_EVENTS


def resolve_status(current: Optional[MailgunEvent],
                   incoming: MailgunEvent) -> Tuple[MailgunEvent, bool]:
    """WHICH STATUS WON — returns ``(winner, changed)``.

    `changed` is False when a lower-ranked event arrives after a higher one (a
    late `delivered` behind a `clicked`, a stale event after a bounce). The
    caller still logs the arrival, but must not write the weaker value back."""
    if current is None:
        return incoming, True
    if _PRECEDENCE[incoming] > _PRECEDENCE[current]:
        return incoming, True
    return current, False

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.hubspot.schema import (  # noqa: E402
    SchemaValidationError,
    campaign_complete_property,
    last_modified_email_property,
)
from mcp.hubspot.server import MCPSession, build_session  # noqa: E402

#: HubSpot's stored ``lqabr_email_status`` -> the MailgunEvent it represents,
#: for ranking an incoming event against what the record already holds. Only
#: the engagement/terminal rungs appear: PENDING and SENT are pre-engagement
#: and map to None ("no prior event"), so the first real event always writes.
_STATUS_TO_EVENT: Dict[str, MailgunEvent] = {
    "DELIVERED": MailgunEvent.DELIVERED,
    "OPENED": MailgunEvent.OPENED,
    "FAILED": MailgunEvent.FAILED,
    "BOUNCED": MailgunEvent.BOUNCED,
}


def _variables(event_data: Dict[str, Any]) -> Dict[str, Any]:
    return event_data.get("user-variables") or event_data.get("user_variables") or {}


def _severity(event_data: Dict[str, Any]) -> str:
    delivery = event_data.get("delivery-status") or event_data.get("delivery_status") or {}
    return str(event_data.get("severity") or delivery.get("severity") or "")


def handle_event(event_data: Dict[str, Any],
                 session: Optional[MCPSession] = None) -> Dict[str, Any]:
    """One inbound Mailgun event.

    Signature verification happens in ``service_app.dispatch_mailgun`` before
    this is called — an unverified event never reaches here.

    Returns a dict whose `status` is `recorded` (written back), `ignored`
    (outside the closed vocabulary), `superseded` (ranked at or below what
    HubSpot already holds), or `unresolved` (flagged, never dropped)."""
    variables = _variables(event_data)
    object_id = str(variables.get("lqabr_object_id") or "")
    run_id = str(variables.get("lqabr_run_id") or "")
    # ctx carries the SEND's run id (echoed back by Mailgun) purely so the four
    # log streams stay correlated with the original send — it is not a lookup
    # key and nothing is read by it.
    ctx = RunContext(object_id=object_id, run_id=run_id) if object_id else None

    raw_event = str(event_data.get("event") or "")
    _log_audit(ctx, step=8, direction="inbound", endpoint="/mailgun/events",
              method="POST", status_code=200, mailgun_event=raw_event)

    # ---------------------------------------------------------- vocabulary
    event = from_mailgun(raw_event, _severity(event_data))
    if event is None:
        if raw_event.lower() not in IGNORED_WIRE_EVENTS:
            _log_process(ctx, step=8, event="event_outside_vocabulary",
                        mailgun_event=raw_event,
                        detail="not in the closed enum — acknowledged and skipped")
        return {"status": "ignored", "event": raw_event}

    # A click is stored, ranked and scored as an open (see module docstring).
    if event is MailgunEvent.CLICKED:
        event = MailgunEvent.OPENED

    if not object_id:
        _log_process(ctx, step=8, event="event_without_object_id",
                    mailgun_event=raw_event,
                    detail="event carried no lqabr_object_id — cannot attribute to a lead")
        return {"status": "unresolved",
                "reason": "event carried no lqabr_object_id", "event": event.value}

    return _write_back(ctx, object_id, event, raw_event, session=session)


def _write_back(ctx: Optional[RunContext], object_id: str, event: MailgunEvent,
                raw_event: str, session: Optional[MCPSession] = None) -> Dict[str, Any]:
    """Land engagement state on the HubSpot record, through the central MCP and
    against the same schema used for the read."""
    mcp_session = session or build_session(obs=MCPObservability(ctx))

    # HubSpot is the system of record: read the lead's current status and
    # probability rather than trusting anything held here.
    try:
        profile = mcp_session.crm.get_lead_profile(object_id)
    except (CRMError, SchemaValidationError) as exc:
        _log_process(ctx, step=9, event="profile_read_failed",
                    object_id=object_id, error=str(exc))
        return {"status": "unresolved", "reason": f"crm-error: {exc}",
                "object_id": object_id, "event": event.value}

    # Rank the incoming event against the status HubSpot already holds. Events
    # arrive out of order and Mailgun retries, so a weaker or duplicate event
    # must not overwrite a stronger one or re-increment probability.
    current = _STATUS_TO_EVENT.get((profile.email_status or "").upper())
    winner, changed = resolve_status(current, event)
    _log_process(ctx, step=8, event="status_resolved", object_id=object_id,
                arrived=event.value, previous=profile.email_status or None,
                winner=winner.value, changed=changed, raw=raw_event)
    if not changed:
        return {"status": "superseded", "event": event.value,
                "current_status": profile.email_status, "object_id": object_id}

    properties: Dict[str, Any] = {"lqabr_email_status": HUBSPOT_EMAIL_STATUS[winner]}

    lm_prop = last_modified_email_property()
    if lm_prop:
        properties[lm_prop] = int(time.time() * 1000)

    new_probability = profile.probability
    if winner in POSITIVE_EVENTS:
        new_probability = apply_event(profile.probability, SCORED_AS[winner])
        properties["probability"] = new_probability

    try:
        mcp_session.crm.patch_object(object_id, properties)
    except (CRMError, SchemaValidationError) as exc:
        _log_process(ctx, step=9, event="writeback_failed", object_id=object_id,
                    error=str(exc), detail="event not recorded — retry expected")
        return {"status": "unresolved", "reason": f"crm-error: {exc}",
                "object_id": object_id, "event": winner.value}

    _log_process(ctx, step=9, event="writeback_applied", object_id=object_id,
                written=properties)

    # THE HAND-OFF: campaign-complete when the status reaches OPENED. A SEPARATE
    # PATCH, deliberately never bundled into the bag above — HubSpot validates a
    # property bag atomically, and `email_campaign_complete` is an unconfirmed
    # placeholder name that 400s with PROPERTY_DOESNT_EXIST where it does not
    # exist; bundled, that 400 would take the perfectly valid status and
    # probability down with it. Idempotent by ranking: once HubSpot holds
    # OPENED, the next open ranks equal and this block is never re-entered.
    email_status = properties["lqabr_email_status"]
    complete_property = campaign_complete_property()
    complete = bool(complete_property and email_status == "OPENED")
    if complete:
        try:
            mcp_session.crm.patch_object(object_id, {complete_property: True})
        except (CRMError, SchemaValidationError) as exc:
            complete = False
            _log_process(
                ctx, step=10, event="campaign_complete_write_failed", object_id=object_id,
                error=str(exc),
                detail=(f"'{complete_property}' could not be written — confirm the real "
                        "property name against the HubSpot schema and set "
                        "LQABR_HUBSPOT_CAMPAIGN_COMPLETE_PROPERTY (or clear it to disable "
                        "this column). lqabr_email_status and probability were still "
                        "written successfully above."))
        else:
            _log_process(ctx, step=10, event="handoff_condition_met", object_id=object_id,
                        detail=(f"{complete_property} set — ownership passes to the "
                                "text/voice agent; the email agent stops acting on this lead"))

    if is_terminal(winner):
        _log_process(ctx, step=9, event="run_ended", object_id=object_id,
                    reason=f"terminal status {winner.value}", handoff=False)

    return {"status": "recorded", "event": winner.value, "object_id": object_id,
            "probability": new_probability,
            "email_status": email_status,
            "campaign_complete": complete,
            "terminal": is_terminal(winner)}
