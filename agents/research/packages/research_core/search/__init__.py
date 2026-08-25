"""Web search — the outside-world half of a research note.

`base.SearchProvider` is the contract; `anthropic_search.AnthropicWebSearch`
is the implementation in use. Swapping the provider is a config change
(``LQABR_RESEARCH_SEARCH_PROVIDER``), never an edit to the pipeline.
"""

from .base import SearchError, SearchProvider, build_provider
from .anthropic_search import AnthropicWebSearch

__all__ = ["SearchError", "SearchProvider", "build_provider", "AnthropicWebSearch"]
