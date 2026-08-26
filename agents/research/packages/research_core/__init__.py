"""research_core — the Research Agent's OWN library.

Standalone on purpose: this package imports nothing from the rest of the repo
(no ``lqabr_core``, no ``mcp.hubspot``, no shared ``tests/``). The single
coupling to the platform is a RUNTIME one — the HubSpot MCP URL. That is a
URL, not an import, and ``tests/test_standalone.py`` fails the build if it
ever stops being true.
"""

from pathlib import Path


def _version() -> str:
    """The agent's version, from the VERSION file that exists for it.

    Six literals used to spell "0.1.0" while the MCP handshake sent "0.1" —
    drift that had already happened by the time anyone looked.
    """
    try:
        return (Path(__file__).resolve().parents[2] / "VERSION").read_text(
            encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


__version__ = _version()

SERVICE_NAME = "lqabr-research-agent"

__all__ = ["settings", "obs", "types", "__version__", "SERVICE_NAME"]
