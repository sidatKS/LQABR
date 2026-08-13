"""ADK discovery shim — re-exports root_agent so `adk web/run/api_server
agents/orchestrator/src` finds this directory as a single-agent module."""

from __future__ import annotations

from .orchestrator_agent import root_agent

__all__ = ["root_agent"]
