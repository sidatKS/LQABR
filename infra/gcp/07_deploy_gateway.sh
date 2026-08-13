#!/usr/bin/env bash
# 07 — build and deploy the Agent Gateway to Cloud Run. Idempotent:
# re-deploys create new revisions.
#
# Run from infra/gcp/ (the script cd's to the repo root for the build):
#   source ./config.sh && bash 07_deploy_gateway.sh
#
# Why this is separate from 05_deploy_agents.sh
# ---------------------------------------------
# The gateway is not an ADK agent and not a provider webhook receiver. It has
# its own Dockerfile (installs the agentgateway binary, runs
# docker-entrypoint.sh) and three deploy properties none of the other services
# share:
#
#   1. PUBLIC ingress is mandatory. HubSpot cannot present a Google identity
#      token, so this service must be --allow-unauthenticated. Its only
#      defence is HubSpot's v3 HMAC signature check, verified in server.py
#      against HUBSPOT_APP_SECRET. That check is load-bearing security here,
#      not a nicety. If it is ever disabled in config.yaml, anyone who learns
#      the URL can inject routing triggers.
#
#   2. Two-pass deploy. The gateway recomputes the signed URI from
#      LQABR_GATEWAY_PUBLIC_URL, so that value must equal the service's own
#      Cloud Run URL exactly. That URL does not exist until after the first
#      deploy — hence pass 1 (create) then pass 2 (set the URL). Any drift
#      between this value and HubSpot's Target URL produces a silent 401, not
#      an obvious error.
#
#   3. Single instance by default. The dedupe store in router.py is in-memory
#      and per-process. Two instances means two independent stores, which
#      means a HubSpot retry can produce duplicate outreach. Until a shared
#      store exists, max-instances is pinned to 1. See KNOWN GAPS below.
set -euo pipefail
: "${PROJECT_ID:?source ./config.sh first}"

SERVICE="lqabr-agent-gateway"
IMAGE="${IMAGE_BASE}/${SERVICE}:latest"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

# ── Tunables (override by exporting before running) ──────────────────────────

# Pinned to 1 while the dedupe store is in-memory. Raising this without a
# shared store WILL allow duplicate agent dispatches on HubSpot retries.
GATEWAY_MAX_INSTANCES="${GATEWAY_MAX_INSTANCES:-1}"

# Cloud Run scale-to-zero plus a Python cold start can exceed HubSpot's
# webhook timeout, producing retries (visible as attemptNumber > 0) and, with
# a per-instance dedupe store, possible duplicate dispatch. Keeping one warm
# instance costs a little and removes the whole class of problem. Set to 0 to
# scale to zero and accept cold starts.
GATEWAY_MIN_INSTANCES="${GATEWAY_MIN_INSTANCES:-1}"

# Matches ingress.max_concurrent_requests in config/config.yaml, which in turn
# matches HubSpot's default 10-concurrent-request throttle. The gateway also
# holds its own asyncio.Semaphore at that limit — these are two independent
# throttles, so keeping them equal avoids an effective limit nobody intended.
GATEWAY_CONCURRENCY="${GATEWAY_CONCURRENCY:-10}"

# The private-app CLIENT SECRET (Auth tab), not the access token. Distinct
# from lqabr-hubspot-access-token, which agents use for CRM reads/writes.
GATEWAY_SECRET_NAME="${GATEWAY_SECRET_NAME:-lqabr-hubspot-app-secret}"

# Request timeout. HubSpot gives up well before this; a long value only lets
# a slow dispatch finish rather than be killed mid-flight.
GATEWAY_TIMEOUT="${GATEWAY_TIMEOUT:-60}"

# The sidecar has never run and cannot as currently configured: the routes in
# config/agentgateway.yaml use ${LQABR_*_AGENT_URL} as their own backends —
# the same variables dispatch.py posts to. One variable means either the
# sidecar is bypassed or it forwards to itself. Deploying with it nominally
# enabled only produces a misleading startup log, so it is off by default.
# See KNOWN GAPS below and deviation D-02.
AGENTGATEWAY_ENABLED="${AGENTGATEWAY_ENABLED:-0}"

# ADK mounts A2A at /a2a/{app_name} (google/adk/cli/fast_api.py). The app name
# is derived from the directory ADK scans, so confirm it against a deployed
# agent before trusting these paths. See KNOWN GAPS.
A2A_EMAIL_PATH="${A2A_EMAIL_PATH:-/a2a/email}"
A2A_VOICE_PATH="${A2A_VOICE_PATH:-/a2a/text_voice}"
A2A_SCHEDULING_PATH="${A2A_SCHEDULING_PATH:-/a2a/scheduling}"

