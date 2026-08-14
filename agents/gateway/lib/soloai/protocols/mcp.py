"""MCP protocol — declared here, deliberately not used to move data.

Rev 3 Step 6: *An MCP request arrives directly from an agent. This path is
direct and bypasses the gateway.* And: *Full profile data flows here only —
never through the gateway.*

So this module is short on purpose. It does two things:

    1. publishes the MCP endpoint and the ``chunk_size_hint`` the agents need,
       so both live in ``config.yaml`` as Rev 3 requires rather than being
       hard-coded in four agents;
    2. refuses to become a profile proxy — ``assert_not_proxying`` fails
       startup if someone flips ``protocols.mcp.proxy_profile_data`` to true.

The MCP listener itself is the agentgateway sidecar's; the agents connect to
it directly with the private-app token. The gateway never brokers that call,
which is why there is no client in this file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


class MCPConfigError(RuntimeError):
    """The MCP configuration would put profile data through the gateway."""


@dataclass(frozen=True)
class MCPEndpoint:
    """What the gateway tells agents about the CRM path.

    ``chunk_size_hint`` is the bound on agent memory footprint (Rev 3 Step 6:
    ~5 profiles per trigger). The gateway publishes it; the agent applies it
    when it scopes its own MCP request by criteria — HubSpot cannot resolve a
    chunk by trigger id on the free tier (D-01).
    """

    base_url: str
    enabled: bool = True
    chunk_size_hint: int = 5
    proxy_profile_data: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "base_url": self.base_url,
            "enabled": self.enabled,
            "chunk_size_hint": self.chunk_size_hint,
            "proxy_profile_data": self.proxy_profile_data,
            "path": "agent -> HubSpot, direct; bypasses the gateway (Rev 3 Step 6)",
        }

    def assert_not_proxying(self) -> None:
        if self.proxy_profile_data:
            raise MCPConfigError(
                "protocols.mcp.proxy_profile_data is true — the gateway would carry "
                "lead-profile data, which the design forbids (trigger-only payload). "
                "Agents must resolve profiles directly from HubSpot over MCP."
            )


def endpoint_from_config(config: Any) -> Optional[MCPEndpoint]:
    """Build the published endpoint from ``config.yaml``; ``None`` when off."""
    if not config.get("protocols.mcp.enabled", False):
        return None
    endpoint = MCPEndpoint(
        base_url=str(config.get("protocols.mcp.sidecar_base_url", "http://127.0.0.1:3000")),
        enabled=True,
        chunk_size_hint=int(config.get("lead_profile.chunk_size_hint", 5)),
        proxy_profile_data=bool(config.get("protocols.mcp.proxy_profile_data", False)),
    )
    endpoint.assert_not_proxying()
    return endpoint
