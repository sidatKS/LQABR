"""research_core — the Research Agent's OWN library.

Standalone on purpose: this package imports nothing from the rest of the repo
(no ``lqabr_core``, no ``mcp.hubspot``, no shared ``tests/``). The single
coupling to the platform is a RUNTIME one — the HubSpot MCP URL. That is a
URL, not an import, and ``tests/test_standalone.py`` fails the build if it
ever stops being true.
"""

__all__ = ["settings", "obs", "types"]
