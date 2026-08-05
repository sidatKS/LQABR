"""Email Agent — the ADK wrapper. v2.

    adk web  agents/email/src
    adk run  agents/email/src
    adk api_server agents/email/src     # what Cloud Run serves

One process, one container, one run ID, four log streams. This module is
only the model-facing surface: the deterministic work lives in typed,
mockable modules beside it, and every HubSpot hop goes through the central
MCP at the project root.

    STEP 3    observability.bind_run       run start, correlation token
    STEP 4    mcp/hubspot/auth.py          machine-to-machine bearer
    STEP 5    mcp/hubspot/server.py        get_lead_profile_details
    STEP 6    skills/ + one model call     construct the email
    STEP 7    outreach.py                  send, one email per lead
    STEP 8/9  events.py + service_app.py   engagement events, write-back
    STEP 10   the campaign-complete column hands the lead to text/voice

The gateway (step 2) invokes this agent with a trigger ID and nothing more;
no profile payload passes through it, and agents never call each other.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict

from google.adk.agents import Agent

import observability as obs
import outreach
import events as events_module
from runstate import RunStateStore

from lqabr_core.crm import CRMError
from lqabr_core.model import build_model

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.hubspot.schema import SchemaValidationError  # noqa: E402
from mcp.hubspot.server import build_session  # noqa: E402

MODEL = os.environ.get("LQABR_EMAIL_MODEL", "gemini-2.0-flash")

obs.configure_logging()
obs.system(event="container_started", component="email-agent", model=MODEL)


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
    no email lookup."""
    ctx = obs.bind_run(object_id=str(object_id))
    session = build_session(obs=obs.MCPObservability(ctx))
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
        subject, html_body, skill = outreach.construct_email(
            ctx, profile, model_fn=outreach.build_model_fn(ctx), cta_url=cta_url)
    except outreach.skills.SkillError as exc:
        return {"error": f"construction: {exc}", "object_id": profile.object_id,
                "skill": outreach.skills.select_skill(profile.industry)[0].name}
    return {"object_id": profile.object_id, "to": profile.email_id, "skill": skill,
            "subject": subject, "html_body": html_body}


