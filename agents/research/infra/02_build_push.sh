#!/usr/bin/env bash
# Build + push the research image. Mirrors agents/summary/infra/02_build_push.sh.
set -euo pipefail
source "$(dirname "$0")/config.sh"
AGENT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

gcloud builds submit "${AGENT_DIR}" \
  --project "${PROJECT_ID}" \
  --config "${AGENT_DIR}/infra/cloudbuild.yaml" \
  --substitutions "_IMAGE=${IMAGE},_TAG=${IMAGE_TAG}" \
  --service-account "${BUILD_SA}"

echo "built ${IMAGE}:${IMAGE_TAG}"
