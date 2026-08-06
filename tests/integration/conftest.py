import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

for src in (REPO_ROOT / "agents" / "orchestrator" / "src",
           REPO_ROOT / "agents" / "email" / "src"):
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
