"""Shared fixtures. Every test in this suite runs OFFLINE.

No test may open a socket, read a developer's .env, or need an API key.
The fake MCP server and the mocked HTTP transport arrive in P2/P1; this
file exists from P0 so the guarantee is stated before the first test that
could break it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

AGENT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch):
    """Strip every LQABR_SUMMARY_* var and provider key from the test
    environment. A test's behaviour must come from its own fixtures, never
    from the machine it runs on."""
    for key in list(os.environ):
        if key.startswith("LQABR_SUMMARY_") or key in {
            "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "HUBSPOT_ACCESS_TOKEN",
        }:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LQABR_SUMMARY_SECRETS_SOURCE", "env")


@pytest.fixture
def agent_root() -> Path:
    return AGENT_ROOT
