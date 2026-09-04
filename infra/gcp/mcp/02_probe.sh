#!/usr/bin/env bash
# Prove an in-VPC caller can complete the FULL MCP handshake against lqabr-dev-mcp.
#
# Reuses the MCP image with its entrypoint overridden, so no extra image is needed, and
# runs with the same SA/subnet/egress a real agent uses. Unlike the P8a stage-3 one-liner
# (which called tools/list cold and got a 500), this performs initialize first and exits
# cleanly, so the job succeeds instead of hitting the task timeout.
set -euo pipefail
source "$(dirname "$0")/config.sh"
JOB="mcp-probe"

MCP_URL="$(gcloud run services describe "${MCP_SERVICE}" --region="${REGION}" \
            --project="${PROJECT_ID}" --format='value(status.url)')"
echo ">> probing ${MCP_URL}/mcp"

PROBE="$(cat "$(dirname "$0")/probe_client.py")"

gcloud run jobs delete "${JOB}" --region="${REGION}" --project="${PROJECT_ID}" --quiet 2>/dev/null || true
gcloud run jobs create "${JOB}" \
  --image="${MCP_AR}:${MCP_TAG}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --service-account="${MCP_SA}" \
  --network="${VPC_NETWORK}" --subnet="${VPC_SUBNET}" --vpc-egress="${VPC_EGRESS}" \
  --set-env-vars="MCP_URL=${MCP_URL}" \
  --command=python --args="^@^-c@${PROBE}" \
  --max-retries=0 --task-timeout=300s

# --wait blocks until the execution finishes. Do NOT delete the job early: a first
# attempt deleted 27s in produced no output at all (runbook, P9).
gcloud run jobs execute "${JOB}" --region="${REGION}" --project="${PROJECT_ID}" --wait

echo ">> output:"
# NOT `gcloud run jobs executions logs read` — that is alpha-only and fails on a
# stock gcloud with "Invalid choice: 'logs'". Cloud Logging is GA and always present.
EXEC="$(gcloud run jobs executions list --job="${JOB}" --region="${REGION}" \
        --project="${PROJECT_ID}" --limit=1 --format='value(name)')"
echo ">> execution: ${EXEC}"
# Filter on the execution name, not --freshness: stale entries inside a freshness
# window have twice been mistaken for the current result (RunBook, P9).
gcloud logging read \
  "resource.type=\"cloud_run_job\" AND labels.\"run.googleapis.com/execution_name\"=\"${EXEC}\"" \
  --project="${PROJECT_ID}" --limit=200 --order=asc --format='value(textPayload)' 

echo ">> clean up when done:  gcloud run jobs delete ${JOB} --region=${REGION} --project=${PROJECT_ID} --quiet"
