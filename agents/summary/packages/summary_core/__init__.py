"""summary_core — the Summary Agent's OWN library.

Deliberately private to `agents/summary`. Nothing here may import
`lqabr_core` or the repo-root `mcp` package: this agent is patched,
tested and deployed on its own, so it carries its own copies of the few
things it needs (settings, secrets, observability, HTTP retries).

`summary_core.mcp` is THIS package's MCP *client* — the thing that dials
the HubSpot MCP container at runtime. It is not, and must never become,
an import of the repo's in-process `mcp/hubspot`.

The rule is enforced mechanically by tests/test_standalone.py.
"""

__version__ = "0.1.0"
