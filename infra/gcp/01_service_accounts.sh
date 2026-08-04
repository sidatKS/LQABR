#!/usr/bin/env bash
# 01 — create the agent runtime service account and grant least-privilege
# roles. Idempotent.
set -euo pipefail
: "${PROJECT_ID:?source ./config.sh first}"

gcloud iam service-accounts describe "${AGENT_SA}" --project "${PROJECT_ID}" >/dev/null 2>&1 || \
gcloud iam service-accounts create "${AGENT_SA_NAME}" \
  --display-name "LQABR agent runtime" --project "${PROJECT_ID}"

# Least-privilege runtime roles:
#   secretAccessor  — read service credentials from Secret Manager
#   pubsub pub/sub  — ingestion trigger + engagement event fan-out
#   run.invoker     — orchestrator invokes stage agents (A2A) service-to-service
#   aiplatform.user — Gemini via Vertex AI (when GOOGLE_GENAI_USE_ENTERPRISE=1)
#   logging/monitoring — standard observability writers
for role in \
  roles/secretmanager.secretAccessor \
  roles/pubsub.publisher \
  roles/pubsub.subscriber \
  roles/run.invoker \
  roles/aiplatform.user \
  roles/logging.logWriter \
  roles/monitoring.metricWriter; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member "serviceAccount:${AGENT_SA}" --role "${role}" \
    --condition=None --quiet >/dev/null
done

echo "01: service account ${AGENT_SA} ready with runtime roles."
