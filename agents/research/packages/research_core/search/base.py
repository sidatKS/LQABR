"""The search contract.

One method, typed, so the pipeline never knows which vendor answered. A
provider returns grounded prose plus the URLs it actually cited — the citations
matter as much as the text: an ungrounded claim in a lead-context note becomes
an ungrounded claim in an outreach email.
"""

from __future__ import annotations

from typing import Protocol

from ..settings import Settings
from ..types import ResearchFindings


class SearchError(RuntimeError):
    """The search could not be performed. Always names what failed."""


class SearchProvider(Protocol):
    """What the pipeline depends on. Implementations live beside this file."""

    name: str

    def research(self, prompt: str, *, system: str = "") -> ResearchFindings:
        """Run the research and return grounded findings with citations."""
        ...


def build_provider(settings: Settings, **kwargs) -> SearchProvider:
    """Config-driven provider selection. Adding a vendor means adding a branch
    here and a module beside it — never a change in the pipeline."""
    from .anthropic_search import AnthropicWebSearch
    return AnthropicWebSearch(settings=settings, **kwargs)
