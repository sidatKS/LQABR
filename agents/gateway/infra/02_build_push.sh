#!/usr/bin/env bash
# Build the gateway image. Context is the REPO ROOT - see infra/cloudbuild.yaml.
set -euo pipefail
source "$(dirname "$0")/config.sh"
REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "${REPO_ROOT}"

# Refuse to build a config that would deploy an open ingress. gateway_deploy_spec 5:
# config.yaml is COPYed into the image and load_config() accepts no env override,
# so this value is fixed at BUILD time - there is no chance to correct it later.
PYTHONPATH=agents/gateway/lib python3 - <<'PY'
import sys
from soloai.config import load_config
cfg = load_config()
sig = cfg.get("gateway.ingress.signature.enabled")
sink = cfg.get("audit.sink")
if sig is not True:
    sys.exit("REFUSING TO BUILD: gateway.ingress.signature.enabled is %r, not true.\n"
             "  The ingress is public; the v3 HMAC is the only thing defending it.\n"
             "  Fix agents/gateway/config/config.yaml (gateway_deploy_spec 4.2/5)." % (sig,))
if sink != "stdout":
    print("WARN: audit.sink resolves to %r, not 'stdout' - the audit stream will not "
          "reach Cloud Logging (gateway_deploy_spec 5)." % (sink,))
print("preflight ok: signature.enabled=True  audit.sink=%r" % (sink,))
PY

gcloud builds submit . \
  --project "${PROJECT_ID}" \
  --config agents/gateway/infra/cloudbuild.yaml \
  --substitutions "_IMAGE=${IMAGE},_TAG=${IMAGE_TAG}" \
  --service-account "${BUILD_SA}"

echo "built ${IMAGE}:${IMAGE_TAG}"
