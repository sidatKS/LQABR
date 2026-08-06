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

# Rev 5 text_voice runs on Vapi (Twilio/Mailgun/Zoom creds are dead weight
# there, and mounting the known-stale Twilio secrets would only mislead).
# Policy 2026-07-31: Vapi + Anthropic keys are ENV-ONLY in code — Cloud Run
# mounts them from Secret Manager as env vars, so ops keeps SM and the code
# never talks to it.
TV_SECRET_FLAGS="LQABR_HUBSPOT_ACCESS_TOKEN=lqabr-hubspot-access-token:latest"
TV_SECRET_FLAGS+=",LQABR_VAPI_API_KEY=lqabr-vapi-api-key:latest"
TV_SECRET_FLAGS+=",ANTHROPIC_API_KEY=lqabr-anthropic-api-key:latest"

TV_ENV_FLAGS="GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
TV_ENV_FLAGS+=",LQABR_SENDER_NAME=${LQABR_SENDER_NAME}"
TV_ENV_FLAGS+=",LQABR_VAPI_PHONE_NUMBER_ID=${LQABR_VAPI_PHONE_NUMBER_ID}"
[[ -n "${LQABR_VAPI_ASSISTANT_ID:-}" ]] && TV_ENV_FLAGS+=",LQABR_VAPI_ASSISTANT_ID=${LQABR_VAPI_ASSISTANT_ID}"
[[ -n "${LQABR_TEXT_VOICE_MODEL:-}" ]] && TV_ENV_FLAGS+=",LQABR_TEXT_VOICE_MODEL=${LQABR_TEXT_VOICE_MODEL}"

build_and_deploy() {  # <agent_dir> <kind> <secrets> <envs> <extra deploy flags...>
  local agent="$1" kind="$2" secrets="$3" envs="$4"; shift 4
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
    --set-secrets "${secrets}" \
    --set-env-vars "${envs}" \
    --memory 512Mi --cpu 1 --max-instances 5 \
    --project "${PROJECT_ID}" --quiet "$@"
}

# Optional scoped deploy (unset -> deploy everything, the original behavior):
#   ONLY=text_voice                deploy just that agent's services
#   KIND=webhook | KIND=agent      deploy just that service kind
#   ONLY=text_voice KIND=webhook   e.g. text_voice production service alone —
#                                  its prod path is the FastAPI webhook behind
#                                  the Agent Gateway; its adk api_server is a
#                                  dev/test surface, not in the prod flow.
# skip() is the single gate both loops consult.
skip() { [[ -n "${ONLY:-}" && "${1}" != "${ONLY}" ]] || [[ -n "${KIND:-}" && "${2}" != "${KIND}" ]]; }

# 1) ADK agents — internal; only the runtime SA (orchestrator/A2A) may invoke.
for agent in "${ADK_AGENTS[@]}"; do
  skip "${agent}" agent && continue
  if [[ "${agent}" == "text_voice" ]]; then
    build_and_deploy "${agent}" agent "${TV_SECRET_FLAGS}" "${TV_ENV_FLAGS}" --no-allow-unauthenticated
  else
    build_and_deploy "${agent}" agent "${SECRET_FLAGS}" "${ENV_FLAGS}" --no-allow-unauthenticated
  fi
done

# 2) Webhook receivers — public endpoints; authenticity is enforced by
#    provider signature verification inside the app (text_voice Rev 5 has no
#    auth on /lead by design — the Agent Gateway in front of it owns auth).
for agent in "${WEBHOOK_AGENTS[@]}"; do
  skip "${agent}" webhook && continue
  if [[ "${agent}" == "text_voice" ]]; then
    # --no-cpu-throttling is REQUIRED for text_voice: /lead returns 200
    # immediately and dials in a BackgroundTask; with Cloud Run's default
    # request-only CPU, that task is starved the moment the response ends
    # and the call may never be placed. (Pre-deploy audit 2026-08-03.)
    build_and_deploy "${agent}" webhook "${TV_SECRET_FLAGS}" \
      "${TV_ENV_FLAGS},APP_MODULE=tools:app" --allow-unauthenticated \
      --no-cpu-throttling
  else
    build_and_deploy "${agent}" webhook "${SECRET_FLAGS}" "${ENV_FLAGS}" --allow-unauthenticated
  fi
done

# 3) Wire the orchestrator to the deployed agent URLs (A2A endpoints).
#    Skipped on a single-agent deploy unless that agent is the orchestrator —
#    the other services may not exist yet in this project.
url_of() { gcloud run services describe "$1" --region "${REGION}" --project "${PROJECT_ID}" --format 'value(status.url)'; }
[[ ( -z "${ONLY:-}" || "${ONLY}" == "orchestrator" ) && ( -z "${KIND:-}" || "${KIND}" == "agent" ) ]] && \
gcloud run services update lqabr-orchestrator-agent \
  --region "${REGION}" --project "${PROJECT_ID}" --quiet \
  --update-env-vars "LQABR_LEAD_PROFILE_AGENT_URL=$(url_of lqabr-lead-profile-agent),LQABR_EMAIL_AGENT_URL=$(url_of lqabr-email-agent),LQABR_TEXT_VOICE_AGENT_URL=$(url_of lqabr-text-voice-agent),LQABR_SCHEDULING_AGENT_URL=$(url_of lqabr-scheduling-agent)"

# 4) Tell the text_voice services where Vapi should push end-of-call reports
#    (Rev 5: assistantOverrides.server.url -> POST /call-report on the webhook
#    service; swap for the Agent Gateway URL once that fronts the flow).
if [[ ( -z "${ONLY:-}" || "${ONLY}" == "text_voice" ) && ( -z "${KIND:-}" || "${KIND}" == "webhook" ) ]]; then
TV_WEBHOOK_URL="$(url_of lqabr-text-voice-webhook)"
gcloud run services update lqabr-text-voice-webhook \
  --region "${REGION}" --project "${PROJECT_ID}" --quiet \
  --update-env-vars "LQABR_VAPI_REPORT_CALLBACK_URL=${TV_WEBHOOK_URL}/call-report"
[[ -z "${KIND:-}" ]] && \
gcloud run services update lqabr-text-voice-agent \
  --region "${REGION}" --project "${PROJECT_ID}" --quiet \
  --update-env-vars "LQABR_VAPI_REPORT_CALLBACK_URL=${TV_WEBHOOK_URL}/call-report"
fi

echo "05: deploy complete${ONLY:+ (ONLY=${ONLY})}."
echo "    Configure provider webhooks to:"
echo "      Mailgun (delivered/opened/clicked): $(url_of lqabr-email-webhook)/webhooks/mailgun"
echo "      Zoom (scheduler events):            $(url_of lqabr-scheduling-webhook)/webhooks/zoom"
echo "      Vapi report callback is set per-call by the agent automatically."
