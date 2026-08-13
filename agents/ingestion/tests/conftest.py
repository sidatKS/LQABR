import sys

import pytest
from pathlib import Path

# This agent's src/ is imported by bare module name (agent, tools, ...). Those
# names collide across the six agents when the WHOLE repo runs in one pytest
# process: whichever agent's src/ was inserted last shadows the others, and
# sys.modules caches the loser. Guard: evict modules loaded from a sibling
# agent's src/ (already-collected sibling tests keep their references) and put
# THIS agent's src/ at the front, so bare imports below always resolve locally.
# A no-op when the agent's tests run alone.
SRC = Path(__file__).resolve().parents[1] / "src"
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


# Conftest import time: needed for this conftest's own bare imports below, and
# sufficient when this agent's tests run alone.
_activate_src()


# Test-module import time: in a whole-repo run every conftest has already
# executed before ANY test module imports, so whichever src/ activated last
# would win. Re-activate ours just before each of our modules is imported.
def pytest_collectstart(collector):
    # Fires immediately before a collector runs — for a Module, that is the
    # moment the test file is imported (pytest may build all Module nodes
    # first and import them in a later pass, so pytest_pycollect_makemodule
    # is too early). Not directory-scoped: every conftest's impl runs for
    # every collector, so act only on modules in this directory.
    path = getattr(collector, "path", None)
    if (
        path is not None
        and Path(str(path)).suffix == ".py"
        and Path(str(path)).is_relative_to(Path(__file__).parent)
    ):
        _activate_src()


def pytest_collection_finish(session):
    # Every test module is imported by the end of collection. Capture our
    # loaded modules NOW, before any sibling agent's run-phase activation
    # evicts them — restoring these same objects preserves class identity
    # for isinstance checks against classes bound at collection time.
    for _name, _mod in list(sys.modules.items()):
        if _module_src_dir(_mod) == SRC:
            _OWN_MODULES[_name] = _mod


# Test RUN time: a test body may import bare names lazily (the discovery
# tests do `import agent` inside the test), and sibling collections may have
# evicted our modules since. Re-activate around every test here.
@pytest.fixture(autouse=True)
def _agent_src_active():
    _activate_src()
    yield