# ── Preflight ────────────────────────────────────────────────────────────────

[[ -f agents/gateway/Dockerfile ]] || {
  echo "07: agents/gateway/Dockerfile not found — run from a full checkout." >&2
  exit 1
}

if ! gcloud secrets describe "${GATEWAY_SECRET_NAME}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  cat >&2 <<EOF
07: secret '${GATEWAY_SECRET_NAME}' does not exist.

    It holds the HubSpot private-app CLIENT SECRET (Auth tab of the Agent
    Gateway app) — the key the v3 webhook signature is computed with. This is
    NOT the same as lqabr-hubspot-access-token.

    Create it:
      gcloud secrets create ${GATEWAY_SECRET_NAME} \\
        --replication-policy automatic --project ${PROJECT_ID}
      printf '%s' 'THE-CLIENT-SECRET' | gcloud secrets versions add \\
        ${GATEWAY_SECRET_NAME} --data-file=- --project ${PROJECT_ID}

    To have 02_secret_manager.sh manage it from now on, add
    '${GATEWAY_SECRET_NAME}' to LQABR_SECRETS in config.sh.
EOF
  exit 1
fi

if ! gcloud secrets versions describe latest \
       --secret "${GATEWAY_SECRET_NAME}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo "07: secret '${GATEWAY_SECRET_NAME}' exists but has no version — add one before deploying." >&2
  exit 1
fi

url_of() {  # <service>  — empty string if the service does not exist
  gcloud run services describe "$1" \
    --region "${REGION}" --project "${PROJECT_ID}" \
    --format 'value(status.url)' 2>/dev/null || true
}

# ── 1) Build ─────────────────────────────────────────────────────────────────

echo "== building ${SERVICE}"
gcloud builds submit . \
  --config infra/gcp/cloud-run/cloudbuild.gateway.yaml \
  --substitutions "_IMAGE=${IMAGE}" \
  --project "${PROJECT_ID}" --quiet

# ── 2) Deploy pass 1 — create/update the service so it has a URL ─────────────
#
# --allow-unauthenticated is deliberate and required; see header note 1.

echo "== deploying ${SERVICE} (pass 1 of 2)"
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --service-account "${AGENT_SA}" \
  --allow-unauthenticated \
  --set-secrets "HUBSPOT_APP_SECRET=${GATEWAY_SECRET_NAME}:latest" \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},AGENTGATEWAY_ENABLED=${AGENTGATEWAY_ENABLED}" \
  --memory 512Mi --cpu 1 \
  --min-instances "${GATEWAY_MIN_INSTANCES}" \
  --max-instances "${GATEWAY_MAX_INSTANCES}" \
  --concurrency "${GATEWAY_CONCURRENCY}" \
  --timeout "${GATEWAY_TIMEOUT}" \
  --project "${PROJECT_ID}" --quiet

GATEWAY_URL="$(url_of "${SERVICE}")"
[[ -n "${GATEWAY_URL}" ]] || { echo "07: could not read the service URL after deploy." >&2; exit 1; }

# ── 3) Resolve agent endpoints ───────────────────────────────────────────────
#
# Deployed by 05_deploy_agents.sh. Missing services are left unset rather than
# pointed at a wrong address: router.py treats an unset endpoint_env as a
# routing error and audits it, which is a far better failure than silently
# dispatching into the void. /readyz reports the same.

EMAIL_URL="$(url_of lqabr-email-agent)"
VOICE_URL="$(url_of lqabr-text-voice-agent)"
SCHED_URL="$(url_of lqabr-scheduling-agent)"

AGENT_ENV=""
MISSING_AGENTS=()
[[ -n "${EMAIL_URL}" ]] && AGENT_ENV+=",LQABR_EMAIL_AGENT_URL=${EMAIL_URL}${A2A_EMAIL_PATH}"           || MISSING_AGENTS+=(lqabr-email-agent)
[[ -n "${VOICE_URL}" ]] && AGENT_ENV+=",LQABR_TEXT_VOICE_AGENT_URL=${VOICE_URL}${A2A_VOICE_PATH}"      || MISSING_AGENTS+=(lqabr-text-voice-agent)
[[ -n "${SCHED_URL}" ]] && AGENT_ENV+=",LQABR_SCHEDULING_AGENT_URL=${SCHED_URL}${A2A_SCHEDULING_PATH}" || MISSING_AGENTS+=(lqabr-scheduling-agent)

