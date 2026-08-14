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
import types
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
    spec = importlib.util.spec_from_file_location(module_name, SRC / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _ensure_adk_stub() -> None:
    """Make `google.adk`/`google.genai` importable when the real SDKs are absent.

    `text_voice.py` builds `root_agent` at module scope as a `TextVoiceAgent`
    (a custom `google.adk.agents.BaseAgent` subclass — no model, no template
    or graph workflow, see its docstring for why). That needs, at import
    time: `BaseAgent` itself, `InvocationContext` (the `_run_async_impl`
    parameter type), `Event` (what it yields), and `google.genai.types`'
    `Content`/`Part` (what a reply's `content=` is built from). None of
    Steps 3/7/8's logic under test actually drives the ADK runtime — no test
    here instantiates a real `InvocationContext` or runs `_run_async_impl`
    end-to-end — but the bare imports are enough to make the whole module
    uncollectable wherever `google-adk`/`google-genai` were never installed,
    and pulling in the real packages plus their transitive deps just to
    satisfy an import is unnecessary here.

    If the real `google.adk`/`google.genai` genuinely resolve (installed,
    e.g. in CI/prod), each piece below is skipped and the real class is used —
    this only fills gaps, it never shadows a real install.
    """
    google_mod = sys.modules.get("google")
    if google_mod is None:
        google_mod = types.ModuleType("google")
        google_mod.__path__ = []  # namespace package
        sys.modules["google"] = google_mod

    adk_mod = sys.modules.get("google.adk")
    if adk_mod is None:
        adk_mod = types.ModuleType("google.adk")
        adk_mod.__path__ = []
        sys.modules["google.adk"] = adk_mod
        google_mod.adk = adk_mod

    try:
        import google.adk.agents  # noqa: F401
    except ImportError:
        agents_mod = types.ModuleType("google.adk.agents")

        class _StubBaseAgent:
            """Mimics enough of BaseAgent for TextVoiceAgent to subclass.

            Never executed as a real ADK runtime by these tests — just needs
            to accept `super().__init__(name=..., description=...)` and set
            `self.name`, since `TextVoiceAgent._run_async_impl` reads it.
            """

            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                self.name = kwargs.get("name", "")
                self.description = kwargs.get("description", "")

        agents_mod.BaseAgent = _StubBaseAgent
        adk_mod.agents = agents_mod
        sys.modules["google.adk.agents"] = agents_mod

    try:
        import google.adk.agents.invocation_context  # noqa: F401
    except ImportError:
        ctx_mod = types.ModuleType("google.adk.agents.invocation_context")

        class _StubInvocationContext:
            """Never instantiated by these tests — they build their own fake
            ctx-like object with a `.user_content` attribute directly; this
            only needs to exist so `from ... import InvocationContext`
            (used purely as a type hint in text_voice.py) resolves."""

        ctx_mod.InvocationContext = _StubInvocationContext
        sys.modules["google.adk.agents"].invocation_context = ctx_mod
        sys.modules["google.adk.agents.invocation_context"] = ctx_mod

    try:
        import google.adk.events  # noqa: F401
    except ImportError:
        events_mod = types.ModuleType("google.adk.events")

        class _StubEvent:
            """Records author/content; never executed by these tests."""

            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs
                self.author = kwargs.get("author")
                self.content = kwargs.get("content")

        events_mod.Event = _StubEvent
        adk_mod.events = events_mod
        sys.modules["google.adk.events"] = events_mod

    try:
        import google.genai.types  # noqa: F401
    except ImportError:
        genai_mod = sys.modules.get("google.genai")
        if genai_mod is None:
            genai_mod = types.ModuleType("google.genai")
            genai_mod.__path__ = []
            sys.modules["google.genai"] = genai_mod
            google_mod.genai = genai_mod

        genai_types_mod = types.ModuleType("google.genai.types")

        class _StubPart:
            def __init__(self, *args, **kwargs):
                self.text = kwargs.get("text")

        class _StubContent:
            def __init__(self, *args, **kwargs):
                self.parts = kwargs.get("parts", [])

        genai_types_mod.Part = _StubPart
        genai_types_mod.Content = _StubContent
        genai_mod.types = genai_types_mod
        sys.modules["google.genai.types"] = genai_types_mod


@pytest.fixture(scope="session")
def tv_tools():
    """agents/text_voice/src/tools.py — Steps 2 and 4 (the FastAPI app)."""
    return _load("tv_tools", "tools.py")


@pytest.fixture(scope="session")
def tv_agent():
    """agents/text_voice/src/text_voice.py — Steps 3, 7 and 8.

    No `google.adk`/`google.genai` imports at all — `root_agent` lives in
    `adk_agent.py` instead, specifically so production's import chain
    (tools.py -> text_voice.py) never needs `google-adk` installed. No ADK
    stub needed for this fixture any more.
    """
    return _load("tv_text_voice", "text_voice.py")


@pytest.fixture(scope="session")
def tv_adk_agent(tv_agent):
    """agents/text_voice/src/adk_agent.py — root_agent, the custom BaseAgent.

    Depends on `tv_agent` and aliases `sys.modules["text_voice"]` to that
    same already-loaded module object *before* importing `adk_agent.py`, so
    `adk_agent.py`'s `import text_voice` fallback (its relative import fails
    here the same way it does under `adk run`, since `_load()`'s modules
    have no real package context) resolves to the exact module these tests
    monkeypatch — not a second, independent load of the same file under the
    plain name "text_voice" (which `SRC` being on `sys.path` would otherwise
    make possible, and which would silently defeat every monkeypatch below).
    """
    _ensure_adk_stub()
    sys.modules.setdefault("text_voice", tv_agent)
    return _load("tv_adk_agent", "adk_agent.py")


@pytest.fixture(autouse=True)
def _isolate_shared_clients(monkeypatch):
    """Reset the process-wide Vapi client between tests.

    It is a memoised singleton holding a requests.Session (see
    `tools.reset_vapi_client`), so a client built with one test's fake
    credentials would otherwise leak into the next test. HubSpot has no
    equivalent module-level singleton to reset: `HubSpotClient` instances are
    owned by whoever constructs them (a test's `make_client`, or
    `_MCPAdapter._crm()`), so tests isolate HubSpot by injecting their own
    fake client/session rather than resetting a shared one.
    """
    monkeypatch.setenv("LQABR_LOG_JSON", "0")
    yield
    for name in ("tv_tools",):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "reset_vapi_client"):
            module.reset_vapi_client()
