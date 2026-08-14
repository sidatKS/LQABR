"""mcp/hubspot — the central HubSpot tool folder.

    auth.py     STEP 4  machine-to-machine token, shared by every agent
    schema.py           HubSpot schema validation, both directions
    crm.py              all HubSpot REST work
    server.py   STEP 5  get_lead_profile_details
                STEP 9  post / patch CRM

Observability: this folder is shared by every agent, so it must not import
any single agent's logging. Callers pass an `ObservabilitySink` — anything
with `.audit(**fields)` and `.process(**fields)` — and get a no-op if they
pass nothing. The email agent's implementation is
`agents/email/src/observability.py::MCPObservability`.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ObservabilitySink(Protocol):
    """Minimal logging contract the MCP writes through. Implemented per
    agent so each agent's own streams receive the MCP's records."""

    def audit(self, **fields: Any) -> None: ...

    def process(self, **fields: Any) -> None: ...


class NullObservability:
    """Default sink — the MCP stays usable (and silent) when a caller has no
    logging wired, e.g. in a unit test."""

    def audit(self, **fields: Any) -> None:
        return None

    def process(self, **fields: Any) -> None:
        return None


__all__ = ["ObservabilitySink", "NullObservability"]
