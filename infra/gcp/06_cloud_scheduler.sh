#!/usr/bin/env bash
# 06 — Cloud Scheduler jobs that keep the pipeline moving. Idempotent.
#
#   lqabr-dispatch-cycle : hits the orchestrator on a schedule so stage
#                          queues are worked continuously (A2A fan-out).
#   lqabr-zoominfo-pull  : publishes a daily ingestion trigger message
#                          (source=zoominfo, batch of 20) to Pub/Sub.
set -euo pipefail
: "${PROJECT_ID:?source ./config.sh first}"

ORCH_URL="$(gcloud run services describe lqabr-orchestrator-agent \
  --region "${REGION}" --project "${PROJECT_ID}" --format 'value(status.url)')"

# Every 30 minutes: one orchestration dispatch cycle.
gcloud scheduler jobs describe lqabr-dispatch-cycle --location "${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1 && \
gcloud scheduler jobs delete lqabr-dispatch-cycle --location "${REGION}" --project "${PROJECT_ID}" --quiet
gcloud scheduler jobs create http lqabr-dispatch-cycle \
  --location "${REGION}" --project "${PROJECT_ID}" \
  --schedule "*/30 * * * *" \
  --uri "${ORCH_URL}" \
  --http-method POST \
  --oidc-service-account-email "${AGENT_SA}" \
  --message-body '{"jsonrpc":"2.0","id":"scheduler","method":"message/send","params":{"message":{"role":"user","parts":[{"kind":"text","text":"Run one dispatch cycle."}],"messageId":"scheduler"}}}' \
  --headers "Content-Type=application/json"

# Daily 07:00: automatic ZoomInfo pull trigger (batch of 20).
gcloud scheduler jobs describe lqabr-zoominfo-pull --location "${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1 && \
gcloud scheduler jobs delete lqabr-zoominfo-pull --location "${REGION}" --project "${PROJECT_ID}" --quiet
gcloud scheduler jobs create pubsub lqabr-zoominfo-pull \
  --location "${REGION}" --project "${PROJECT_ID}" \
  --schedule "0 7 * * *" \
  --topic "${TOPIC_INGESTION}" \
  --message-body '{"source":"zoominfo","batch_size":20}'

echo "06: Cloud Scheduler jobs ready (dispatch every 30 min, ZoomInfo pull daily 07:00)."
