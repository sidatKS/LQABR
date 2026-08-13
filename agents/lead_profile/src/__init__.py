"""ADK agent package.

``adk run agents/lead_profile/src`` and ``adk web agents`` discover the agent
by importing this package and reading ``agent.root_agent`` — so this import is
load-bearing, not decorative.
"""

from . import agent
from .agent import root_agent

__all__ = ["agent", "root_agent"]
