#!/usr/bin/env bash
# Deploy the research agent to Cloud Run. Scales to zero; nothing stays warm.
#
# Spec: docs/research_deploy_spec.md v1.1 — §4 is the flag set, §4.1 the
# rationale, §9 the verification this script prints.
#
#   bash agents/research/infra/03_deploy_run.sh
#
# Idempotent: `gcloud run deploy` keys on the service name, so re-running
# redeploys the same service and never creates a second one (S6).
set -euo pipefail
source "$(dirname "$0")/config.sh"

# Captured BEFORE the deploy so the verification filter in §9/V3 cannot miss
# events, and cannot match the previous revision's either.
DEPLOY_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

gcloud run deploy "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --image "${IMAGE}:${IMAGE_TAG}" \
  --service-account "${RUNTIME_SA}" \
  --execution-environment gen2 \
  --network "${VPC_NETWORK}" \
  --subnet "${VPC_SUBNET}" \
  --vpc-egress "${VPC_EGRESS}" \
  --ingress "${INGRESS}" \
  --no-allow-unauthenticated \
  --port "${PORT}" \
  --cpu "${CPU}" \
  --memory "${MEMORY}" \
  --timeout "${TIMEOUT}" \
  --min-instances "${MIN_INSTANCES}" \
  --max-instances "${MAX_INSTANCES}" \
  --add-volume "name=${LOG_VOLUME_NAME},type=in-memory" \
  --add-volume-mount "volume=${LOG_VOLUME_NAME},mount-path=${LOG_MOUNT_PATH}" \
  --set-secrets "LQABR_ANTHROPIC_API_KEY=${MODEL_SECRET}:latest,LQABR_HUBSPOT_ACCESS_TOKEN=${HUBSPOT_SECRET}:latest" \
  --set-env-vars "LQABR_RESEARCH_SECRETS_SOURCE=${LQABR_RESEARCH_SECRETS_SOURCE}" \
  --set-env-vars "LQABR_RESEARCH_MCP_BASE_URL=${LQABR_RESEARCH_MCP_BASE_URL}" \
  --set-env-vars "LQABR_RESEARCH_LOG_FORMAT=${LQABR_RESEARCH_LOG_FORMAT}" \
  --set-env-vars "LQABR_RESEARCH_DRY_RUN=${LQABR_RESEARCH_DRY_RUN}"

# why: --service-account      never inherit the default compute SA, which holds
#                             zero roles in this project (F5).
# why: --network/--subnet/--vpc-egress
#                             WITHOUT THESE the service is not on the VPC and
#                             cannot reach the ingress=internal MCP at all (F4).
#                             all-traffic is verified: a VPC caller does reach an
#                             internal callee (runbook P8a stage 3).
# why: --ingress internal     otherwise the agent is internet-reachable (F3).
# why: --no-allow-unauthenticated
#                             IAM enforced on top of ingress (F3).
# why: --execution-environment gen2
#                             matches the rest of the fleet.
# why: --timeout 600          one run is a page fetch PLUS an Anthropic call with
#                             web search; the 300s default is too tight.
# why: --memory 1Gi           the log volume is tmpfs and counts against this.
# why: --add-volume /app/logs config.yaml points logging.file at a tree the image
#                             does not contain; without it file logging errors.
# why: --set-secrets          values injected under the names secrets.py:_env_name
#                             derives; the runtime SA gates access and no token
#                             enters the image.
# why: SECRETS_SOURCE=env     secret_manager raises ImportError here -- research
#                             ships no google-cloud-secret-manager (F1).

URL="$(gcloud run services describe "${SERVICE_NAME}" --project "${PROJECT_ID}" \
        --region "${REGION}" --format='value(status.url)')"

cat <<INFO

deployed: ${SERVICE_NAME}
     url: ${URL}
   image: ${IMAGE}:${IMAGE_TAG}
      sa: ${RUNTIME_SA}
 ingress: ${INGRESS}    egress: ${VPC_EGRESS} via ${VPC_NETWORK}/${VPC_SUBNET}
 dry-run: ${LQABR_RESEARCH_DRY_RUN}

-- VERIFY -------------------------------------------------------------------

1) Did it start, and did the MCP handshake succeed?

   gcloud run services logs read ${SERVICE_NAME} \\
     --project ${PROJECT_ID} --region ${REGION} --limit 30

2) The events that matter. Filter on timestamp>, NEVER --freshness: a stale
   entry inside a freshness window reads exactly like a current result.

   gcloud logging read 'resource.type="cloud_run_revision"
     AND resource.labels.service_name="${SERVICE_NAME}"
     AND timestamp>"${DEPLOY_TS}"' \\
     --project=${PROJECT_ID} --limit=25 \\
     --format='value(timestamp,jsonPayload.event,jsonPayload.reason)'

   Expect, in order:
       service_start
       mcp_initialized
       mcp_tools_discovered
       mcp_startup_check_ok      <- anything else means the tool names moved

-- REACHING IT --------------------------------------------------------------

   ingress=${INGRESS}. A curl from your laptop returns 404 EVEN WITH a valid
   identity token, because Cloud Run hides an internal service rather than
   admitting it exists. That is the control working, not a fault.

   For a real request, invoke from inside the VPC with a Cloud Run job --
   see docs/CloudRun_RunBook.md §0.2 for the pattern. POST to:
       ${URL}/research/campaign/a2a

   To reach it from a laptop temporarily, flip ingress AND FLIP IT BACK:
       gcloud run services update ${SERVICE_NAME} \\
         --project ${PROJECT_ID} --region ${REGION} --ingress all
       gcloud run services update ${SERVICE_NAME} \\
         --project ${PROJECT_ID} --region ${REGION} --ingress internal

INFO
