"""mcp_server.py — MCP entry point for the Lead Profile (enrichment) agent's tool.

Exposes `build_lead_profiles` (lead_profile_agent.py) directly as an MCP
tool via FastMCP, so any MCP host — the orchestrator agent, Claude
Desktop/Code, or another MCP-speaking client — can call lead profiling over
MCP instead of only through the in-process ADK FunctionTool wired onto
`root_agent`.

This wraps the tool function itself (deterministic CSV join/filter logic,
no model call) rather than the whole LLM agent conversation. That keeps an
MCP call here fast, free of model cost, and returning the exact typed
contract documented on `build_lead_profiles` — no LLM paraphrasing in
between — consistent with CLAUDE.md §5 ("Tools are typed, mockable, and own
their own retry/failure behavior") and §9 (never hard-code agent/model
specifics; this server has none).

`root_agent` (the ADK Agent wrapping this same tool) is unchanged and still
discoverable via `adk web/run/api_server agents/enrichment/src` for local
dev / conversational use. This module is an additional entry point, not a
replacement — same tool function, two ways to call it (in-process ADK tool,
or out-of-process MCP tool call).

Run locally:
    python -m src.mcp_server                          # streamable-http on 127.0.0.1:8080
    MCP_TRANSPORT=stdio python -m src.mcp_server       # stdio, for local MCP hosts (e.g. Claude Desktop/Code)

Env vars:
    MCP_TRANSPORT   "streamable-http" (default) or "stdio".
    MCP_HOST        Bind host for streamable-http. Defaults to "0.0.0.0" so
                     it's reachable in a container; ignored for stdio.
    PORT / MCP_PORT Bind port for streamable-http. Cloud Run injects PORT;
                     MCP_PORT is the local-dev override. Defaults to 8080.

Cloud Run serves this over streamable-http, binding 0.0.0.0:$PORT per the
platform convention (CLAUDE.md §2/§3), reachable from the orchestrator agent
or the API gateway as any other MCP-speaking client.
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

try:
    from .lead_profile_agent import build_lead_profiles
except ImportError:  # running as a top-level script, no package context
    from lead_profile_agent import build_lead_profiles

MCP_TRANSPORT = os.environ.get("MCP_TRANSPORT", "streamable-http")
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("PORT", os.environ.get("MCP_PORT", "8080")))

server = FastMCP(
    name="lead_profile_agent",
    instructions=(
        "Builds decision-maker lead profiles by joining employee, contact, "
        "and company records. Call build_lead_profiles to enrich/profile "
        "qualified leads. Every result always separates 'profiles' (joined "
        "records) from 'unresolved' (flagged, never silently dropped)."
    ),
    host=MCP_HOST,
    port=MCP_PORT,
)

server.add_tool(
    build_lead_profiles,
    name="build_lead_profiles",
    title="Build lead profiles",
)


def main() -> None:
    server.run(transport=MCP_TRANSPORT)


if __name__ == "__main__":
    main()
