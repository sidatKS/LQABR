"""ADK discovery shim — re-exports root_agent so `adk web/run/api_server
agents/email/src` finds this directory as a single-agent module.

This module has one job beyond re-exporting: putting THIS directory on
`sys.path` before anything else is imported.

Two things load the code in this folder, and they set the path up
differently:

  adk web agents/email/src   imports it as a PACKAGE (`src.agent`), which
                             puts `agents/email/` on sys.path — but not
                             `agents/email/src/`.
  uvicorn service_app:app    is run FROM this directory, so cwd covers it.

Every module in here imports its siblings flat (`import outreach`,
`import events`). Without the insert below, `adk web` fails at
`email_agent.py` with ModuleNotFoundError on a sibling module, because the
`adk web` path is the only one that does not already put this directory on
sys.path.

Inserting here rather than converting every sibling to a relative import
keeps one import style across both entry points.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SRC_DIR = str(Path(__file__).resolve().parent)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


def _load_local_env() -> None:
    """Load agents/email/.env BEFORE email_agent is imported.

    `email_agent.py` reads LQABR_EMAIL_MODEL at import time and hands it to
    build_model(), which resolves the provider key — so the environment has to
    be in place before that import, not after. `service_app.py` does the same
    thing for the uvicorn entry point; this is the ADK one.

    Do not rely on ADK's own dotenv handling: it looks in its own places, and
    which of them wins has changed between versions. Loading it here means
    `adk run`, `adk web` and `uvicorn` all see identical configuration.

    override=False so Cloud Run's injected environment stays authoritative.
    Skipped under pytest so a developer's real credentials never leak into a
    test run."""
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return
    try:
        from dotenv import load_dotenv
    except ImportError:          # python-dotenv absent — use the ambient env
        return
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


_load_local_env()

from email_agent import root_agent  # noqa: E402  (must follow the path insert + env load)

__all__ = ["root_agent"]
