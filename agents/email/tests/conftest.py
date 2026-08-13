import importlib.util
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture(scope="session")
def email_webhook_app():
    """Load this agent's webhook_app under a unique module name (several
    agents ship a module called webhook_app)."""
    spec = importlib.util.spec_from_file_location("email_webhook_app", SRC / "webhook_app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["email_webhook_app"] = module
    spec.loader.exec_module(module)
    return module
