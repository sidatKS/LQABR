#!/usr/bin/env bash
# ============================================================
# agents/gateway — its OWN infra config.  Spec: docs/gateway_deploy_spec.md
#
# The gateway does not read infra/gcp/config.sh and is not deployed by
# infra/gcp/07_deploy_gateway.sh (superseded; see gateway_deploy_spec 11 for
# the statements in that script the code contradicts).
# ============================================================
export PROJECT_ID="${PROJECT_ID:-ldqfingsrv-dev}"
export PROJECT_NUMBER="${PROJECT_NUMBER:-432617526728}"
export REGION="${REGION:-us-central1}"

# The LIVE service name. 07_deploy_gateway.sh still says lqabr-agent-gateway,
# which would create a SECOND service on a new URL - and HubSpot's Target URL
# points at this one.
export SERVICE_NAME="${SERVICE_NAME:-lqabr-dev-gtwy}"
export AR_REPO="${AR_REPO:-lqabr}"
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}"
export IMAGE_TAG="${IMAGE_TAG:-$(cat "$(dirname "${BASH_SOURCE[0]}")/../VERSION" 2>/dev/null || echo latest)}"

export RUNTIME_SA="${RUNTIME_SA:-lqabr-agent-dev@${PROJECT_ID}.iam.gserviceaccount.com}"
export BUILD_SA="${BUILD_SA:-projects/${PROJECT_ID}/serviceAccounts/lqabr-build@${PROJECT_ID}.iam.gserviceaccount.com}"

# The private app's CLIENT SECRET (Auth tab) - the key the v3 webhook signature
# is computed with. NOT lqabr-hubspot-access-token, which is the CRM token.
# Verified against the live service 2026-08-29: HUBSPOT_APP_SECRET is bound to
# lqabr-hubspot-webhook-secret. That secret EXISTS but has ZERO VERSIONS, so
# versions/latest does not resolve, the env var is never injected, and every
# webhook 401s. 07_deploy_gateway.sh names a third variant
# (lqabr-hubspot-app-secret) that does not exist at all.
export APP_SECRET_NAME="${APP_SECRET_NAME:-lqabr-hubspot-webhook-secret}"

# --- Ingress: PUBLIC, and that is mandatory -------------------------------
# HubSpot cannot present a Google ID token, so this service must be
# --allow-unauthenticated. Its only defence is the v3 HMAC verified in
# lib/soloai/protocols/http.py against HUBSPOT_APP_SECRET, gated by
# config.yaml gateway.ingress.signature.enabled - which MUST be true.
export INGRESS="${INGRESS:-all}"

# --- Egress: still needs the VPC ------------------------------------------
# Ingress and egress are independent axes. lqabr-dev-research is
# ingress=internal, and the R-blog-summary route dispatches to it, so an
# off-VPC gateway gets a 404 from Google's front end. The LIVE service has no
# VPC at all (gateway_deploy_spec 4.3).
export VPC_NETWORK="${VPC_NETWORK:-lqabr-vpc}"
export VPC_SUBNET="${VPC_SUBNET:-lqabr-run-uscentral1}"
export VPC_EGRESS="${VPC_EGRESS:-all-traffic}"

# --- Instances: a CORRECTNESS constraint, not a cost knob -----------------
# router.py DedupeStore is an in-memory TTL+LRU set, per process. N instances
# means N independent stores. min=1 because a cold start can exceed HubSpot's
# webhook timeout, and the retry it triggers is exactly the case a per-instance
# store cannot cover. The LIVE service runs maxScale=20.
export MIN_INSTANCES="${MIN_INSTANCES:-1}"
export MAX_INSTANCES="${MAX_INSTANCES:-1}"

# Matches gateway.ingress.max_concurrent_requests in config/config.yaml, which
# also drives an asyncio.Semaphore AND a ConcurrencyGuard. Three throttles;
# keeping them equal avoids an effective limit nobody chose.
export CONCURRENCY="${CONCURRENCY:-10}"
export TIMEOUT="${TIMEOUT:-60}"
export MEMORY="${MEMORY:-512Mi}"
export CPU="${CPU:-1}"

# The Dockerfile sets 1. config/agentgateway.yaml routes use ${LQABR_*_AGENT_URL}
# as their own backends - the same vars dispatch.py posts to - so an enabled
# sidecar forwards to itself. Off by default; the entrypoint degrades cleanly.
export AGENTGATEWAY_ENABLED="${AGENTGATEWAY_ENABLED:-0}"

# --- Downstream agents ----------------------------------------------------
# Variable NAMES are fixed by endpoint_env in config/agents_registry.yaml and
# read at router.py:337. There is no `scheduling` agent in the registry.
export EMAIL_SERVICE="${EMAIL_SERVICE:-lqabr-dev-email-agent}"
export VOICE_SERVICE="${VOICE_SERVICE:-lqabr-dev-txtv}"
export RESEARCH_SERVICE="${RESEARCH_SERVICE:-lqabr-dev-research}"

export INGRESS_PATH="${INGRESS_PATH:-/hubspot/events}"

echo "gateway config: project=${PROJECT_ID} region=${REGION} service=${SERVICE_NAME} tag=${IMAGE_TAG}"
