#!/usr/bin/env bash
# Deploy lqabr-dev-mcp. Idempotent — re-running produces a new revision.
# Closes the runbook's open item: "MCP has no deploy script of its own."
set -euo pipefail
source "$(dirname "$0")/config.sh"

gcloud run deploy "${MCP_SERVICE}" \
  --image="${MCP_AR}:${MCP_TAG}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --service-account="${MCP_SA}" \
  --execution-environment=gen2 \
  --network="${VPC_NETWORK}" --subnet="${VPC_SUBNET}" --vpc-egress="${VPC_EGRESS}" \
  --ingress=internal --no-allow-unauthenticated \
  --set-env-vars="LQABR_SECRET_PROJECT=${PROJECT_ID},LQABR_SECRET_HUBSPOT_PRIVATE_APP_TOKEN=${MCP_SECRET_REF}" \
  --add-volume=name=errors,type=in-memory \
  --add-volume-mount=volume=errors,mount-path=/app/errors \
  --port=8080 --max-instances=3

# Why the volume: the image runs as user `mcp` with WorkingDir=/app, which is root-owned.
# It writes an `errors` path at TOOL-CALL time, so without this the service starts clean
# and then dies with [Errno 13] Permission denied: 'errors' on the first real write.

# KNOWN DRIFT on the live service (2026-08-29): revision 00005-ddf ALSO carries a
# HUBSPOT_PRIVATE_APP_TOKEN secretKeyRef and an unused `errdir` volume, both left over
# from the manual 2026-08-26 deploys — `gcloud run deploy` preserves existing secrets
# and volumes unless cleared. Harmless (the image accepts either token path), but it
# means this script alone does NOT reproduce the live shape in a clean project. Add
# --clear-secrets here if you want the two to converge.

echo ">> deployed:"
gcloud run services describe "${MCP_SERVICE}" --region="${REGION}" --project="${PROJECT_ID}" \
  --format='value(status.url, metadata.annotations."run.googleapis.com/ingress")'
