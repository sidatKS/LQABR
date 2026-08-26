"""Email Agent — the ADK wrapper. v2.

    adk web  agents/email/src
    adk run  agents/email/src
    adk api_server agents/email/src     # what Cloud Run serves

One process, one container, one run ID, four log streams. This module is
only the model-facing surface: the deterministic work lives in typed,
mockable modules beside it, and every HubSpot hop goes through the central
MCP at the project root.

    STEP 3    _bind_run (inline)           run start, correlation token
    STEP 4    mcp/hubspot/auth.py          machine-to-machine bearer
    STEP 5    mcp/hubspot/server.py        profile + lead_context
    STEP 6    skills/ + one model call     construct the email
    STEP 7    outreach.py                  send, one email per lead
    STEP 8/9  events.py + service_app.py   engagement events, write-back
    STEP 10   the campaign-complete column hands the lead to text/voice

Those are the log `step=` keys, in the rev-7 numbering; the FRD v4 numbering
for the same work is 8-14. `outreach.py`'s module docstring carries the
mapping table.

The gateway invokes this agent with a trigger ID and nothing more; no profile
payload passes through it, and agents never call each other.

REV 8 (v4): research is a SEPARATE agent. It builds each lead's knowledge
graph and persists `lead_context` to HubSpot, and that write is what triggers
this agent. This agent does no research: it loads that context, frames
construction with it, and skips any lead that has none.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

from google.adk.agents import Agent

# --------------------------------------------------------------- logging
# One JSON line per event, stamped with object_id + run_id so a whole run
# greps back together. Inlined per module (no shared observability file); the
# MCP is handed `MCPObservability` because mcp/hubspot/ cannot import agent code.
import json as _json
import logging as _logging
import hashlib as _hashlib
import uuid as _uuid
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
    import os
    f.setdefault("host", os.environ.get("K_REVISION") or os.environ.get("HOSTNAME", "local"))
    _emit("system_log", None, **f)


def _bind_run(object_id, run_id=None):
    if not object_id:
        raise ValueError("object_id is required — a run cannot be logged without it")
    ctx = RunContext(str(object_id), run_id or _uuid.uuid4().hex)
    _log_process(ctx, step=3, event="run_started")
    return ctx


def _configure_logging(level=_logging.INFO):
    _LOG.setLevel(level)
    if not _LOG.handlers:
        h = _logging.StreamHandler(); h.setFormatter(_logging.Formatter("%(message)s"))
        _LOG.addHandler(h)
    _LOG.propagate = False


class MCPObservability:
    def __init__(self, ctx=None): self.ctx = ctx
    def process(self, **f): _log_process(self.ctx, **f)
    def audit(self, **f): _log_audit(self.ctx, **f)

import outreach

from lqabr_core.crm import CRMError
from lqabr_core.model import build_model

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.hubspot.schema import SchemaValidationError  # noqa: E402
from mcp.hubspot.server import build_session  # noqa: E402

MODEL = os.environ.get("LQABR_EMAIL_MODEL", "gemini-2.0-flash")

_configure_logging()
_log_system(event="container_started", component="email-agent", model=MODEL)


# --------------------------------------------------------------------- tools
def run_email_campaign(object_id: str, limit: int = 0, dry_run: bool = False) -> Dict[str, Any]:
    """Run one outreach campaign for a HubSpot campaign trigger — the agent's
    main entry point.

    Args:
        object_id: the ID the HubSpot campaign fired at the gateway. The
            lead profiles are chunked under it and stay in HubSpot; this is
            the only thing the trigger carries.
        limit: max leads to work this run (0 = the configured batch size).
        dry_run: construct every email and report it, but send nothing.
            Use this when the operator asks to preview a campaign.

    Returns per-lead results plus an `unresolved` list — any lead that could
    not be worked is there with an explicit reason, never dropped. Delivery,
    opens and clicks are recorded later by the Mailgun webhook, never here.
    """
    try:
        return outreach.run_campaign(object_id, limit=limit, dry_run=dry_run)
    except CRMError as exc:
        return {"error": f"crm-error: {exc}", "object_id": object_id}


def preview_email(object_id: str, cta_url: str = "") -> Dict[str, Any]:
    """Show which skill a lead would get and what the email would say, for one
    HubSpot object id — no send, no run state, read-only.

    `object_id` is the lead's HubSpot record id (the same id the gateway
    forwards). The profile is read straight from HubSpot by that id; there is
    no email lookup.

    Reports `awaiting-research` for a lead with no lead_context, because that
    is what the campaign would do with it. A preview that invented copy the
    campaign would refuse to send would be worse than no preview at all."""
    ctx = _bind_run(object_id=str(object_id))
    session = build_session(obs=MCPObservability(ctx))
    try:
        profile = session.crm.get_lead_profile(str(object_id))
    except SchemaValidationError as exc:
        return {"error": str(exc), "object_id": str(object_id)}
    except CRMError as exc:
        return {"error": f"crm-error: {exc}", "object_id": str(object_id)}

    # Construction is instruction-based, so a preview must draft with the
    # model too — there is no template to render without one. A preview that
    # could not reach the model reports that rather than showing copy the
    # campaign would never actually send.
    try:
        subject, html_body, skill = outreach.construct_email(ctx, profile, cta_url=cta_url)
    except outreach.MissingLeadContext as exc:
        return {"error": f"awaiting-research: {exc}", "object_id": profile.object_id,
                "lead_context": ""}
    except outreach.skills.SkillError as exc:
        return {"error": f"construction: {exc}", "object_id": profile.object_id,
                "skill": outreach.skills.select_skill(profile.industry)[0].name}
    return {"object_id": profile.object_id, "to": profile.email, "skill": skill,
            "subject": subject, "html_body": html_body}


def send_outreach_email(object_id: str, cta_url: str = "") -> Dict[str, Any]:
    """Send one outreach email to one lead, by HubSpot object id, through the
    full step 4-7 path (bearer, MCP profile read, skill selection, Mailgun
    send).

    `object_id` is the lead's HubSpot record id and the ONLY input. The send
    tags the Mailgun message with that object id, so a returning Mailgun event
    resolves straight back to it — no run state is kept. There is no email
    argument and no email lookup.

    The result carries object_id and run_id (the send's identity). The Mailgun
    message id is dropped from the result — the operator's process keys on
    object id and run id only.

    Args:
        object_id: the lead's HubSpot object (record) id.
        cta_url: link for the call-to-action; clicks on it are tracked.
    """
    ctx, session = outreach.start_run(str(object_id))
    try:
        profile = session.crm.get_lead_profile(str(object_id))
    except SchemaValidationError as exc:
        return {"error": str(exc), "object_id": str(object_id)}
    except CRMError as exc:
        return {"error": f"crm-error: {exc}", "object_id": str(object_id)}

    try:
        subject, html_body, skill = outreach.construct_email(ctx, profile, cta_url=cta_url)
    except outreach.MissingLeadContext as exc:
        # Nothing is written back and nothing is sent: the lead is waiting on
        # the research agent, not broken. It stays at its current status so a
        # later campaign picks it up untouched.
        return {"error": f"awaiting-research: {exc}", "object_id": profile.object_id}
    except outreach.skills.SkillError as exc:
        return {"error": f"construction: {exc}", "object_id": profile.object_id}
    result = outreach.send_one(ctx, session, profile, subject, html_body, skill)
    # A send is identified by object id + run id here. The Mailgun message id is
    # not surfaced — the returning event carries the object id, so the operator's
    # process keys on object id and run id only.
    result.pop("message_id", None)
    result["object_id"] = ctx.object_id
    result["run_id"] = ctx.run_id
    return result


def get_lead_status(object_id: str) -> Dict[str, Any]:
    """One lead's current email status and probability, by HubSpot object id.
    Read-only.

    Never claim an email was opened or clicked unless this says so."""
    ctx = _bind_run(object_id=str(object_id))
    session = build_session(obs=MCPObservability(ctx))
    try:
        profile = session.crm.get_lead_profile(str(object_id))
    except SchemaValidationError as exc:
        return {"error": str(exc), "object_id": str(object_id)}
    except CRMError as exc:
        return {"error": f"crm-error: {exc}"}
    return {
        "object_id": profile.object_id,
        "email": profile.email,
        "email_status": profile.email_status,
        "probability": profile.probability,
        "company": profile.company,
        "job_title": profile.job_title,
    }


def list_email_queue(object_id: str = "", limit: int = 25) -> Dict[str, Any]:
    """The leads chunked under a campaign trigger and awaiting outreach.

    With no object_id, falls back to the not-yet-emailed queue so an
    operator can still see what is outstanding."""
    ctx = _bind_run(object_id=object_id or "queue-inspection")
    session = build_session(obs=MCPObservability(ctx))
    try:
        leads = session.crm.leads_for_trigger(object_id or ctx.object_id, limit=limit)
    except CRMError as exc:
        return {"error": f"crm-error: {exc}"}
    return {"count": len(leads), "object_id": object_id or None,
            "leads": [lead.to_dict() for lead in leads]}


def get_lead_profile(object_id: str) -> Dict[str, Any]:
    """One lead's full HubSpot profile, read directly by its object id.

    On the manual send path the object id IS the lead's numeric HubSpot
    object (contact) id, so this returns that contact's schema-validated
    9-pointer profile straight from HubSpot — no email lookup, no send, no
    run state. Read-only.

    The campaign path uses object_id differently: there it is the trigger
    value many leads are chunked under (the HubSpot `object_id` contact
    property), so use list_email_queue for that — this tool reads a single
    contact record by its id."""
    ctx = _bind_run(object_id=str(object_id))
    session = build_session(obs=MCPObservability(ctx))
    try:
        profile = session.crm.get_lead_profile(str(object_id))
    except SchemaValidationError as exc:
        return {"error": str(exc)}
    except CRMError as exc:
        return {"error": f"crm-error: {exc}"}
    # The record id, then EXACTLY the construction fields, in the agreed order.
    #
    # This deliberately mirrors what the email is built from rather than
    # listing a different set: an operator reading this is checking the inputs
    # behind a draft, and a profile view that showed `employee_id` and
    # `company_id` — which construction never sees, and which must never reach
    # an email — while hiding the company name, industry group, About-Us,
    # website and revenue that it DOES see would be reviewing a different lead
    # than the one being written to.
    #
    # `CONSTRUCTION_FIELDS` is the single source of truth, shared with
    # `as_context()`, so the two cannot drift apart again.
    view = {"object_id": profile.object_id}
    view.update(profile.construction_view())
    return view


root_agent = Agent(
    name="email_agent",
    model=build_model(MODEL),
    description=("LQABR Email Agent (v4): runs a HubSpot campaign trigger end to "
                 "end — reads each lead's 9-parameter profile AND the research "
                 "agent's lead_context through the central HubSpot MCP, selects "
                 "an email skill from the lead's industry, drafts the email with "
                 "one model call per lead framed by that context, and sends one "
                 "Mailgun email per lead tagged with the lead's object id. It "
                 "does no research of its own. Delivery, opens and clicks return "
                 "asynchronously as Mailgun's inbound event call and are written "
                 "back to HubSpot by object id."),
    instruction=(
        "You are the LQABR email outreach agent.\n\n"
        "The designed entry point is a campaign object: when you are given a "
        "trigger ID, call run_email_campaign with it. That single call binds "
        "the run's correlation token, acquires the HubSpot bearer, reads each "
        "lead's profile through the central MCP, chooses the email skill, and "
        "sends one email per lead. Report its `results` and `unresolved` "
        "lists exactly as returned — an unresolved lead is a real outcome, "
        "not a failure to hide.\n\n"
        "Every single-lead tool takes ONE input: the lead's HubSpot object "
        "id (its numeric record id). There is no email argument anywhere — "
        "given an object id you can preview, send, and check status end to "
        "end.\n\n"
        "Use dry_run=True when the operator wants to preview a campaign "
        "before it goes out, and preview_email(object_id) when they ask what "
        "a single lead would receive.\n\n"
        "When the operator names one specific lead to contact now, call "
        "send_outreach_email(object_id). Report exactly the fields the tool "
        "returns (status, to, skill, object_id, run_id) as a plain list — no "
        "extra commentary, no notes about message ids or policy. Use "
        "get_lead_status(object_id) for a "
        "plain read of what HubSpot already holds, or get_lead_profile("
        "object_id) to read a lead's profile. For every read tool, report "
        "exactly the fields the tool returns, as a plain list, in the order "
        "given — never add fields, and never add a note about missing, absent, "
        "or missing-pointer fields. When the operator asks whether a lead has "
        "opened or clicked, call get_lead_status — it reads what HubSpot holds, "
        "which the inbound Mailgun event keeps current. Use list_email_queue to "
        "see what is outstanding.\n\n"
        "Email content is skill-based. The skill is selected from the lead's "
        "real industry and fields, and the draft is framed by the lead's "
        "lead_context — the research agent's knowledge graph for that lead. "
        "You do not write the email from scratch and you never write one "
        "email for a batch. Only state facts present in the lead's profile or "
        "its lead_context; if a field is missing, the skill writes around it "
        "rather than inventing a value.\n\n"
        "You do NO research. A separate research agent builds lead_context and "
        "writes it to HubSpot, and that write is what triggers this campaign. "
        "A lead with no lead_context is reported as awaiting-research: no "
        "email is constructed for it, nothing is written to its record, and it "
        "stays where it is for a later run once research has reached it. Say "
        "exactly that when it happens — do not describe it as a failure, do "
        "not offer to write the email anyway, and never assemble a context "
        "yourself from the profile fields.\n\n"
        "Never invent an object id: only work leads that exist in HubSpot. "
        "Report Mailgun and CRM errors verbatim — a failed send is "
        "never ignored, and a rejected send resolves to a terminal status "
        "that ends that lead's run. Engagement comes only from Mailgun — the "
        "inbound event updates HubSpot, never fabricated: never claim an email "
        "was delivered, opened or clicked unless get_lead_status actually "
        "shows it."
    ),
    tools=[run_email_campaign, preview_email, send_outreach_email,
           get_lead_status, get_lead_profile, list_email_queue],
)
