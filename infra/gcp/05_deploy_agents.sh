#!/usr/bin/env bash
# 05 — build and deploy every LQABR service to Cloud Run (Google's managed
# serverless sandbox). Idempotent: re-deploys create new revisions.
#
# Run from infra/gcp/ (the script cd's to the repo root for the builds):
#   source ./config.sh && bash 05_deploy_agents.sh
#
# Services deployed:
#   SERVICE_AGENTS   the agent's own domain surface (uvicorn service_app:app)
#                    — POST /hubspot/campaign, /health AND /healthz. This is
#                    what the gateway dispatches to. Internal only.
#   ADK_AGENTS       lqabr-<agent>-agent — generic ADK api_server (A2A),
#                    internal only, invoked by the orchestrator SA.
#   WEBHOOK_AGENTS   lqabr-<agent>-webhook — Mailgun/Twilio/Zoom receivers,
#                    public (authenticity is the provider signature).
#
# A service kind's name can be pinned via SERVICE_NAME_OVERRIDES so a kind
# change does not orphan a URL something else already holds — that is how
# email keeps serving on lqabr-email-agent after moving off the ADK runner.
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
# auto = env-first, and --set-secrets below injects Secret Manager values AS
# env vars, so credentials come from Secret Manager without an API call on
# every cold start. Set LQABR_SECRETS_SOURCE=secret_manager to force the
# direct API instead (rotation without redeploy, at ~one call per secret).
ENV_FLAGS+=",LQABR_SECRETS_SOURCE=${LQABR_SECRETS_SOURCE:-secret_manager}"
ENV_FLAGS+=",GOOGLE_GENAI_USE_ENTERPRISE=1"
ENV_FLAGS+=",GOOGLE_CLOUD_LOCATION=${REGION}"
ENV_FLAGS+=",MAILGUN_DOMAIN=${MAILGUN_DOMAIN}"
ENV_FLAGS+=",MAILGUN_FROM=${MAILGUN_FROM}"
ENV_FLAGS+=",TWILIO_FROM_NUMBER=${TWILIO_FROM_NUMBER}"
ENV_FLAGS+=",LQABR_SENDER_NAME=${LQABR_SENDER_NAME}"
ENV_FLAGS+=",LQABR_CTA_URL=${LQABR_CTA_URL}"
[[ -n "${ZOOM_BOOKING_URL}" ]] && ENV_FLAGS+=",ZOOM_BOOKING_URL=${ZOOM_BOOKING_URL}"
ENV_FLAGS+=",LQABR_EMAIL_MODEL=${LQABR_EMAIL_MODEL:-gemini-2.0-flash}"
ENV_FLAGS+=",LQABR_EMAIL_TEMPERATURE=${LQABR_EMAIL_TEMPERATURE:-1.0}"
ENV_FLAGS+=",LQABR_EMAIL_ROUTES=${LQABR_EMAIL_ROUTES:-all}"
# HubSpot property names are owned by the HubSpot schema, so they are config,
# never defaults to be discovered at runtime. Passed explicitly: the code
# default for the selection property is a guess, and a wrong name now FAILS
# the run rather than quietly working a different set of leads.
ENV_FLAGS+=",LQABR_HUBSPOT_OBJECT_ID_PROPERTY=${LQABR_HUBSPOT_OBJECT_ID_PROPERTY:-object_id}"
ENV_FLAGS+=",LQABR_HUBSPOT_CAMPAIGN_COMPLETE_PROPERTY=${LQABR_HUBSPOT_CAMPAIGN_COMPLETE_PROPERTY:-email_campaign_complete}"
# Explicit rather than implied: the image provisions this path and chowns
# it to `nobody`. Without a writable state dir every campaign 503s at step 3.
ENV_FLAGS+=",LQABR_EMAIL_RUNSTATE_DIR=${LQABR_EMAIL_RUNSTATE_DIR:-/var/lib/lqabr/email/runstate}"

# Cloud Run service name for an <agent>:<kind>, honouring SERVICE_NAME_OVERRIDES.
service_name() {  # <agent_dir> <kind>
  local key="$1:$2" entry
  for entry in "${SERVICE_NAME_OVERRIDES[@]:-}"; do
    [[ "${entry%%=*}" == "${key}" ]] && { echo "${entry#*=}"; return; }
  done
  echo "lqabr-${1//_/-}-${2}"
}

