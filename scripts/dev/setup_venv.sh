#!/usr/bin/env bash
# One shared WSL venv for the LQABR local MVP. Idempotent.
set -euo pipefail
cd "$(dirname "$0")/../.."          # repo root
ROOT="$(pwd)"
echo "repo root: $ROOT"

python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip

# third-party deps (google-adk unpinned -> latest)
pip install -r scripts/dev/requirements-shared.txt

# lqabr_core editable so the root suite + agents can import it
pip install -e packages/lqabr_core

echo "===== RESOLVED VERSIONS ====="
python -c "import importlib.metadata as m; \
print('google-adk ', m.version('google-adk')); \
print('litellm    ', m.version('litellm')); \
print('fastapi    ', m.version('fastapi')); \
print('pydantic   ', m.version('pydantic'))"
