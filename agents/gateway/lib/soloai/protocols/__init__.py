"""Protocol handling for the gateway.

Rev 3, "Orchestrator Runtime — Solo AI": *Handles all protocols the gateway
needs: HTTP (ingress), MCP (agent->CRM), and A2A (gateway->agent hand-off).*

One module per protocol, each importable on its own so the business logic in
``src/`` never imports a transport library directly:

    http.py   inbound  — HubSpot webhook ingress: signature, batch, concurrency
    a2a.py    outbound — gateway -> agent hand-off, trigger_id only
    mcp.py    sidecar  — agent -> HubSpot, direct; the gateway only publishes
                         the endpoint and the chunk hint, it carries no data
"""

from __future__ import annotations

from . import a2a, http, mcp

__all__ = ["a2a", "http", "mcp"]
