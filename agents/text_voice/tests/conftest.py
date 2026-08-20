"""Fixtures for the Rev 5 text_voice agent's tests.

`tools.py` and `text_voice.py` are loaded by file path under unique module
names because several agents in this repo ship modules with the same basename,
and because `src/` is not a package on sys.path when pytest collects from the
repo root.

Both modules are importable without credentials: the HubSpot and Vapi clients
are built lazily on first use, not at import time. The env defaults below make
that explicit rather than accidental — a test that reaches a real provider
should fail on a missing credential, not silently place a phone call.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
# Evict bare-name modules loaded from a sibling agent's src/ (they collide in
# a whole-repo pytest run) and keep THIS agent's src/ at the path front. A
# no-op when the agent's tests run alone.
_AGENTS_DIR = SRC.parents[1]
_OWN_MODULES: dict = {}


def _module_src_dir(mod):
    file = getattr(mod, "__file__", None)
    if not file:
        return None
    path = Path(file)
    if _AGENTS_DIR not in path.parents:
        return None
    rel = path.relative_to(_AGENTS_DIR).parts
    if len(rel) >= 2 and rel[1] == "src":
        return _AGENTS_DIR / rel[0] / "src"
    return None


def _activate_src() -> None:
    """Make bare imports (agent, tools, ...) resolve to THIS agent's src/,
    preserving module identity across re-activations.

    1. Capture every module already loaded from our src/ (so a class imported
       at collection time stays the SAME object at run time).
    2. Evict modules loaded from a sibling agent's src/ — their bare names
       (agent, webhook_app, ...) collide with ours. The sibling's own
       activation restores its captured modules before its tests run.
    3. Restore our captured modules and put our src/ at the path front.
    """
    for name, mod in list(sys.modules.items()):
        if _module_src_dir(mod) == SRC:
            _OWN_MODULES[name] = mod
    for name in [n for n, m in list(sys.modules.items()) if _module_src_dir(m) not in (None, SRC)]:
        del sys.modules[name]
    sys.modules.update(_OWN_MODULES)
    while str(SRC) in sys.path:
        sys.path.remove(str(SRC))
    sys.path.insert(0, str(SRC))


def _load(module_name: str, filename: str):
    # _activate_src() must run BEFORE the module body executes. text_voice.py
    # falls back to a bare `from tools import ...` when its relative import
    # fails (which it always does here — _load gives the module no package
    # context), so without SRC on sys.path every fixture that loads it dies
    # with ModuleNotFoundError: No module named 'tools'. This call was missing:
    # _activate_src was defined and never invoked, which errored 60 of the 87
    # tests in this suite at setup. Verified by running the suite before and
    # after (2026-08-17).
    _activate_src()
    spec = importlib.util.spec_from_file_location(module_name, SRC / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# Activate at conftest IMPORT time as well as inside _load(). test_fixes.py does
# a bare `import tools` / `import text_voice` at module scope, which pytest
# executes during COLLECTION — before any fixture runs — so without this the
# module is uncollectable (ModuleNotFoundError: No module named 'tools') and
# pytest aborts the whole run with "Interrupted: 1 error during collection".
# _activate_src() is idempotent and re-runnable by design, so calling it here
# does not weaken the sibling-agent module eviction it performs per _load().
_activate_src()


@pytest.fixture(scope="session")
def tv_tools():
    """agents/text_voice/src/tools.py — Steps 2 and 4 (the FastAPI app)."""
    return _load("tv_tools", "tools.py")


@pytest.fixture(scope="session")
def tv_agent():
    """agents/text_voice/src/text_voice.py — Steps 3, 7 and 8.

    No `google.adk`/`google.genai` imports at all. ADK was removed from this
    agent entirely on 2026-08-18 — the only surface that ever used it
    (`adk_agent.py`/`agent.py`, the `adk web` console) is retired, and the
    production path is `uvicorn tools:app`. No ADK stub is needed anywhere.
    """
    return _load("tv_text_voice", "text_voice.py")


@pytest.fixture(autouse=True)
def _isolate_shared_clients(monkeypatch):
    """Reset the process-wide Vapi client between tests.

    It is a memoised singleton holding a requests.Session (see
    `tools.reset_vapi_client`), so a client built with one test's fake
    credentials would otherwise leak into the next test. HubSpot has no
    equivalent module-level singleton to reset: since the 2026-08-19 MCP
    switchover this agent holds no HubSpot client at all — tests monkeypatch
    `text_voice.mcp` (the StepFiveMCPClient instance) with a fake, and the
    client itself opens no connection until its first tool call.
    """
    monkeypatch.setenv("LQABR_LOG_JSON", "0")
    yield
    for name in ("tv_tools",):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "reset_vapi_client"):
            module.reset_vapi_client()
