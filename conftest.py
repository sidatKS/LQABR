"""Repo-root pytest conftest.

Provides a lightweight google.adk stub when the real package isn't
installed, so agent modules (which build `root_agent` at import time) are
testable in any environment. When google-adk IS installed, the real package
is used untouched.
"""

from __future__ import annotations

import sys
import types

try:  # pragma: no cover - depends on environment
    import google.adk.agents  # noqa: F401
except ImportError:  # build a minimal stub
    google_mod = sys.modules.setdefault("google", types.ModuleType("google"))
    adk_mod = types.ModuleType("google.adk")
    agents_mod = types.ModuleType("google.adk.agents")

    class Agent:  # minimal stand-in for google.adk.agents.Agent
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    agents_mod.Agent = Agent
    adk_mod.agents = agents_mod
    google_mod.adk = adk_mod
    sys.modules["google.adk"] = adk_mod
    sys.modules["google.adk.agents"] = agents_mod
