"""Orchestrator Agent — owns the LQABR pipeline state machine and routes
leads between the stage agents via A2A.

    adk web  agents/orchestrator/src
    adk run  agents/orchestrator/src
    adk api_server agents/orchestrator/src    # Cloud Run

Pipeline (probability thresholds from lqabr_core.probability — the single
source of truth):

    ingested/profiled ──► email_outreach          (Email Agent, Mailgun)
        probability >= TEXT_VOICE_THRESHOLD (30)
                      ──► text_voice_outreach     (Text/Voice Agent, Twilio)
        probability >= SCHEDULING_THRESHOLD (60)
                      ──► scheduling              (Scheduling Agent, Zoom)
        booking       ──► meeting_scheduled       (rep handoff)

HubSpot is the system of record: this agent reads work queues from HubSpot
and dispatches instructions; it keeps no lead state of its own. Stage
promotions happen in CRM writeback (record_event) when thresholds are
crossed — the orchestrator simply picks up each stage's queue and hands it
to the owning agent.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from google.adk.agents import Agent

from lqabr_core.crm import CRMClient, HubSpotClient
from lqabr_core.probability import SCHEDULING_THRESHOLD, TEXT_VOICE_THRESHOLD
from lqabr_core.types import LeadStage

try:
    from .a2a_dispatch import A2ADispatcher, A2AError
except ImportError:  # pragma: no cover
    from a2a_dispatch import A2ADispatcher, A2AError  # type: ignore

MODEL = os.environ.get("LQABR_ORCHESTRATOR_MODEL", "gemini-2.0-flash")

# Which agent owns which stage, and what it should do per lead.
STAGE_ROUTING = {
    LeadStage.PROFILED: ("email", "Send an outreach email to the lead with email {email} "
                                  "(HubSpot contact {contact_id})."),
    LeadStage.EMAIL_OUTREACH: ("email", "Continue email outreach for the lead with email {email} "
                                        "(HubSpot contact {contact_id}) if a follow-up is due."),
    LeadStage.TEXT_VOICE_OUTREACH: ("text_voice", "Place an outreach call to the lead with email "
                                                  "{email} (HubSpot contact {contact_id})."),
    LeadStage.SCHEDULING: ("scheduling", "Send a schedule invite to the lead with email {email} "
                                         "(HubSpot contact {contact_id})."),
}


def _engine_crm() -> CRMClient:
    return HubSpotClient()


def pipeline_status() -> Dict[str, Any]:
    """Lead counts and probability stats per pipeline stage — the pipeline
    dashboard view, read live from HubSpot."""
    crm = _engine_crm()
    status: Dict[str, Any] = {"thresholds": {
        "text_voice": TEXT_VOICE_THRESHOLD, "scheduling": SCHEDULING_THRESHOLD}}
    for stage in LeadStage:
        leads = crm.leads_in_stage(stage, limit=100)
        status[stage.value] = {
            "count": len(leads),
            "avg_probability": (round(sum(l.probability for l in leads) / len(leads), 1)
                                if leads else 0),
        }
    return status


def dispatch_cycle(batch_per_stage: int = 10, dry_run: bool = False) -> Dict[str, Any]:
    """Run one orchestration cycle: for every stage with an owning agent,
    pull its queue from HubSpot and dispatch each lead to that agent via A2A.

    Args:
        batch_per_stage: max leads dispatched per stage per cycle.
        dry_run: plan the dispatches without sending A2A messages.

    Returns a report of dispatched/planned work items and any failures —
    failed dispatches are listed with reasons, never dropped.
    """
    crm = _engine_crm()
    dispatcher = A2ADispatcher()
    dispatched: List[Dict[str, str]] = []
    failures: List[Dict[str, str]] = []

    for stage, (agent_key, instruction) in STAGE_ROUTING.items():
        for lead in crm.leads_in_stage(stage, limit=batch_per_stage):
            if not lead.email:
                failures.append({"contact_id": lead.contact_id or "?",
                                 "stage": stage.value,
                                 "reason": "bad-data: lead has no email lookup key"})
                continue
            text = instruction.format(email=lead.email, contact_id=lead.contact_id)
            item = {"contact_id": lead.contact_id or "?", "stage": stage.value,
                    "agent": agent_key, "instruction": text,
                    "probability": str(lead.probability)}
            if dry_run:
                dispatched.append({**item, "status": "planned"})
                continue
            try:
                dispatcher.send_message(agent_key, text,
                                        metadata={"contact_id": lead.contact_id,
                                                  "stage": stage.value})
                dispatched.append({**item, "status": "dispatched"})
            except A2AError as exc:
                failures.append({**item, "reason": str(exc)})

    return {"dry_run": dry_run, "dispatched": dispatched, "failures": failures,
            "summary": {"dispatched": len(dispatched), "failures": len(failures)}}


root_agent = Agent(
    name="orchestrator_agent",
    model=MODEL,
    description=("LQABR orchestrator: owns the pipeline state machine, reads "
                 "stage queues from HubSpot, and routes leads to the email, "
                 "text/voice, and scheduling agents via A2A."),
    instruction=(
        "You are the LQABR pipeline orchestrator. HubSpot is the system of "
        "record — you never hold lead state yourself.\n\n"
        "Use pipeline_status to report the pipeline. Use dispatch_cycle to "
        "route work: profiled/email leads go to the Email Agent, leads past "
        "the text/voice threshold to the Text/Voice Agent, and leads past the "
        "scheduling threshold to the Scheduling Agent. Prefer dry_run=True "
        "first when the user wants to review before sending.\n\n"
        "Always report dispatch failures with their reasons — no lead is ever "
        "silently dropped or advanced. Stage promotion happens automatically "
        "in CRM writeback when probability thresholds are crossed; never "
        "override a stage manually unless the user explicitly asks."
    ),
    tools=[pipeline_status, dispatch_cycle],
)