# ── 4) Deploy pass 2 — the service now knows its own public URL ──────────────
#
# LQABR_GATEWAY_PUBLIC_URL must match the URL HubSpot calls, character for
# character, because HubSpot signs over the full request URI. See header note 2.

echo "== deploying ${SERVICE} (pass 2 of 2) — setting LQABR_GATEWAY_PUBLIC_URL"
gcloud run services update "${SERVICE}" \
  --region "${REGION}" --project "${PROJECT_ID}" --quiet \
  --update-env-vars "LQABR_GATEWAY_PUBLIC_URL=${GATEWAY_URL}${AGENT_ENV}"

# ── 5) Report ────────────────────────────────────────────────────────────────

INGRESS_PATH="/hubspot/events"

cat <<EOF

============================================================================
07: ${SERVICE} deployed.

  Service URL   ${GATEWAY_URL}
  Health        ${GATEWAY_URL}/healthz     (liveness)
  Readiness     ${GATEWAY_URL}/readyz      (also checks every agent endpoint)
  Instances     min=${GATEWAY_MIN_INSTANCES} max=${GATEWAY_MAX_INSTANCES} concurrency=${GATEWAY_CONCURRENCY}
  Sidecar       AGENTGATEWAY_ENABLED=${AGENTGATEWAY_ENABLED}

NEXT — point HubSpot at it:

  HubSpot -> Settings -> Integrations -> Private Apps -> Agent Gateway
    -> Edit app -> Webhooks -> Target URL:

      ${GATEWAY_URL}${INGRESS_PATH}

  Include the ${INGRESS_PATH} path. The gateway listens on that route only;
  a bare domain 404s every delivery.

  KNOWN HUBSPOT UI DEFECT: the FIRST commit after a fresh page load silently
  does not persist — the page reports success and keeps the old value. The
  second commit sticks. Reload the page and re-read the field to confirm.
  Allow up to 5 minutes afterwards; HubSpot caches webhook settings.

VERIFY:
  gcloud run services logs read ${SERVICE} --region ${REGION} --project ${PROJECT_ID} --limit 50

  A good delivery logs, in order:
    hubspot_ingress_received  with "signature_verified": true
    routing_decision          with a trigger_id, agent and route_id
    agent_dispatch            with "status": 200
    run_summary               with "routed": 1, "dispatched_ok": 1

  "signature_verified": false means LQABR_GATEWAY_PUBLIC_URL and HubSpot's
  Target URL disagree, or ${GATEWAY_SECRET_NAME} is not the client secret.
============================================================================
EOF

if ((${#MISSING_AGENTS[@]})); then
  cat <<EOF
WARNING: agent service(s) not deployed: ${MISSING_AGENTS[*]}
  Their LQABR_*_AGENT_URL variables are unset, so matching events will be
  audited as routing errors. Run 05_deploy_agents.sh, then re-run this script
  to wire the URLs.

EOF
fi

cat <<'EOF'
KNOWN GAPS — dispatch will not reach a real agent yet:

  1. ADK does not expose an A2A endpoint by default. --a2a defaults to False
     in google-adk 2.3.0, and infra/gcp/cloud-run/entrypoint.sh runs
     `adk api_server` without it. Deployed agents therefore serve ADK's REST
     API but not JSON-RPC message/send — the gateway will get a 404.

  2. The A2A path may not match. With --a2a enabled, ADK mounts each app at
     /a2a/{app_name}. Confirm the resolved app name against a deployed agent
     and override A2A_*_PATH here if it differs.

  3. No identity token is attached. The agent services are deployed
     --no-allow-unauthenticated, so calls need a Google-signed ID token.
     01_service_accounts.sh already grants roles/run.invoker to the runtime
     service account, but IAM permission is not the same as a token in the
     request: A2AClient (lib/soloai/protocols/a2a.py) uses plain requests with
     no Authorization header, so calls will 403 until token minting is added.

  4. No agent has a handler for object_id. The gateway sends
     metadata.object_id (deviation D-05); the email agent's tools are
     list_email_queue() and send_outreach_email(contact_email) — neither maps
     a contact id to a lead. lqabr_core's HubSpotClient.get_lead(contact_id)
     is the missing bridge.

  5. Dedupe is per-instance (in-memory). max-instances is pinned to 1 for
     this reason. Raising it without a shared store risks duplicate outreach
     on HubSpot retries.

  Until 1-4 are resolved, deploying is still worthwhile: it gives HubSpot a
  permanent URL, removes the ngrok dependency, and proves ingress, signature
  verification and routing in Cloud Run. Dispatch failures are audited
  explicitly rather than lost.
EOF
