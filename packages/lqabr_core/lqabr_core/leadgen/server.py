"""The central HubSpot layer as a REAL MCP server — the second front door.

Two front doors, ONE implementation (decision, 31 Jul):

  in-process import   the lead_profile agent calls
                      lqabr_core.leadgen.hubspot.crm directly. Context §7.3
                      stands: Step 4 -> Step 5 is a Python call, not a network
                      hop.

  MCP protocol        this module. The email / voice / scheduler agents consume
                      the same two tools over stdio (locally) or streamable
                      HTTP (Cloud Run), via ADK's MCPToolset. They get a
                      language-agnostic contract and cannot drift from the
                      write path the lead_profile agent uses, because there is
                      only one write path.

Both surfaces call the SAME functions in lqabr_core.leadgen.hubspot.crm.
Nothing is reimplemented here — this file is transport and shape, no logic.

Run it:
    lqabr-mcp-server                                  # stdio, for a local ADK MCPToolset
    lqabr-mcp-server --transport streamable-http --port 8080

Consume it from another ADK agent:

    from google.adk.agents import LlmAgent
    from google.adk.tools.mcp_tool import MCPToolset, StdioConnectionParams
    from mcp import StdioServerParameters

    agent = LlmAgent(
        name="email_agent",
        model=...,
        tools=[MCPToolset(connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command="lqabr-mcp-server", args=[],
            ),
        ))],
    )

The MCP SDK is imported as ``mcp``; this module lives at
``lqabr_core.leadgen.server`` so the two never collide.
"""

from __future__ import annotations

import argparse
from typing import Any

from mcp.server import MCPServer

from lqabr_core.obs import (
    Observability,
    RunContext,
    get_obs,
    new_lead_ref_id,
    set_current_run,
    set_obs,
)

from .hubspot.crm import get_lead_profile as _get_lead_profile
from .hubspot.crm import upsert_lead_profiles as _upsert_lead_profiles
from .hubspot.failures import SystemicFailure
from .hubspot.schema import LeadProfile

server = MCPServer(
    name="lqabr-hubspot",
    version="0.1.0",
    instructions=(
        "LQABR central MCP for HubSpot CRM. Two tools: upsert_lead_profile "
        "(the only writer) and get_lead_profile (the shared read path). "
        "Contacts dedup on employee_id, Companies on company_id. HubSpot is the "
        "system of record; on conflict CRM wins."
    ),
)


def _bind_run(agent: str) -> None:
    """Each MCP session logs under its own run — never the caller's (B10)."""
    ctx = RunContext(agent=agent, uses_model=False)
    set_current_run(ctx)
    set_obs(Observability(ctx))


@server.tool(
    name="upsert_lead_profile",
    description=(
        "Create or update one lead in HubSpot: upserts the Company, upserts the "
        "Contact, and associates them. Creating and updating are ONE upsert, so "
        "calling this twice for the same lead is safe and does not duplicate. "
        "Returns the HubSpot ids. Bad data is recorded and reported as failed, "
        "never silently dropped."
    ),
)
async def upsert_lead_profile(
    employee_id: str,
    company_id: str,
    decision_maker_flag: str,
    job_title: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    industry: str | None = None,
    annual_revenue_m: str | None = None,
    frequency_of_purchase: str | None = None,
    company_name: str | None = None,
    industry_group: str | None = None,
    about_us: str | None = None,
    website_url: str | None = None,
    lead_ref_id: str | None = None,
) -> dict[str, Any]:
    """The contract fields, flattened for the wire. Field names are the contract.
    company_name / industry_group / about_us / website_url (added 2026-08-25)
    map to HubSpot's standard name / hs_industry_group / about_us / website."""
    _bind_run("mcp_client")
    profile = LeadProfile(
        employee_id=employee_id,
        company_id=company_id,
        decision_maker_flag=decision_maker_flag,
        job_title=job_title,
        email=email,
        phone=phone,
        industry=industry,
        annual_revenue_m=annual_revenue_m,
        frequency_of_purchase=frequency_of_purchase,
        company_name=company_name,
        industry_group=industry_group,
        about_us=about_us,
        website_url=website_url,
    )
    ref = lead_ref_id or new_lead_ref_id()
    try:
        return _upsert_lead_profiles(profile, ref).to_dict()
    except SystemicFailure as exc:
        # Config/auth is broken for every caller — say so plainly rather than
        # returning something that looks like a per-record rejection.
        get_obs().process.emit("mcp_systemic_failure", lead_ref_id=ref, reason=str(exc))
        return {
            "lead_ref_id": ref,
            "status": "halted",
            "failure_kind": "systemic",
            "reasons": [str(exc)],
        }


@server.tool(
    name="get_lead_profile",
    description=(
        "Read one lead's current state from HubSpot. Read-only. Returns the nine "
        "contract fields plus contact_hs_id and company_hs_id so you can write "
        "status back without re-searching. Check company_resolved: when false, "
        "the company fields are unpopulated and warnings explains why."
    ),
)
async def get_lead_profile(
    employee_id: str | None = None, email: str | None = None
) -> dict[str, Any]:
    """employee_id is the primary key. email searches HubSpot's standard email property."""
    _bind_run("mcp_client")
    if not employee_id and not email:
        return {"found": False, "error": "employee_id or email is required"}
    return _get_lead_profile(employee_id or None, email or None).to_dict()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lqabr-mcp-server",
        description="LQABR central MCP (HubSpot) over the MCP protocol.",
    )
    parser.add_argument(
        "--transport",
        default="stdio",
        choices=("stdio", "sse", "streamable-http"),
        help="stdio for a local ADK MCPToolset; streamable-http for Cloud Run",
    )
    args = parser.parse_args(argv)
    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
