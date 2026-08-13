import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
REPO_ROOT = Path(__file__).resolve().parents[3]

for _path in (str(REPO_ROOT), str(SRC)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from email_fakes import FakeCRM, FakeMailgun, FakeSession  # noqa: E402


@pytest.fixture
def run_ctx():
    import observability as obs
    return obs.RunContext(object_id="trg-1", run_id="run-1")


@pytest.fixture
def store(tmp_path):
    """A RunStateStore isolated to the test's tmp dir — never the repo."""
    from runstate import RunStateStore
    return RunStateStore(directory=tmp_path / "runstate")


@pytest.fixture
def fake_crm():
    return FakeCRM


@pytest.fixture
def fake_session():
    return FakeSession


@pytest.fixture
def fake_mailgun():
    return FakeMailgun


@pytest.fixture
def fake_model_fn():
    """The one model call per lead, faked.

    Construction is instruction-based, so every path through step 6 needs a
    model — there is no template to render without one. The draft leaves the
    reserved markers in place so tests can still assert that code, not the
    model, substitutes them."""
    def model_fn(prompt_body, facts):
        who = " ".join(p for p in (facts.get("first_name", ""),
                                   facts.get("last_name", "")) if p) or "there"
        company = facts.get("company_id", "")
        return (f"A note for {company}".strip(),
                f'<p>Hi {who},</p><p>Drafted for {company}. '
                '<a href="{cta_url}">Overview</a></p><p>{sender_name}</p>')
    return model_fn
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


@pytest.fixture(scope="session")
def email_webhook_app():
    """Load this agent's webhook_app under a unique module name (several
    agents ship a module called webhook_app)."""
    spec = importlib.util.spec_from_file_location("email_webhook_app", SRC / "webhook_app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["email_webhook_app"] = module
    spec.loader.exec_module(module)
    return module
