#!/usr/bin/env bash
# scheduler.sh — optional Cloud Scheduler heartbeat (PLANNED). Idempotent.
# In SP-2 HubSpot workflows carry orchestration, so this is off by default.
# Set LQABR_ENABLE_SCHEDULER=1 to create a periodic OIDC call to the Gateway.
# Self-contained: ./config.sh.
set -euo pipefail
: "${PROJECT_ID:?source ./config.sh first}"

if [[ "${LQABR_ENABLE_SCHEDULER:-0}" != "1" ]]; then
  echo "scheduler: PLANNED — HubSpot orchestrates this sprint. Set LQABR_ENABLE_SCHEDULER=1 to create the heartbeat."
  exit 0
fi

GTWY="$(for e in "${LQABR_SERVICES[@]}"; do IFS='|' read -r n d k x <<<"$e"; [[ "$d" == "gateway" ]] && echo "$n"; done)"
GTWY_URL="$(gcloud run services describe "${GTWY}" --region "${REGION}" --project "${PROJECT_ID}" --format 'value(status.url)' 2>/dev/null)" || true
if [[ -z "${GTWY_URL}" ]]; then
  echo "scheduler: Gateway not deployed yet (run deploy.sh first) — nothing to schedule." >&2
  exit 0
fi

gcloud scheduler jobs describe lqabr-dispatch-cycle --location "${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1 && \
gcloud scheduler jobs delete lqabr-dispatch-cycle --location "${REGION}" --project "${PROJECT_ID}" --quiet
gcloud scheduler jobs create http lqabr-dispatch-cycle \
  --location "${REGION}" --project "${PROJECT_ID}" \
  --schedule "*/30 * * * *" \
  --uri "${GTWY_URL}/hooks/dispatch" \
  --http-method POST \
  --oidc-service-account-email "${AGENT_SA}" \
  --oidc-token-audience "${GTWY_URL}" \
  --message-body '{"source":"scheduler","action":"dispatch"}' \
  --headers "Content-Type=application/json"

echo "scheduler: heartbeat ready (dispatch every 30 min → Gateway) in ${PROJECT_ID}."
