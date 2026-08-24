"""The MCP *client* — how this agent reaches the HubSpot MCP container.

JSON-RPC over HTTP: initialize -> notifications/initialized -> tools/list
-> tools/call. Tool names are discovered and bound at startup, never
hard-coded into agent logic, so a rename on the server is a config change.
"""
