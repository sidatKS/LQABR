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
