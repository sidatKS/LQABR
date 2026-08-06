"""Shared model-provider wiring — one place to swap Gemini <-> another
provider, so agents never hardcode a provider-specific model object.

A bare Gemini model string (e.g. "gemini-2.0-flash") passes straight
through to ADK's native Gemini client, unchanged from before. Any other
provider string (e.g. "anthropic/claude-sonnet-5") is wrapped in ADK's
LiteLlm so the request routes through litellm instead. Agents keep
importing a plain model-name string from their own env var
(LQABR_<AGENT>_MODEL) — only the wrapping decision lives here.

Requires the `google-adk[extensions]` extra installed when using a
non-Gemini model string (pip install "google-adk[extensions]"; already
listed in requirements.txt for agents that default to it).
"""

from __future__ import annotations

from typing import Any


def build_model(model_name: str) -> Any:
    """Return what ADK's `Agent(model=...)` should receive for this
    model name — the bare string for Gemini, or a LiteLlm wrapper for
    everything else."""
    if model_name.startswith("gemini"):
        return model_name

    from google.adk.models.lite_llm import LiteLlm  # pip install "google-adk[extensions]"

    return LiteLlm(model=model_name)
