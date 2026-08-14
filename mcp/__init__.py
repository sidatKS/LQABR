"""LQABR central MCP folder — lives at the project ROOT, not under any agent.

One copy of the HubSpot tool surface that the email, text/voice and
scheduling agents all reference. It is declared as a dependency of each
agent's container and loaded **in-process at runtime**: no second process,
no second instance, no MCP service running on its own.

Agents never make a direct HubSpot call. The profile payload moves
HubSpot <-> agent over this tool surface only — never through the gateway.

NOTE on the package name: this directory is named `mcp` to match the
approved v2 layout. It deliberately does not import the PyPI package also
called `mcp` (the Model Context Protocol SDK); because the design mandates
in-process loading rather than a standalone server, that SDK is not a
dependency of this repo and the names never collide at import time. If a
future change does need the SDK, rename this package first.
"""
