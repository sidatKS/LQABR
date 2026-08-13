import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from mcp_fakes import FakeResponse, FakeSession, RecordingObs  # noqa: E402


@pytest.fixture
def fake_response():
    return FakeResponse


@pytest.fixture
def fake_session():
    return FakeSession


@pytest.fixture
def obs_sink():
    return RecordingObs()


@pytest.fixture(autouse=True)
def _no_secret_cache():
    from lqabr_core.secrets import get_secret
    get_secret.cache_clear()
    yield
    get_secret.cache_clear()
