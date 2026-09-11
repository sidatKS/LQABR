#!/usr/bin/env bash
# deploy.sh — deploy the SP-2 LQABR services to Cloud Run from IMAGES ALREADY
# BUILT AND PUSHED BY CI to Artifact Registry. This step does NOT build images.
# Idempotent: re-runs create new revisions. Self-contained: ./config.sh.
#
#   source ./config.sh && bash deploy.sh
#
# Prereqs: infra setup done (../gcp or ../terraform) AND CI has pushed
# ${IMAGE_BASE}/<service>:${IMAGE_TAG} for every service below.
#
# SP-2 topology (all run AS the runtime SA, min-instances=0 / scale-to-zero):
#   gateway (public)  — the ONLY public door; verifies HubSpot/Mailgun/Vapi
#                       signatures, routes to agents over OIDC.
#   mcp     (internal)— shared HubSpot read/write path.
#   ldpf/email/voice  — internal agents, OIDC only.
set -euo pipefail
: "${PROJECT_ID:?source ./config.sh first}"
: "${IMAGE_TAG:?set IMAGE_TAG in ./config.sh (the CI-built tag, e.g. latest or a git sha)}"

COMMON_ENV="GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION}"

url_of() { gcloud run services describe "$1" --region "${REGION}" --project "${PROJECT_ID}" --format 'value(status.url)'; }

# Resolve a service NAME from its stable agent_dir in LQABR_SERVICES.
svc() { local d="$1" n dir k x; for e in "${LQABR_SERVICES[@]}"; do IFS='|' read -r n dir k x <<<"$e"; [[ "$dir" == "$d" ]] && { echo "$n"; return; }; done; echo "SERVICE_FOR_${d}_NOT_IN_CONFIG" >&2; return 1; }

deploy_svc() {  # <service> <exposure: public|internal> [extra gcloud flags...]
  local service="$1" exposure="$2"; shift 2
  local image="${IMAGE_BASE}/${service}:${IMAGE_TAG}"
  local auth=(--ingress internal --no-allow-unauthenticated)
  [[ "$exposure" == "public" ]] && auth=(--ingress all --allow-unauthenticated)
  echo "== deploying ${service} (${exposure}) ${image}"
  gcloud run deploy "${service}" \
    --image "${image}" --region "${REGION}" \
    --service-account "${AGENT_SA}" \
    --min-instances 0 --memory 512Mi --cpu 1 --max-instances 5 \
    "${auth[@]}" --project "${PROJECT_ID}" --quiet "$@"
}

MCP="$(svc hubspot_mcp)"; LDPF="$(svc lead_profile)"; EMAIL="$(svc email)"; VOICE="$(svc voice)"; GTWY="$(svc gateway)"

# 1) Shared HubSpot MCP — NOT deployed here. Use infra/gcp/mcp/01_deploy.sh.
#    The block that used to live here was wrong in two ways that only surface at
#    runtime: --set-secrets LQABR_HUBSPOT_ACCESS_TOKEN is not this image's token
#    contract (it resolves lazily via its own LQABR_SECRET_* layer, so the service
#    starts clean and fails on the first HubSpot write), and it omitted the
#    in-memory volume at /app/errors the image needs at tool-call time.
#    See docs/CloudRun_RunBook.md P8a/P9. Deploy the MCP first, then run this.
MCP_URL="$(url_of "${MCP}")"
[[ -n "${MCP_URL}" ]] || { echo "MCP ${MCP} is not deployed - run infra/gcp/mcp/01_deploy.sh first" >&2; exit 1; }

# 2) Internal agents — OIDC only; reach HubSpot ONLY via the MCP URL.
deploy_svc "${LDPF}" internal \
  --set-env-vars "${COMMON_ENV},LQABR_MCP_URL=${MCP_URL}"

deploy_svc "${EMAIL}" internal \
  --set-secrets "${MODEL_KEY_ENV}=${MODEL_SECRET}:latest,LQABR_MAILGUN_API_KEY=lqabr-mailgun-api-key:latest" \
  --set-env-vars "${COMMON_ENV},LQABR_MCP_URL=${MCP_URL},LQABR_EMAIL_MODEL=${LQABR_EMAIL_MODEL},MAILGUN_DOMAIN=${MAILGUN_DOMAIN},MAILGUN_FROM=${MAILGUN_FROM},LQABR_SENDER_NAME=${LQABR_SENDER_NAME},LQABR_CTA_URL=${LQABR_CTA_URL}"

deploy_svc "${VOICE}" internal \
  --set-secrets "LQABR_VAPI_API_KEY=lqabr-vapi-api-key:latest" \
  --set-env-vars "${COMMON_ENV},LQABR_MCP_URL=${MCP_URL},VAPI_PHONE_NUMBER_ID=${VAPI_PHONE_NUMBER_ID},VAPI_ASSISTANT_ID=${VAPI_ASSISTANT_ID}"

# 3) Agent Gateway — PUBLIC; deployed last so it knows the agent URLs to route to.
#    allUsers invoker requires the project DRS allowAll exception (owner-set).
deploy_svc "${GTWY}" public \
  --set-secrets "HUBSPOT_SIGNING_SECRET=lqabr-hubspot-signing-secret:latest,MAILGUN_WEBHOOK_SIGNING_KEY=lqabr-mailgun-webhook-signing-key:latest,VAPI_WEBHOOK_SECRET=lqabr-vapi-webhook-secret:latest" \
  --set-env-vars "${COMMON_ENV},LQABR_LEAD_PROFILE_AGENT_URL=$(url_of "${LDPF}"),LQABR_EMAIL_AGENT_URL=$(url_of "${EMAIL}"),LQABR_VOICE_AGENT_URL=$(url_of "${VOICE}")"

# 4) Confirm the public invoker binding on the Gateway (idempotent).
gcloud run services add-iam-policy-binding "${GTWY}" \
  --region "${REGION}" --project "${PROJECT_ID}" \
  --member allUsers --role roles/run.invoker --quiet
# Internal services are invoked as the runtime SA (project-level run.invoker).

GTWY_URL="$(url_of "${GTWY}")"
echo "deploy: all SP-2 services live in ${PROJECT_ID}. Public Gateway: ${GTWY_URL}"
echo "    Point provider webhooks at the Gateway (verifies signatures + routes):"
echo "      HubSpot workflow trigger : ${GTWY_URL}/hooks/hubspot"
echo "      Mailgun (delivered/opened/clicked): ${GTWY_URL}/hooks/mailgun"
echo "      Vapi (call status/outcome):         ${GTWY_URL}/hooks/vapi"
