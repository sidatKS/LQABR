#!/usr/bin/env bash
# 01 — identity & access. Creates the runtime service account with least-
# privilege runtime roles, and grants the developer group its deploy-and-
# operate roles + actAs on the runtime SA. Idempotent. OWNER runs this.
# Self-contained: reads this environment's values from ./config.sh.
set -euo pipefail
: "${PROJECT_ID:?source ./config.sh first}"

# ── Runtime service account ──────────────────────────────────
gcloud iam service-accounts describe "${AGENT_SA}" --project "${PROJECT_ID}" >/dev/null 2>&1 || \
gcloud iam service-accounts create "${AGENT_SA_NAME}" \
  --display-name "LQABR agent runtime" --project "${PROJECT_ID}"

# Least-privilege RUNTIME roles (what the services run AS):
#   secretAccessor  — read service credentials from Secret Manager
#   pubsub pub/sub  — engagement event fan-out (planned spine)
#   run.invoker     — Gateway→agent and agent→MCP OIDC calls (service-to-service)
#   aiplatform.user — Vertex AI (when a Google model is used)
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

# ── Developer group — deploy-and-operate roles (roles bind to the GROUP) ──
# The group builds/redeploys and reads secrets/logs; it cannot provision infra
# or edit IAM (that stays with the owner). Skipped if DEV_GROUP is empty.
if [[ -n "${DEV_GROUP:-}" ]]; then
  for role in \
    roles/run.developer \
    roles/cloudbuild.builds.editor \
    roles/artifactregistry.writer \
    roles/secretmanager.viewer \
    roles/secretmanager.secretAccessor \
    roles/logging.viewer \
    roles/monitoring.viewer \
    roles/serviceusage.serviceUsageConsumer; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member "group:${DEV_GROUP}" --role "${role}" \
      --condition=None --quiet >/dev/null
  done
  # actAs on the runtime SA — the ONLY deployer→runtime link (deploy as the SA).
  gcloud iam service-accounts add-iam-policy-binding "${AGENT_SA}" \
    --member "group:${DEV_GROUP}" --role "roles/iam.serviceAccountUser" \
    --project "${PROJECT_ID}" --condition=None --quiet >/dev/null
  echo "01: runtime SA ${AGENT_SA} + developer group ${DEV_GROUP} ready."
else
  echo "01: runtime SA ${AGENT_SA} ready (DEV_GROUP unset — skipped group grants)."
fi
