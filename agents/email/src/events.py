"""Business logic 2 — steps 8 and 9. Asynchronous, matched by token.

    STEP 8  Receive engagement events   (matched by correlation token)
    STEP 9  Write the status back       (same central MCP as the read)

Delivered arrives first; opened and clicked may arrive hours or days later.
The correlation token Mailgun echoes back is what attaches an event to the
right lead however long after the send it lands, so events never collide
across leads.

The write-back lands `lqabr_email_status`, `probability` and the
campaign-complete column on the HubSpot record, validating against the same
schema used for the read at step 5. **The campaign-complete column is set
when `lqabr_email_status` reaches OPENED** (confirmed 2026-08-04) — and
because the HubSpot enumeration has no CLICKED value, a click maps to OPENED
too, so one rule covers both. That column alone is what step 10 reads;
probability is still written, for reporting only.

A terminal status ends the run after the write-back, with no hand-off.

TWO WAYS IN, ONE IMPLEMENTATION
-------------------------------
Mailgun events reach step 8 either way, and `handle_event` cannot tell which:

  PUSH       Mailgun posts to the agent's OWN endpoint — the same service the
             gateway reaches (`POST /mailgun/events` on service_app).
  TOOL CALL  `sync_run_engagement()` asks Mailgun for the status of a run's
             track IDs through the mailgun tool, once, inside a run that a
             trigger already started.

Platform Engineering, 2026-08-04:

  Swaroop 15:08 — "you have to give the endpoint in the mail gun... your
                   e-mail agent endpoint, as in gateway is reaching, and then
                   your mail gun has to send back the event triggers back to
                   the SAME agent."
  Swaroop 15:29 — "once the request comes in, what happens to that request is
                   a business logic WITHIN the agent."
  Swaroop 20:27 — "you fetch one pull saying that these are the track IDs,
                   give me the status of those track IDs that I got a trigger
                   from... if there is anything update, you are going to put it."

Both are on-demand. Neither polls. There is no scheduler in this module and
none may be added: the container exists only for the length of a run and
then goes back to zero instances.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

import observability as obs
from enums import (
    HUBSPOT_EMAIL_STATUS,
    IGNORED_WIRE_EVENTS,
    POSITIVE_EVENTS,
    SCORED_AS,
    MailgunEvent,
    from_mailgun,
    is_terminal,
    resolve_status,
)
from runstate import RunStateStore

from lqabr_core.crm import CRMError
from lqabr_core.mailgun import MailgunError
from lqabr_core.probability import apply_event

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.hubspot.schema import (  # noqa: E402
    SchemaValidationError,
    campaign_complete_property,
)
from mcp.hubspot.server import MCPSession, build_session  # noqa: E402


def _variables(event_data: Dict[str, Any]) -> Dict[str, Any]:
    return event_data.get("user-variables") or event_data.get("user_variables") or {}


def _severity(event_data: Dict[str, Any]) -> str:
    delivery = event_data.get("delivery-status") or event_data.get("delivery_status") or {}
    return str(event_data.get("severity") or delivery.get("severity") or "")


def _message_id(event_data: Dict[str, Any]) -> str:
    message = event_data.get("message") or {}
    headers = message.get("headers") or {}
    return str(headers.get("message-id") or message.get("id") or "")


def handle_event(event_data: Dict[str, Any],
                 session: Optional[MCPSession] = None,
                 store: Optional[RunStateStore] = None) -> Dict[str, Any]:
    """STEPS 8 + 9 for one Mailgun event.

    Signature verification happens in `service_app.dispatch_mailgun`
    before this is called — an unverified event never reaches here.

    Returns a dict with a `status` of: `recorded` (written back),
    `ignored` (outside the closed vocabulary), `superseded` (a weaker status
    arrived after a stronger one) or `unresolved` (flagged, never dropped)."""
    store = store or RunStateStore()

    raw_event = str(event_data.get("event") or "")
    variables = _variables(event_data)
    token = str(variables.get("lqabr_correlation_token") or "")
    ctx = obs.parse_correlation_token(token)

    obs.audit(ctx, step=8, direction="inbound", endpoint="/mailgun/events",
              method="POST", status_code=200, mailgun_event=raw_event)

    # ---------------------------------------------------------- vocabulary
    event = from_mailgun(raw_event, _severity(event_data))
    if event is None:
        if raw_event.lower() not in IGNORED_WIRE_EVENTS:
            obs.process(ctx, step=8, event="event_outside_vocabulary",
                        mailgun_event=raw_event,
                        detail="not in the closed enum — acknowledged and skipped")
        return {"status": "ignored", "event": raw_event}

    # -------------------------------------------------- match by token (8)
    if ctx is None:
        # No correlation token at all. Fall back to the contact id the send
        # also attaches, so a real event is never thrown away over a
        # missing variable — but say so loudly.
        object_id = str(variables.get("lqabr_object_id") or "")
        obs.process(ctx, step=8, event="event_without_correlation_token",
                    mailgun_event=raw_event, object_id=object_id or None,
                    detail="run state cannot be matched; see 'no path today' in the design")
        if not object_id:
            return {"status": "unresolved",
                    "reason": "event carried neither a correlation token nor a contact id",
                    "event": event.value}
        return _write_back(None, object_id, event, record=None, store=store,
                           session=session, run_object_id="", run_id="")

    record = store.resolve(ctx.object_id, ctx.run_id,
                           message_id=_message_id(event_data),
                           lead_object_id=str(variables.get("lqabr_object_id") or ""))
    if record is None:
        obs.process(ctx, step=8, event="event_matched_no_run_state",
                    mailgun_event=event.value, correlation_token=token,
                    detail="an event whose token matches no run state has no path today")
        return {"status": "unresolved", "reason": "no run state for this correlation token",
                "event": event.value, "correlation_token": token}

    # ------------------------------------------------- which status won (8)
    current = MailgunEvent(record.status) if record.status else None
    winner, changed = resolve_status(current, event)
    record.events.append({"event": event.value, "raw": raw_event})
    if event is MailgunEvent.DELIVERED:
        record.delivered = True
    if event is MailgunEvent.CLICKED:
        record.clicked = True

    obs.process(ctx, step=8, event="status_resolved", object_id=record.object_id,
                message_id=record.message_id or None, arrived=event.value,
                previous=current.value if current else None,
                winner=winner.value, changed=changed)

    if not changed:
        store.update(ctx.object_id, ctx.run_id, record)
        return {"status": "superseded", "event": event.value,
                "current_status": winner.value, "object_id": record.object_id}

    record.status = winner.value
    return _write_back(ctx, record.object_id, winner, record=record, store=store,
                       session=session, run_object_id=ctx.object_id, run_id=ctx.run_id)


def _write_back(ctx: Optional[obs.RunContext], object_id: str, winner: MailgunEvent,
                record, store: RunStateStore, session: Optional[MCPSession],
                run_object_id: str, run_id: str) -> Dict[str, Any]:
    # object_id     = the lead being written back to HubSpot (a HubSpot record)
    # run_object_id = the run-state key (the campaign/trigger object id); the
    #                 two are the same on a single-lead send and differ in a
    #                 campaign, so they must stay distinct.
    """STEP 9 — land engagement state on the system of record, through the
    same central MCP and against the same schema used for the read."""
    mcp_session = session or build_session(obs=obs.MCPObservability(ctx))

    # HubSpot is the system of record: read the current probability back
    # rather than trusting anything held here (on conflict, CRM wins).
    try:
        profile = mcp_session.crm.get_lead_profile(object_id)
        current_probability = profile.probability
    except (CRMError, SchemaValidationError) as exc:
        obs.process(ctx, step=9, event="probability_read_failed",
                    object_id=object_id, error=str(exc))
        return {"status": "unresolved", "reason": f"crm-error: {exc}",
                "object_id": object_id, "event": winner.value}

    properties: Dict[str, Any] = {"lqabr_email_status": HUBSPOT_EMAIL_STATUS[winner]}

    # Probability moves only on real engagement, and only by the increments
    # lqabr_core.probability defines — never redefined here.
    new_probability = current_probability
    if winner in POSITIVE_EVENTS:
        new_probability = apply_event(current_probability, SCORED_AS[winner])
        properties["probability"] = new_probability

    # THE HAND-OFF CONDITION: lqabr_email_status reaches OPENED.
    #
    # Confirmed by Yashwanth, 2026-08-04: the campaign is complete once the
    # email status is OPENED. That is read off the STATUS, not off the raw
    # Mailgun event, and the two are not the same thing — the confirmed
    # HubSpot enumeration has no CLICKED value, so `enums.HUBSPOT_EMAIL_STATUS`
    # maps a click to OPENED as well (a click implies an open). Keying on the
    # status therefore covers `opened` AND `clicked` with one rule, and stays
    # correct if the enumeration ever gains a CLICKED value.
    #
    # The campaign-complete column ALONE is what the voice campaign reads —
    # never a probability threshold. Written once: `record.campaign_complete`
    # stops a later event from re-writing a column that is already true.
    email_status = properties["lqabr_email_status"]
    # The campaign-complete column is opt-out: an empty configured name
    # (LQABR_HUBSPOT_CAMPAIGN_COMPLETE_PROPERTY="") disables the hand-off write
    # entirely, so engagement still records on accounts where that property
    # does not exist yet — a missing hand-off column must not block the open.
    complete_property = campaign_complete_property()
    complete = bool(complete_property and email_status == "OPENED"
                    and (record is None or not record.campaign_complete))
    if complete:
        properties[complete_property] = True

    try:
        mcp_session.crm.patch_object(object_id, properties)
    except (CRMError, SchemaValidationError) as exc:
        obs.process(ctx, step=9, event="writeback_failed", object_id=object_id,
                    error=str(exc), detail="event not recorded — retry expected")
        return {"status": "unresolved", "reason": f"crm-error: {exc}",
                "object_id": object_id, "event": winner.value}

    if record is not None:
        if complete:
            record.campaign_complete = True
        record.terminal = is_terminal(winner)
        store.update(run_object_id, run_id, record)

    if complete:
        obs.process(ctx, step=10, event="handoff_condition_met", object_id=object_id,
                    detail=(f"{campaign_complete_property()} set — ownership passes to the "
                            "text/voice agent; the email agent stops acting on this lead"))

    if is_terminal(winner):
        obs.process(ctx, step=9, event="run_ended", object_id=object_id,
                    reason=f"terminal status {winner.value}", handoff=False)

    return {"status": "recorded", "event": winner.value, "object_id": object_id,
            "probability": new_probability,
            "email_status": properties["lqabr_email_status"],
            "campaign_complete": complete,
            "terminal": is_terminal(winner)}


# ------------------------------------------------------- the tool-call entry
def sync_run_engagement(object_id: str, run_id: str,
                        session: Optional[MCPSession] = None,
                        store: Optional[RunStateStore] = None,
                        events_client: Optional[Any] = None) -> Dict[str, Any]:
    """STEP 8 via a TOOL CALL — "give me the status of these track IDs".

    Reads the run's own state for the message IDs it sent, asks Mailgun for
    their events through the central MCP, and feeds each one through exactly
    the same `handle_event` a pushed event goes through. Whatever changed is
    written back to HubSpot; whatever did not is reported as `superseded` and
    written nowhere.

    Called once, on demand, inside a run that is already up. It is not a
    poller: nothing here loops, sleeps, or schedules itself.

    Returns a per-message summary plus counts, so the caller can say what
    actually moved rather than claiming engagement that did not happen."""
    store = store or RunStateStore()
    ctx = obs.RunContext(object_id=object_id, run_id=run_id)

    state = store.summary(object_id, run_id)
    leads = state.get("leads", {}) or {}
    message_ids = [rec.get("message_id") for rec in leads.values() if rec.get("message_id")]

    if not message_ids:
        obs.process(ctx, step=8, event="sync_no_messages",
                    detail="run state holds no message ids — nothing was sent, or "
                           "state was lost before this ran")
        return {"object_id": object_id, "run_id": run_id, "messages": 0,
                "events": 0, "recorded": 0, "results": [],
                "reason": "no message ids in run state"}

    from mailgun_tool import fetch_event_status  # local: keeps import cost off push

    try:
        fetched = fetch_event_status(
            message_ids=message_ids,
            correlation_token=ctx.correlation_token,
            client=events_client,
            obs=obs.MCPObservability(ctx),
        )
    except MailgunError as exc:
        obs.process(ctx, step=8, event="sync_failed", error=str(exc))
        return {"object_id": object_id, "run_id": run_id, "messages": len(message_ids),
                "events": 0, "recorded": 0, "results": [],
                "error": f"mailgun-error: {exc}"}

    results = [handle_event(event, session=session, store=store) for event in fetched]
    recorded = sum(1 for r in results if r.get("status") == "recorded")

    obs.process(ctx, step=8, event="sync_complete", messages=len(message_ids),
                events=len(fetched), recorded=recorded,
                superseded=sum(1 for r in results if r.get("status") == "superseded"),
                unresolved=sum(1 for r in results if r.get("status") == "unresolved"))

    return {"object_id": object_id, "run_id": run_id, "messages": len(message_ids),
            "events": len(fetched), "recorded": recorded, "results": results}
