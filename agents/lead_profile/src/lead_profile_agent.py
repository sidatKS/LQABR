"""Lead Profile Agent — builds the 9-pointer lead profile and saves it to
HubSpot, the central lead-profile source.

The deterministic logic (record -> profile -> HubSpot upsert) lives in
lqabr_core.profile / lqabr_core.crm; this module is the ADK wrapper: a
FunctionTool the model can call plus the `root_agent` the ADK loader
discovers:

    adk web  agents/lead_profile/src
    adk run  agents/lead_profile/src
    adk api_server agents/lead_profile/src    # what Cloud Run serves

The 9 pointers every profile carries (lqabr_core.types.LeadProfile):
full_name, job_title, company, email, phone, industry,
company_size_revenue, location (+ timezone), linkedin_url.

Records normally arrive from the ingestion trigger (agents/ingestion,
source csv|zoominfo). This agent can also be invoked directly with raw
records — e.g. via A2A from the orchestrator, or in the ADK dev UI.

Rules honored here (CLAUDE.md): HubSpot is the system of record (the built
profile is immediately upserted; nothing canonical is kept in the agent);
bad records are flagged with a reason, never silently dropped; model choice
is config-driven (LQABR_LEAD_PROFILE_MODEL), never hard-coded.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from google.adk.agents import Agent

from lqabr_core.crm import HubSpotClient
from lqabr_core.profile import profile_and_upsert, profile_records

MODEL = os.environ.get("LQABR_LEAD_PROFILE_MODEL", "gemini-2.0-flash")


def build_lead_profiles(records: List[Dict[str, Any]], save_to_hubspot: bool = True) -> Dict[str, Any]:
    """Build 9-pointer lead profiles from raw records and save them to HubSpot.

    Args:
        records: normalized raw lead records (keys: full_name, job_title,
            company, email, phone, industry, company_size, annual_revenue,
            location, linkedin_url, external_employee_id,
            external_company_id, source).
        save_to_hubspot: when False, only build/validate profiles (dry run).

    Returns:
        {"summary": counts, "profiles": [9-pointer profiles with HubSpot ids],
         "unresolved": flagged records with reasons,
         "upsert_failures": CRM write failures with reasons}.
        Unresolved and failed records are always reported — never dropped.
    """
    if save_to_hubspot:
        result = profile_and_upsert(records, HubSpotClient())
    else:
        result = profile_records(records)
    return {
        "summary": result.summary,
        "profiles": [p.to_dict() for p in result.profiles],
        "unresolved": [{"reason": u.reason,
                        "external_employee_id": u.external_employee_id,
                        "external_company_id": u.external_company_id} for u in result.unresolved],
        "upsert_failures": [{"reason": u.reason,
                             "external_employee_id": u.external_employee_id} for u in result.upsert_failures],
    }


def get_lead_profile(email: str) -> Dict[str, Any]:
    """Fetch one lead profile from HubSpot by email.

    Returns the lead's 9 pointers plus pipeline metadata (stage, probability,
    engagement counters), or {"error": ...} if not found.
    """
    lead = HubSpotClient().find_lead_by_email(email)
    if lead is None:
        return {"error": f"no HubSpot contact with email {email}"}
    return lead.to_dict()


root_agent = Agent(
    name="lead_profile_agent",
    model=MODEL,
    description=("LQABR Lead Profile Agent: builds the 9-pointer lead profile "
                 "(name, title, company, email, phone, industry, size/revenue, "
                 "location, LinkedIn) and saves it to HubSpot."),
    instruction=(
        "You are the LQABR lead profile agent. HubSpot is the central lead "
        "profile source.\n\n"
        "When given raw lead records to profile, call build_lead_profiles. "
        "When asked about an existing lead, call get_lead_profile with its "
        "email.\n\n"
        "Your final response MUST include the full profiles list as JSON, "
        "every record kept exactly as returned — all 9 pointers plus stage "
        "and probability — nothing summarized, reworded, or dropped. Then "
        "report the summary counts and always surface unresolved records and "
        "upsert failures with their reasons: no lead is ever silently dropped."
    ),
    tools=[build_lead_profiles, get_lead_profile],
)
