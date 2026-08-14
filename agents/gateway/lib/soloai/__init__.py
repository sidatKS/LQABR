"""Solo AI runtime adapter — the gateway's protocol and audit layer.

WHAT THIS IS
------------
Rev 3 names "Solo AI (open source)" as the orchestrator runtime that gives
the gateway its protocol handling (HTTP / MCP / A2A) and its customizable
auditing, co-located in ``lib/soloai`` — "one process, one deploy, no extra
hop".

The product is Solo.io's **agentgateway** (https://agentgateway.dev, now
hosted by the Linux Foundation). Checked before writing this: it is a
standalone data plane written in Rust, run as ``agentgateway -f config.yaml``,
configured with static YAML (binds -> listeners -> routes -> backends), with
MCP and A2A backend types, Prometheus metrics and OpenTelemetry tracing, and
a UI on :15000. **There is no Python library to import.**

So "co-located library, in-process" is not buildable as written. What is
buildable, and what this package is:

  * agentgateway runs as a **sidecar inside the gateway's own container** —
    still one deploy, one release artifact, no extra network hop off-box.
    Its static config is ``config/agentgateway.yaml``.
  * this package is the **Python adapter** over it: the same module tree Rev 3
    specifies (``protocols/http.py``, ``protocols/mcp.py``, ``protocols/a2a.py``,
    ``audit_hooks.py``), with the gateway's business logic depending only on
    these interfaces.

Recorded as **D-02** in the deviation register. Everything else in Rev 3 is
preserved: single ingress, trigger-only payload, HubSpot as system of record,
four log streams, one process, one deploy.

Because the business logic (``router`` / ``audit`` / ``dispatch``) talks only
to these interfaces, replacing the adapter with a real in-process library —
should one ever ship — is a change inside this package alone.
"""

from __future__ import annotations

from .config import Config, load_config, load_registry_document
from .audit_hooks import (
    AuditHooks,
    ProfileFieldLeak,
    Stream,
    new_run_id,
)

__all__ = [
    "AuditHooks",
    "Config",
    "ProfileFieldLeak",
    "Stream",
    "load_config",
    "load_registry_document",
    "new_run_id",
    "RUNTIME",
]

#: What the adapter is actually sitting on. Reported by ``/healthz`` and
#: written to the system stream at startup so a running container can be
#: asked which runtime it believes it has.
RUNTIME = {
    "name": "agentgateway",
    "vendor": "solo.io",
    "project_url": "https://agentgateway.dev",
    "deployment": "sidecar in the gateway container (one process group, one deploy)",
    "protocols": ["http", "mcp", "a2a"],
    "adapter": "lib/soloai (this package)",
    "deviation": "D-02 — co-located process, not an in-process library",
}
