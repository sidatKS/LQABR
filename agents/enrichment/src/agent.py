"""ADK discovery shim for the Lead Profile (enrichment) agent.

The ADK CLI resolves an agent directory in one of two ways:
  - as a *parent* of agent packages, listing its sub-directories, or
  - as a *single agent* directory, which it detects by the presence of a file
    named exactly `agent.py` (see google.adk.cli.utils.agent_loader.
    is_single_agent_directory), then loading `<dir>.agent.root_agent`.

The real agent lives in lead_profile_agent.py as one self-contained module
(pure join logic + ADK wrapper, no relative imports), which the tests import
flat. This file only re-exports its `root_agent` under the name the ADK loader
looks for, so `adk web/run/api_server agents/enrichment/src` discovers it
without splitting that module apart.
"""

from __future__ import annotations

from .lead_profile_agent import root_agent

__all__ = ["root_agent"]
