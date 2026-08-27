# Research agent deployment settings. Mirrors agents/summary/infra/config.sh.
# Every value is overridable from the environment.
export PROJECT_ID="${PROJECT_ID:-ldqfingsrv-dev}"
export REGION="${REGION:-us-central1}"

# Service/image name follows the LIVE platform convention lqabr-dev-<component>
# (lqabr-dev-gtwy, lqabr-dev-txtv, lqabr-dev-mcp), NOT summary's older
# lqabr-<agent>-agent spelling. See the runbook's note on competing naming.
export SERVICE_NAME="${SERVICE_NAME:-lqabr-dev-research}"
export AR_REPO="${AR_REPO:-lqabr}"
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}"
export IMAGE_TAG="${IMAGE_TAG:-$(cat "$(dirname "${BASH_SOURCE[0]}")/../VERSION" 2>/dev/null || echo latest)}"

# The single runtime SA (runbook decision D2). NOTE: summary/infra/config.sh
# still names lqabr-agent-runtime@, which does not exist in this project.
export RUNTIME_SA="${RUNTIME_SA:-lqabr-agent-dev@${PROJECT_ID}.iam.gserviceaccount.com}"

# The real deployed MCP (P8a), not a guessed hostname.
export LQABR_RESEARCH_MCP_BASE_URL="${LQABR_RESEARCH_MCP_BASE_URL:-https://lqabr-dev-mcp-432617526728.us-central1.run.app/mcp}"

# --- Secret Manager names (spec §4.2) --------------------------------------
# These are SECRET NAMES, not values. --set-secrets injects each value under the
# env var research expects, which secrets.py:_env_name derives by uppercasing the
# secret name and turning hyphens into underscores:
#     lqabr-anthropic-api-key    -> LQABR_ANTHROPIC_API_KEY
#     lqabr-hubspot-access-token -> LQABR_HUBSPOT_ACCESS_TOKEN
# research needs BOTH: the model key, and a HubSpot token for the one read-only
# direct lookup the MCP cannot serve (spec §4.3, hubspot_direct.py).
export MODEL_SECRET="${MODEL_SECRET:-lqabr-anthropic-api-key}"
export HUBSPOT_SECRET="${HUBSPOT_SECRET:-lqabr-hubspot-access-token}"

# --- Network (spec §4.1) ---------------------------------------------------
# INBOUND: ingress=internal rejects the public internet.
# OUTBOUND: --network/--subnet/--vpc-egress put the service ON the VPC, which is
# what lets it reach the ingress=internal MCP at all, and routes SaaS calls
# (Anthropic, api.hubapi.com) out through Cloud NAT's fixed IP 34.45.4.100.
# Two separate axes; the service needs both.
export VPC_NETWORK="${VPC_NETWORK:-lqabr-vpc}"
export VPC_SUBNET="${VPC_SUBNET:-lqabr-run-uscentral1}"
export VPC_EGRESS="${VPC_EGRESS:-all-traffic}"
export INGRESS="${INGRESS:-internal}"

# --- Runtime shape (spec §4, §4.1) -----------------------------------------
export PORT="${PORT:-8080}"
export CPU="${CPU:-1}"
export MEMORY="${MEMORY:-1Gi}"
# 600s, not the 300s default: one run is a page fetch PLUS an Anthropic call
# with web search (search_max_uses=5, search_timeout_seconds=90).
export TIMEOUT="${TIMEOUT:-600}"
export MIN_INSTANCES="${MIN_INSTANCES:-0}"
export MAX_INSTANCES="${MAX_INSTANCES:-3}"

# --- Writable log path (spec §4.1) -----------------------------------------
# config/config.yaml sets logging.file: logs/agents/research/agent.log, which
# resolves to /app/logs/... inside the image, where that tree does not exist.
# tmpfs: counts against MEMORY, discarded on scale-to-zero. It exists so file
# logging does not ERROR, not because anything reads it -- stdout is the real
# log path. Set LQABR_RESEARCH_LOG_FILE="" to disable file logging instead.
export LOG_VOLUME_NAME="${LOG_VOLUME_NAME:-logs}"
export LOG_MOUNT_PATH="${LOG_MOUNT_PATH:-/app/logs}"

# --- Agent settings set explicitly at deploy (spec §4) ---------------------
# MUST be env, never secret_manager: research is standalone and its
# requirements.txt has no google-cloud-secret-manager, so the secret_manager
# path raises ImportError at runtime (spec §4.2, F1).
export LQABR_RESEARCH_SECRETS_SOURCE="${LQABR_RESEARCH_SECRETS_SOURCE:-env}"
# json, not auto: stdout is not a terminal on Cloud Run, and explicit beats
# inferred when Cloud Logging is doing the parsing.
export LQABR_RESEARCH_LOG_FORMAT="${LQABR_RESEARCH_LOG_FORMAT:-json}"
# Writes are live. Set to 1 to suppress HubSpot writes.
export LQABR_RESEARCH_DRY_RUN="${LQABR_RESEARCH_DRY_RUN:-0}"

# NOTE deliberately NOT set: LQABR_RESEARCH_GCP_PROJECT (F2) applies only to the
# secret_manager source and implies the wrong mode if present.

# --- Cloud Build identity (runbook P3) -------------------------------------
# Cloud Build will NOT accept a human account or the Google-managed
# 432617526728@cloudbuild.gserviceaccount.com ("provide a user-managed service
# account"), and the default compute SA holds no roles at all. This SA is
# user-managed and holds only what a build needs.
export BUILD_SA="${BUILD_SA:-projects/${PROJECT_ID}/serviceAccounts/lqabr-build@${PROJECT_ID}.iam.gserviceaccount.com}"
