#!/usr/bin/env bash
# Deploy the Agent Gateway. Spec: docs/gateway_deploy_spec.md 4.
#
# TWO PASSES, and the reason is not cosmetic. LQABR_GATEWAY_PUBLIC_URL must
# equal the URL HubSpot calls character for character, because the v3 signature
# is computed over the full request URI and Cloud Run rewrites Host. That URL
# does not exist until the service does. Drift between the two is a silent,
# permanent 401 - never a visible error.
#
# Idempotent: re-running redeploys the same service.
set -euo pipefail
source "$(dirname "$0")/config.sh"

url_of() {  # <service> -> URL, or "" if it does not exist
  gcloud run services describe "$1" --region "${REGION}" --project "${PROJECT_ID}" \
    --format='value(status.url)' 2>/dev/null || true
}

# ---------------------------------------------------------------- pass 1
echo "== pass 1/2: deploy ${SERVICE_NAME}"
gcloud run deploy "${SERVICE_NAME}" \
  --image "${IMAGE}:${IMAGE_TAG}" \
  --project "${PROJECT_ID}" --region "${REGION}" \
  --service-account "${RUNTIME_SA}" \
  --execution-environment gen2 \
  --network "${VPC_NETWORK}" \
  --subnet "${VPC_SUBNET}" \
  --vpc-egress "${VPC_EGRESS}" \
  --ingress "${INGRESS}" \
  --allow-unauthenticated \
  --port 8080 \
  --cpu "${CPU}" --memory "${MEMORY}" --timeout "${TIMEOUT}" \
  --min-instances "${MIN_INSTANCES}" \
  --max-instances "${MAX_INSTANCES}" \
  --concurrency "${CONCURRENCY}" \
  --set-secrets "HUBSPOT_APP_SECRET=${APP_SECRET_NAME}:latest" \
  --set-env-vars "AGENTGATEWAY_ENABLED=${AGENTGATEWAY_ENABLED}" \
  --quiet

# why: --allow-unauthenticated  HubSpot cannot mint a Google ID token. REQUIRED.
#                               The v3 HMAC is the only defence (spec 4.2).
# why: --ingress all            same reason; internal would block HubSpot entirely.
# why: --network/--subnet/--vpc-egress
#                               EGRESS, not ingress. lqabr-dev-research is
#                               ingress=internal and R-blog-summary dispatches to
#                               it; off-VPC that is a 404 (spec 4.3).
# why: --max-instances 1        DedupeStore is in-memory per process (router.py:363).
#                               N instances = N stores (spec 4.5).
# why: --min-instances 1        a cold start can exceed HubSpot's webhook timeout;
#                               the retry it causes is exactly what a per-instance
#                               store cannot cover.
# why: --concurrency 10         matches ingress.max_concurrent_requests, the
#                               asyncio.Semaphore and the ConcurrencyGuard (spec 4.6).
# why: --set-secrets HUBSPOT_APP_SECRET
#                               the private app's CLIENT SECRET, not the CRM token.
# why: AGENTGATEWAY_ENABLED=0   the sidecar's backends are the same
#                               LQABR_*_AGENT_URL vars dispatch posts to (spec 4.7).

GATEWAY_URL="$(url_of "${SERVICE_NAME}")"
[[ -n "${GATEWAY_URL}" ]] || { echo "FATAL: no URL after pass 1" >&2; exit 1; }

# ---------------------------------------------------------------- pass 2
# Variable NAMES come from endpoint_env in config/agents_registry.yaml and are
# read at router.py:337. A missing service is left UNSET rather than pointed at
# a wrong address: router.py treats an unset endpoint_env as a routing error and
# audits it, and /readyz reports it - both far better than dispatching into the void.
# WIRE_AGENTS=0 deploys the ingress WITHOUT any agent endpoint. Use it to test
# HubSpot triggers without the risk of contacting a real lead: a matching event
# is audited as a routing error and answered 503 instead of dispatched.
# NOTE HubSpot treats 503 as retryable, and the gateway deliberately does NOT
# record those ids in the dedupe store, so each such trigger WILL be redelivered.
WIRE_AGENTS="${WIRE_AGENTS:-1}"

if [[ "${WIRE_AGENTS}" == "1" ]]; then
  EMAIL_URL="$(url_of "${EMAIL_SERVICE}")"
  VOICE_URL="$(url_of "${VOICE_SERVICE}")"
  RESEARCH_URL="$(url_of "${RESEARCH_SERVICE}")"
else
  EMAIL_URL=""; VOICE_URL=""; RESEARCH_URL=""
  echo ">> WIRE_AGENTS=0 - deploying ingress only; no agent will be dispatched."
  echo ">> Matching events will be audited as routing errors and answered 503."
fi

ENVS="LQABR_GATEWAY_PUBLIC_URL=${GATEWAY_URL}"
MISSING=()
[[ -n "${EMAIL_URL}"    ]] && ENVS+=",LQABR_EMAIL_AGENT_URL=${EMAIL_URL}"           || MISSING+=("${EMAIL_SERVICE}")
[[ -n "${VOICE_URL}"    ]] && ENVS+=",LQABR_TEXT_VOICE_AGENT_URL=${VOICE_URL}"      || MISSING+=("${VOICE_SERVICE}")
[[ -n "${RESEARCH_URL}" ]] && ENVS+=",LQABR_RESEARCH_AGENT_URL=${RESEARCH_URL}"     || MISSING+=("${RESEARCH_SERVICE}")

echo "== pass 2/2: setting the URLs that could not exist before pass 1"
gcloud run services update "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" --region "${REGION}" --quiet \
  --update-env-vars "${ENVS}"

cat <<INFO

deployed: ${GATEWAY_URL}

  ingress ${INGRESS} (PUBLIC, and required)   instances min=${MIN_INSTANCES} max=${MAX_INSTANCES}   concurrency=${CONCURRENCY}
  vpc     ${VPC_NETWORK}/${VPC_SUBNET} egress=${VPC_EGRESS}

  Point HubSpot at:  ${GATEWAY_URL}${INGRESS_PATH}
  The gateway serves that path ONLY; a bare domain 404s every delivery. The
  Target URL must match LQABR_GATEWAY_PUBLIC_URL exactly or every delivery 401s.

  Verify:  bash infra/04_verify.sh      (docs/gateway_verify_spec.md)
INFO

if ((${#MISSING[@]})); then
  if [[ "${WIRE_AGENTS}" == "1" ]]; then
    cat <<INFO
WARNING: not deployed, so their endpoint_env is UNSET: ${MISSING[*]}
  /readyz will report 503 and matching events are audited as routing errors
  rather than dispatched. Deploy them, then re-run this script.
INFO
  else
    cat <<INFO
INGRESS-ONLY deploy (WIRE_AGENTS=0). Unset by choice: ${MISSING[*]}
  Expected now:  /readyz 503, verify A11 and C3 FAIL - that is this mode, not a fault.
  Still proven:  A1-A10, B, and C1/C2/C4/C5/C6/C7 (signature + routing).
  A real matching trigger returns 503 and HubSpot WILL redeliver it.
  When the agents are ready:  bash infra/03_deploy_run.sh      (WIRE_AGENTS defaults to 1)
INFO
  fi
fi
