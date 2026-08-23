"""Input adapters — one per source kind, all returning NormalizedDocument.

    url   a web page          (the ported blog crawler)
    json  a raw JSON payload  (any shape, optionally field-selected)
    api   another HTTP/FastAPI endpoint
    text  plain text, passed through

Adding a fifth source is a new module plus one registry entry. The agent,
its tools and its HTTP surface do not change.
"""

from .base import available_kinds, fetch, get_adapter, guard_url, register, select_path  # noqa: E402,F401

# Importing the package registers every adapter — a caller does `from
# summary_core import sources` and all four kinds are available.
from . import api, json_source, text, web  # noqa: E402,F401

__all__ = ["fetch", "available_kinds", "get_adapter", "guard_url", "register",
           "select_path", "api", "json_source", "text", "web"]