build_and_deploy() {  # <agent_dir> <kind> <extra deploy flags...>
  local agent="$1" kind="$2"; shift 2
  local service; service="$(service_name "${agent}" "${kind}")"
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

# 1) Domain surfaces — ONE service per agent, serving both the gateway entry
#    and the provider push (Platform Engineering, 2026-08-04: "your mail gun
#    has to send back the event triggers back to the SAME agent").
#
#    Deployed --allow-unauthenticated because Mailgun cannot present a Google
#    ID token. /mailgun/events proves itself via the Mailgun HMAC.
#    /hubspot/campaign and /engagement/sync are currently UNAUTHENTICATED —
#    the LQABR_EMAIL_GATEWAY_TOKEN check was removed 2026-08-05 (the gateway
#    wasn't sending the header); nothing replaces it yet.
for agent in "${SERVICE_AGENTS[@]:-}"; do
  [[ -n "${agent}" ]] || continue
  build_and_deploy "${agent}" service --allow-unauthenticated
done

# 2) ADK agents — internal; only the runtime SA (orchestrator/A2A) may invoke.
for agent in "${ADK_AGENTS[@]}"; do
  build_and_deploy "${agent}" agent --no-allow-unauthenticated
done

# 3) Webhook receivers — public endpoints; authenticity is enforced by
#    Mailgun/Twilio/Zoom signature verification inside the app.
for agent in "${WEBHOOK_AGENTS[@]}"; do
  build_and_deploy "${agent}" webhook --allow-unauthenticated
done

# 4) Wire the orchestrator to the deployed agent URLs.
#    NOTE: LQABR_EMAIL_AGENT_URL now points at a DOMAIN surface
#    (POST /hubspot/campaign), not an ADK runner. Anything dispatching to it
#    must send the domain shape — the same shape text_voice takes — not an
#    ADK {appName, userId, sessionId, newMessage} envelope. `service_name`
#    keeps the URL itself unchanged.
url_of() { gcloud run services describe "$1" --region "${REGION}" --project "${PROJECT_ID}" --format 'value(status.url)'; }
gcloud run services update lqabr-orchestrator-agent \
  --region "${REGION}" --project "${PROJECT_ID}" --quiet \
  --update-env-vars "LQABR_LEAD_PROFILE_AGENT_URL=$(url_of lqabr-lead-profile-agent),LQABR_EMAIL_AGENT_URL=$(url_of lqabr-email-agent),LQABR_TEXT_VOICE_AGENT_URL=$(url_of lqabr-text-voice-agent),LQABR_SCHEDULING_AGENT_URL=$(url_of lqabr-scheduling-agent)"

# 5) Tell the text_voice services their own public webhook base URL (TwiML callbacks).
TV_WEBHOOK_URL="$(url_of lqabr-text-voice-webhook)"
gcloud run services update lqabr-text-voice-webhook \
  --region "${REGION}" --project "${PROJECT_ID}" --quiet \
  --update-env-vars "LQABR_WEBHOOK_BASE_URL=${TV_WEBHOOK_URL}"
gcloud run services update lqabr-text-voice-agent \
  --region "${REGION}" --project "${PROJECT_ID}" --quiet \
  --update-env-vars "LQABR_WEBHOOK_BASE_URL=${TV_WEBHOOK_URL}"

echo "05: all services deployed."
echo
echo "    Email agent contract (what the gateway must send):"
echo "      POST $(url_of "$(service_name email service)")/hubspot/campaign"
echo "           {\"object_id\": \"<trigger id>\"}      # trigger_id also accepted"
echo "      GET  $(url_of "$(service_name email service)")/health   (also /healthz)"
echo "      POST $(url_of "$(service_name email service)")/engagement/sync"
echo "           {\"object_id\": \"...\", \"run_id\": \"...\"}   # Mailgun track-ID tool call"
echo "    Configure provider webhooks to:"
echo "      Mailgun (delivered/opened/clicked): $(url_of "$(service_name email service)")/mailgun/events"
echo "        ^ the SAME service the gateway calls. There is no email webhook service."
echo "      Zoom (scheduler events):            $(url_of lqabr-scheduling-webhook)/webhooks/zoom"
echo "      Twilio callbacks are set per-call by the agent automatically."