def send_outreach_email(object_id: str, cta_url: str = "") -> Dict[str, Any]:
    """Send one outreach email to one lead, by HubSpot object id, through the
    full step 4-7 path (bearer, MCP profile read, skill selection, Mailgun
    send, run state).

    `object_id` is the lead's HubSpot record id and the ONLY input. The run is
    correlated under that same object id, so a returning Mailgun event resolves
    straight back to it. There is no email argument and no email lookup.

    The result carries object_id and run_id (the send's identity). The Mailgun
    message id is recorded in run state for event matching but is not returned
    here.

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
        subject, html_body, skill = outreach.construct_email(
            ctx, profile, model_fn=outreach.build_model_fn(ctx), cta_url=cta_url)
    except outreach.skills.SkillError as exc:
        return {"error": f"construction: {exc}", "object_id": profile.object_id}
    result = outreach.send_one(ctx, session, profile, subject, html_body, skill)
    # A send is identified by object id + run id here. The Mailgun message id
    # is persisted in run state (for event matching) but is not part of this
    # tool's result — the operator's process keys on object id and run id only.
    result.pop("message_id", None)
    result["object_id"] = ctx.object_id
    result["run_id"] = ctx.run_id
    return result


def get_lead_status(object_id: str) -> Dict[str, Any]:
    """One lead's current email status and probability, by HubSpot object id.
    Read-only.

    Never claim an email was opened or clicked unless this says so."""
    ctx = obs.bind_run(object_id=str(object_id))
    session = build_session(obs=obs.MCPObservability(ctx))
    try:
        profile = session.crm.get_lead_profile(str(object_id))
    except SchemaValidationError as exc:
        return {"error": str(exc), "object_id": str(object_id)}
    except CRMError as exc:
        return {"error": f"crm-error: {exc}"}
    return {
        "object_id": profile.object_id,
        "email": profile.email_id,
        "email_status": profile.email_status,
        "probability": profile.probability,
        "company": profile.company,
        "job_title": profile.job_title,
    }


def refresh_lead_status(object_id: str) -> Dict[str, Any]:
    """Fetch a lead's LIVE engagement from Mailgun by HubSpot object id, write
    any change back to HubSpot, then return the refreshed status.

    This is the on-demand Mailgun tool call: one pull per run the
    lead belongs to — no webhook and no scheduler. Use it when the operator
    asks whether a lead has opened or clicked, or to refresh a status the
    webhook has not delivered — for example in local development, where Mailgun
    cannot reach the agent's push endpoint. When the lead has no run state on
    this host to pull, it degrades to the same read as get_lead_status.

    Engagement still comes only from Mailgun — this fetches real events, it
    never fabricates one."""
    ctx = obs.bind_run(object_id=str(object_id))
    session = build_session(obs=obs.MCPObservability(ctx))

    # Find the runs this lead was sent in, and pull each once. sync_run_engagement
    # asks Mailgun for the run's track IDs and writes back whatever moved — the
    # same handle_event a pushed webhook event goes through.
    runs = RunStateStore().runs_for_object(str(object_id))
    pulls = []
    for run_object_id, run_id in runs:
        try:
            summary = events_module.sync_run_engagement(run_object_id, run_id)
        except Exception as exc:  # a refresh must never break a status read
            pulls.append({"object_id": run_object_id, "run_id": run_id,
                          "error": f"pull-failed: {exc}"})
            continue
        pulls.append({"object_id": run_object_id, "run_id": run_id,
                      "events": summary.get("events", 0),
                      "recorded": summary.get("recorded", 0),
                      "error": summary.get("error")})

    # HubSpot is the system of record: re-read after any write-back.
    try:
        profile = session.crm.get_lead_profile(str(object_id))
    except (SchemaValidationError, CRMError) as exc:
        return {"error": f"crm-error: {exc}", "object_id": str(object_id),
                "runs_pulled": pulls}

    return {
        "object_id": profile.object_id,
        "email": profile.email_id,
        "email_status": profile.email_status,
        "probability": profile.probability,
        "company": profile.company,
        "job_title": profile.job_title,
        "runs_pulled": pulls,
        "note": ("pulled live Mailgun status for this lead's run(s)" if runs
                 else "no run state on this host for the lead — nothing to pull; "
                      "showing the stored status"),
    }


def list_email_queue(object_id: str = "", limit: int = 25) -> Dict[str, Any]:
    """The leads chunked under a campaign trigger and awaiting outreach.

    With no object_id, falls back to the not-yet-emailed queue so an
    operator can still see what is outstanding."""
    ctx = obs.bind_run(object_id=object_id or "queue-inspection")
    session = build_session(obs=obs.MCPObservability(ctx))
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
    ctx = obs.bind_run(object_id=str(object_id))
    session = build_session(obs=obs.MCPObservability(ctx))
    try:
        profile = session.crm.get_lead_profile(str(object_id))
    except SchemaValidationError as exc:
        return {"error": str(exc)}
    except CRMError as exc:
        return {"error": f"crm-error: {exc}"}
    # Only these fields are surfaced, in this order. Everything else on the
    # profile (probability, email_status, phone, location, linkedin_url,
    # company_size_revenue, missing_pointers, ...) is deliberately omitted.
    full = profile.to_dict()
    fields = ("object_id", "email_id", "employee_id", "job_title",
              "industry", "company_id")
    return {k: full.get(k) for k in fields}


def get_run_state(object_id: str, run_id: str) -> Dict[str, Any]:
    """What a past run sent, and what has come back for it so far. Use this
    to answer 'did this run get stuck' without inventing engagement."""
    return RunStateStore().summary(object_id, run_id)


root_agent = Agent(
    name="email_agent",
    model=build_model(MODEL),
    description=("LQABR Email Agent (v2): runs a HubSpot campaign trigger end to "
                 "end — reads each lead's 9-parameter profile through the central "
                 "HubSpot MCP, selects a templatized email skill from the lead's "
                 "industry, personalises it with one model call per lead, and "
                 "sends one Mailgun email per lead tagged with the run's "
                 "correlation token. Delivery, opens and clicks return "
                 "asynchronously (webhook push or the refresh_lead_status pull) and are written back to HubSpot."),
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
        "or missing-pointer fields. When the "
        "operator asks whether a "
        "lead has opened or clicked, or to refresh / recheck a lead's status, "
        "call refresh_lead_status — it pulls the live status from Mailgun and "
        "writes any change back before reporting, so a status check reflects "
        "reality without waiting on the webhook. Use list_email_queue to see "
        "what is outstanding.\n\n"
        "Email content is templatized and skill-based. The skill is selected "
        "from the lead's real industry and fields — you do not write the "
        "email from scratch and you never write one email for a batch. Only "
        "state facts present in the lead's profile; if a field is missing, "
        "the skill writes around it rather than inventing a value.\n\n"
        "Never invent an object id: only work leads that exist in HubSpot. "
        "Report Mailgun and CRM errors verbatim — a failed send is "
        "never ignored, and a rejected send resolves to a terminal status "
        "that ends that lead's run. Engagement comes only from Mailgun — via "
        "the webhook push or the refresh_lead_status pull, never fabricated: "
        "never claim an email was delivered, opened or clicked unless "
        "get_lead_status, refresh_lead_status or get_run_state actually shows it."
    ),
    tools=[run_email_campaign, preview_email, send_outreach_email,
           get_lead_status, get_lead_profile, refresh_lead_status,
           list_email_queue, get_run_state],
)
