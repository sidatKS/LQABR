#!/usr/bin/env bash
# Deploy to Cloud Run. Scales to zero; nothing here stays warm.
set -euo pipefail
source "$(dirname "$0")/config.sh"

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
  --min-instances 0 \
  --max-instances 3 \
  --cpu 1 --memory 1Gi --timeout 300 \
  --set-env-vars "LQABR_SUMMARY_MCP_BASE_URL=${LQABR_SUMMARY_MCP_BASE_URL}" \
  --set-env-vars "LQABR_SUMMARY_MODEL=${LQABR_SUMMARY_MODEL}" \
  --set-env-vars "LQABR_SUMMARY_HUBSPOT_OBJECT_TYPE=${LQABR_SUMMARY_HUBSPOT_OBJECT_TYPE}" \
  --set-env-vars "LQABR_SUMMARY_HUBSPOT_SUMMARY_PROPERTY=${LQABR_SUMMARY_HUBSPOT_SUMMARY_PROPERTY}" \
  --set-env-vars "LQABR_SUMMARY_HUBSPOT_INDUSTRY_PROPERTY=${LQABR_SUMMARY_HUBSPOT_INDUSTRY_PROPERTY}" \
  --set-env-vars "LQABR_SUMMARY_ROUTES=${LQABR_SUMMARY_ROUTES}" \
  --set-env-vars "LQABR_SUMMARY_DRY_RUN=${LQABR_SUMMARY_DRY_RUN}" \
  --set-env-vars "LQABR_SUMMARY_MCP_STARTUP_CHECK=${LQABR_SUMMARY_MCP_STARTUP_CHECK}" \
  --set-env-vars "LQABR_SUMMARY_MCP_TOOL_READ=${LQABR_SUMMARY_MCP_TOOL_READ}" \
  --set-env-vars "LQABR_SUMMARY_MCP_TOOL_WRITE=${LQABR_SUMMARY_MCP_TOOL_WRITE}" \
  --set-env-vars "LQABR_SUMMARY_MCP_WRITE_STYLE=${LQABR_SUMMARY_MCP_WRITE_STYLE}" \
  --set-env-vars "LQABR_SUMMARY_SECRETS_SOURCE=secret_manager" \
  --set-env-vars "LQABR_SUMMARY_GCP_PROJECT=${PROJECT_ID}" \
  --add-volume "name=logs,type=in-memory" \
  --add-volume-mount "volume=logs,mount-path=/app/logs" \
  --no-allow-unauthenticated

URL="$(gcloud run services describe "${SERVICE_NAME}" --project "${PROJECT_ID}" \
        --region "${REGION}" --format='value(status.url)')"

cat <<INFO

deployed: ${URL}

  NOTE ingress=${INGRESS}. With "internal" this service is NOT reachable from a
  laptop -- a curl returns 404 even WITH a valid identity token, because Cloud
  Run hides the service rather than admitting it exists. That is the control
  working, not a fault. Verify from inside the VPC, or from the logs:
    gcloud run services logs read ${SERVICE_NAME} --project ${PROJECT_ID} --region ${REGION} --limit 30
  To reach it from a laptop temporarily, flip ingress (and flip it back):
    gcloud run services update ${SERVICE_NAME} --project ${PROJECT_ID} --region ${REGION} --ingress all

  Point the gateway at it (only if you route summaries through the gateway):
    export LQABR_SUMMARY_AGENT_URL=${URL}

  DRY RUN is ${LQABR_SUMMARY_DRY_RUN}  (0 = writes are LIVE, 1 = compute and log only).
  Live is the default, in config.sh AND in summary_core/settings.py. It is passed
  explicitly on every deploy, so it cannot silently revert the way it did on
  2026-08-26. To suppress writes for one deploy:
    LQABR_SUMMARY_DRY_RUN=1 bash infra/03_deploy_run.sh
INFO
