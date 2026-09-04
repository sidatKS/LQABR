#!/usr/bin/env bash
# Promote the upstream MCP image into Artifact Registry. Idempotent.
# Cloud Run CANNOT pull from Docker Hub — this is what makes the image deployable.
set -euo pipefail
source "$(dirname "$0")/config.sh"

# Cloud Run is x86-64 only. An arm64-only image runs fine locally and then fails on
# Cloud Run with "container failed to start and listen on port" — never mentioning arch.
echo ">> verifying upstream is linux/amd64"
docker manifest inspect "${MCP_UPSTREAM}" | grep -E '"architecture"|"os"'
echo ">> (an extra unknown/unknown entry is the buildx attestation manifest — ignore it)"

# In WSL the gcloud credential helper often is not on Docker Desktop's PATH; this
# access-token login bypasses it. Failure mode otherwise looks like an IAM error.
gcloud auth print-access-token \
  | docker login -u oauth2accesstoken --password-stdin "https://${REGION}-docker.pkg.dev"

docker pull "${MCP_UPSTREAM}"
docker tag  "${MCP_UPSTREAM}" "${MCP_AR}:${MCP_TAG}"
docker tag  "${MCP_UPSTREAM}" "${MCP_AR}:latest"
# A dated tag as well: 0.1.0 is NOT immutable in practice — it was silently redefined
# on 2026-08-28 when upstream :latest moved, which destroyed the only durable pin.
# This tag is never reused, so every promotion stays addressable afterwards.
docker tag  "${MCP_UPSTREAM}" "${MCP_AR}:${MCP_DATED_TAG}"
docker push "${MCP_AR}:${MCP_TAG}"
docker push "${MCP_AR}:latest"
docker push "${MCP_AR}:${MCP_DATED_TAG}"

echo ">> promoted. Digest now in AR:"
gcloud container images describe "${MCP_AR}:${MCP_TAG}" \
  --project="${PROJECT_ID}" --format='value(image_summary.digest)'
echo ">> expected (recorded 2026-08-26): ${MCP_DIGEST}"
echo ">> if these differ, upstream :latest moved — update MCP_DIGEST in config.sh deliberately."
