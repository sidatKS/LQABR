# Not dead code: ADK's agent discovery (`adk web/run/api_server`) requires the
# package to import its agent module — this line is what makes `src` loadable
# as an ADK app at all (see the quickstart's documented layout).
from . import agent  # noqa: F401
