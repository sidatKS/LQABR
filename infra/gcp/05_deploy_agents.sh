#!/usr/bin/env bash
# 05 — build and deploy every LQABR service to Cloud Run (Google's managed
# serverless sandbox). Idempotent: re-deploys create new revisions.
#
# Run from infra/gcp/ (the script cd's to the repo root for the builds):
#   source ./config.sh && bash 05_deploy_agents.sh
#
# Services deployed:
#   lqabr-<agent>-agent    ADK api_server endpoints (A2A) — internal only,
#                          invoked by the orchestrator service account
#   lqabr-<agent>-webhook  Mailgun/Twilio/Zoom webhook receivers — public
#                          (verified by provider signatures)
set -euo pipefail
: "${PROJECT_ID:?source ./config.sh first}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

SECRET_FLAGS="LQABR_HUBSPOT_ACCESS_TOKEN=lqabr-hubspot-access-token:latest"
SECRET_FLAGS+=",LQABR_MAILGUN_API_KEY=lqabr-mailgun-api-key:latest"
SECRET_FLAGS+=",LQABR_MAILGUN_WEBHOOK_SIGNING_KEY=lqabr-mailgun-webhook-signing-key:latest"
SECRET_FLAGS+=",LQABR_TWILIO_ACCOUNT_SID=lqabr-twilio-account-sid:latest"
SECRET_FLAGS+=",LQABR_TWILIO_AUTH_TOKEN=lqabr-twilio-auth-token:latest"
SECRET_FLAGS+=",LQABR_ZOOMINFO_USERNAME=lqabr-zoominfo-username:latest"
SECRET_FLAGS+=",LQABR_ZOOMINFO_PASSWORD=lqabr-zoominfo-password:latest"
SECRET_FLAGS+=",LQABR_ZOOM_ACCOUNT_ID=lqabr-zoom-account-id:latest"
SECRET_FLAGS+=",LQABR_ZOOM_CLIENT_ID=lqabr-zoom-client-id:latest"
SECRET_FLAGS+=",LQABR_ZOOM_CLIENT_SECRET=lqabr-zoom-client-secret:latest"
SECRET_FLAGS+=",LQABR_ZOOM_WEBHOOK_SECRET_TOKEN=lqabr-zoom-webhook-secret-token:latest"

ENV_FLAGS="GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
ENV_FLAGS+=",GOOGLE_GENAI_USE_ENTERPRISE=1"
ENV_FLAGS+=",GOOGLE_CLOUD_LOCATION=${REGION}"
ENV_FLAGS+=",MAILGUN_DOMAIN=${MAILGUN_DOMAIN}"
ENV_FLAGS+=",MAILGUN_FROM=${MAILGUN_FROM}"
ENV_FLAGS+=",TWILIO_FROM_NUMBER=${TWILIO_FROM_NUMBER}"
ENV_FLAGS+=",LQABR_SENDER_NAME=${LQABR_SENDER_NAME}"
ENV_FLAGS+=",LQABR_CTA_URL=${LQABR_CTA_URL}"
[[ -n "${ZOOM_BOOKING_URL}" ]] && ENV_FLAGS+=",ZOOM_BOOKING_URL=${ZOOM_BOOKING_URL}"

build_and_deploy() {  # <agent_dir> <kind> <extra deploy flags...>
  local agent="$1" kind="$2"; shift 2
  local service="lqabr-${agent//_/-}-${kind}"
  local image="${IMAGE_BASE}/${service}:latest"

  echo "== building ${service}"
  gcloud builds submit . \
    --config infra/gcp/cloud-run/cloudbuild.yaml \
    --substitutions "_AGENT=${agent},_KIND=${kind},_IMAGE=${image}" \
    --project "${PROJECT_ID}" --quiet

  echo "== deploying ${service}"
  gcloud run deploy "${service}" \
    --image "${image}" \
    --region "${REGION}" \
    --service-account "${AGENT_SA}" \
    --set-secrets "${SECRET_FLAGS}" \
    --set-env-vars "${ENV_FLAGS}" \
    --memory 512Mi --cpu 1 --max-instances 5 \
    --project "${PROJECT_ID}" --quiet "$@"
}

# 1) ADK agents — internal; only the runtime SA (orchestrator/A2A) may invoke.
for agent in "${ADK_AGENTS[@]}"; do
  build_and_deploy "${agent}" agent --no-allow-unauthenticated
done

# 2) Webhook receivers — public endpoints; authenticity is enforced by
#    Mailgun/Twilio/Zoom signature verification inside the app.
for agent in "${WEBHOOK_AGENTS[@]}"; do
  build_and_deploy "${agent}" webhook --allow-unauthenticated
done

# 3) Wire the orchestrator to the deployed agent URLs (A2A endpoints).
url_of() { gcloud run services describe "$1" --region "${REGION}" --project "${PROJECT_ID}" --format 'value(status.url)'; }
gcloud run services update lqabr-orchestrator-agent \
  --region "${REGION}" --project "${PROJECT_ID}" --quiet \
  --update-env-vars "LQABR_LEAD_PROFILE_AGENT_URL=$(url_of lqabr-lead-profile-agent),LQABR_EMAIL_AGENT_URL=$(url_of lqabr-email-agent),LQABR_TEXT_VOICE_AGENT_URL=$(url_of lqabr-text-voice-agent),LQABR_SCHEDULING_AGENT_URL=$(url_of lqabr-scheduling-agent)"

# 4) Tell the text_voice services their own public webhook base URL (TwiML callbacks).
TV_WEBHOOK_URL="$(url_of lqabr-text-voice-webhook)"
gcloud run services update lqabr-text-voice-webhook \
  --region "${REGION}" --project "${PROJECT_ID}" --quiet \
  --update-env-vars "LQABR_WEBHOOK_BASE_URL=${TV_WEBHOOK_URL}"
gcloud run services update lqabr-text-voice-agent \
  --region "${REGION}" --project "${PROJECT_ID}" --quiet \
  --update-env-vars "LQABR_WEBHOOK_BASE_URL=${TV_WEBHOOK_URL}"

echo "05: all services deployed."
echo "    Configure provider webhooks to:"
echo "      Mailgun (delivered/opened/clicked): $(url_of lqabr-email-webhook)/webhooks/mailgun"
echo "      Zoom (scheduler events):            $(url_of lqabr-scheduling-webhook)/webhooks/zoom"
echo "      Twilio callbacks are set per-call by the agent automatically."
