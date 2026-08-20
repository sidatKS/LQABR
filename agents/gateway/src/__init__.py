"""LQABR Agent Gateway — business logic for Rev 3 Steps 2-4.

    server.py    HTTP ingress, entry point (the only entry point)
    router.py    Step 2 — filter value, resolve endpoint, mint trigger_id
    audit.py     Steps 3 & 7 — run/trigger id + routing decision, four streams
    dispatch.py  Step 4 — hand off to the agent, trigger_id only

The runtime adapter (protocols + audit hooks) is co-located at
``agents/gateway/lib/soloai`` and put on ``sys.path`` by ``server.py``.

Modules here use flat imports (``from router import ...``) — production runs
``uvicorn server:app`` from ``src/`` and the tests load them standalone, so
this file is documentation, not an import path.
"""

from __future__ import annotations

__all__ = ["audit", "dispatch", "router", "server"]
