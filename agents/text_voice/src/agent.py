"""ADK discovery shim — re-exports root_agent so `adk web/run/api_server
agents/text_voice/src` finds this directory as a single-agent module."""

from __future__ import annotations

from .text_voice_agent import root_agent

__all__ = ["root_agent"]
