"""LQABR Agent Gateway — business logic for Rev 3 Steps 2-4.

    server.py    HTTP ingress, entry point (the only entry point)
    router.py    Step 2 — filter value, resolve endpoint, mint trigger_id
    audit.py     Steps 3 & 7 — run/trigger id + routing decision, four streams
    dispatch.py  Step 4 — hand off to the agent, trigger_id only

The runtime adapter (protocols + audit hooks) is co-located at
``agents/gateway/lib/soloai`` and put on ``sys.path`` by ``server.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Keep `import soloai` working regardless of which module is imported first.
_LIB = Path(__file__).resolve().parents[1] / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

__all__ = ["audit", "dispatch", "router", "server"]
