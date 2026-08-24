#!/usr/bin/env bash
# Build the agent's own image from ITS OWN folder as the build context.
set -euo pipefail
source "$(dirname "$0")/config.sh"
AGENT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

gcloud builds submit "${AGENT_DIR}" \
  --project "${PROJECT_ID}" \
  --config "${AGENT_DIR}/infra/cloudbuild.yaml" \
  --substitutions "_IMAGE=${IMAGE},_TAG=${IMAGE_TAG}"

echo "built ${IMAGE}:${IMAGE_TAG}"
