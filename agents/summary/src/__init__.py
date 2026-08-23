"""ADK agent package.

``adk run agents/summary/src`` and ``adk web agents`` discover the agent by
importing this package and reading ``agent.root_agent`` — so this import is
load-bearing, not decorative.

It is guarded because the same directory is imported by the tests as plain
modules (``pythonpath = ../src``), where google-adk may not be installed and
is not needed: the pipeline, the summariser, the tools and the HTTP surface
have no ADK dependency at all. If ADK is absent, the package still imports
and only ``root_agent`` is missing.
"""

try:  # pragma: no cover - exercised by `adk`, not by the suite
    from . import agent
    from .agent import root_agent

    __all__ = ["agent", "root_agent"]
except ImportError:  # google-adk not installed — every other surface still works
    __all__ = []
