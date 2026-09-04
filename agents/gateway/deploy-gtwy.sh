#!/usr/bin/env bash
# Build the gateway image from the CURRENT local code and deploy it to the
# existing Cloud Run service lqabr-dev-gtwy (project ldqfingsrv-dev, us-central1).
# Run from the repo root:  bash agents/gateway/deploy-gtwy.sh
set -euo pipefail

PROJECT="ldqfingsrv-dev"
REGION="us-central1"
SERVICE="lqabr-dev-gtwy"
IMAGE="us-central1-docker.pkg.dev/${PROJECT}/lqabr/${SERVICE}"

# The Cloud Run service backing the Research Agent. agents_registry.yaml maps
# the `research` agent to endpoint_env LQABR_RESEARCH_AGENT_URL, so the gateway
# resolves the R-blog-summary route through whatever this variable holds. It is
# NOT in the image: agents/gateway/config/.env (which carries the localhost:8086
# dev value) is excluded from the build context by the root .dockerignore, so
# without an env var on the service the route has no endpoint and every blog
# summary fails to dispatch.
RESEARCH_SERVICE="${RESEARCH_SERVICE:-lqabr-dev-research}"
# Path appended to the service URL. Must match the research agent's A2A route.
RESEARCH_PATH="${RESEARCH_PATH:-/research/campaign/a2a}"

url_of() {
  gcloud run services describe "$1" \
    --region "${REGION}" --project "${PROJECT}" \
    --format 'value(status.url)' 2>/dev/null
}

echo "== 0/3 resolving research agent endpoint"
# An explicit LQABR_RESEARCH_AGENT_URL in the environment always wins, so a
# non-Cloud-Run research deployment can be pointed at without editing this file.
if [[ -n "${LQABR_RESEARCH_AGENT_URL:-}" ]]; then
  RESEARCH_URL="${LQABR_RESEARCH_AGENT_URL}"
  echo "   using LQABR_RESEARCH_AGENT_URL from the environment"
else
  RESEARCH_BASE="$(url_of "${RESEARCH_SERVICE}")"
  if [[ -z "${RESEARCH_BASE}" ]]; then
    echo "ERROR: Cloud Run service '${RESEARCH_SERVICE}' not found in ${PROJECT}/${REGION}." >&2
    echo "       Set RESEARCH_SERVICE=<name>, or export LQABR_RESEARCH_AGENT_URL=<full url>." >&2
    echo "       Services available:" >&2
    gcloud run services list --region "${REGION}" --project "${PROJECT}" \
      --format 'value(metadata.name)' 2>/dev/null | sed 's/^/         /' >&2
    exit 1
  fi
  RESEARCH_URL="${RESEARCH_BASE}${RESEARCH_PATH}"
fi
# Refuse to ship a localhost endpoint to Cloud Run: it would look like a healthy
# deploy and then drop every research hand-off with a connection error.
if [[ "${RESEARCH_URL}" == *localhost* || "${RESEARCH_URL}" == *127.0.0.1* ]]; then
  echo "ERROR: research endpoint is a local address (${RESEARCH_URL}) — refusing to deploy it." >&2
  exit 1
fi
echo "   LQABR_RESEARCH_AGENT_URL=${RESEARCH_URL}"

echo "== 1/3 building ${IMAGE}:latest from local code"
gcloud builds submit . \
  --config infra/gcp/cloud-run/cloudbuild.gateway.yaml \
  --substitutions "_IMAGE=${IMAGE},_VERSION=0.1.0" \
  --project "${PROJECT}"

echo "== 2/3 deploying to ${SERVICE} (keeps existing env vars + secrets)"
# --update-env-vars MERGES: it sets this one key and leaves every other var and
# secret on the service untouched. --set-env-vars would REPLACE the whole block
# and silently drop LQABR_EMAIL_AGENT_URL / LQABR_TEXT_VOICE_AGENT_URL.
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}:latest" \
  --region "${REGION}" \
  --project "${PROJECT}" \
  --update-env-vars "LQABR_RESEARCH_AGENT_URL=${RESEARCH_URL}"

echo "== 3/3 verifying the gateway resolved every enabled agent endpoint"
GTWY_URL="$(url_of "${SERVICE}")"
# /readyz is 503 unless every enabled agent in agents_registry.yaml resolves, so
# it is the check that actually proves the research endpoint took effect.
curl -fsS "${GTWY_URL}/readyz" && echo || {
  echo "readyz reported not-ready — see the JSON above for which agent failed." >&2
  exit 1
}
echo "== done. ${GTWY_URL}"
