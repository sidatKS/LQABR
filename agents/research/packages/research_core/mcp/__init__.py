"""The Research Agent's connection to the central HubSpot MCP.

This is the ONLY door to HubSpot. The agent holds no HubSpot token and makes
no call to api.hubapi.com: every read and every write goes through the MCP
container, which owns authentication, schema validation and the audit trail.
"""

from .client import MCPClient, MCPError, MCPToolMissing, unwrap_result
from .hubspot import HubSpotMCP

__all__ = ["MCPClient", "MCPError", "MCPToolMissing", "unwrap_result", "HubSpotMCP"]
