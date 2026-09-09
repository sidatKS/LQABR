#!/usr/bin/env bash
# ============================================================
# agents/summary — its OWN infra config.
#
# This agent does not read infra/gcp/config.sh and is not listed in
# infra/gcp/05_deploy_agents.sh. Deploying it changes nothing for any
# other agent, and a repo-wide infra change cannot break it. That is the
# point; edit here, then `source infra/config.sh`.
# ============================================================

export PROJECT_ID="${PROJECT_ID:-ldqfingsrv-dev}"
export REGION="${REGION:-us-central1}"

export SERVICE_NAME="lqabr-summary-agent"
export AR_REPO="${AR_REPO:-lqabr}"
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}"
export IMAGE_TAG="${IMAGE_TAG:-$(cat "$(dirname "${BASH_SOURCE[0]}")/../VERSION" 2>/dev/null || echo latest)}"

# Runtime service account. Needs roles/secretmanager.secretAccessor for the
# model key, and run.invoker on the HubSpot MCP service if that service is
# deployed authenticated.
export RUNTIME_SA="${RUNTIME_SA:-lqabr-agent-runtime@${PROJECT_ID}.iam.gserviceaccount.com}"

# The MCP container this agent dials at runtime. THE one coupling.
export LQABR_SUMMARY_MCP_BASE_URL="${LQABR_SUMMARY_MCP_BASE_URL:-https://lqabr-hubspot-mcp-${REGION}.run.app/mcp}"

# Secret Manager secret holding the model provider key.
export MODEL_SECRET="${MODEL_SECRET:-lqabr-anthropic-api-key}"

export LQABR_SUMMARY_MODEL="${LQABR_SUMMARY_MODEL:-anthropic/claude-sonnet-5}"
export LQABR_SUMMARY_HUBSPOT_OBJECT_TYPE="${LQABR_SUMMARY_HUBSPOT_OBJECT_TYPE:-ticket}"
export LQABR_SUMMARY_HUBSPOT_SUMMARY_PROPERTY="${LQABR_SUMMARY_HUBSPOT_SUMMARY_PROPERTY:-blog_summary}"
export LQABR_SUMMARY_HUBSPOT_INDUSTRY_PROPERTY="${LQABR_SUMMARY_HUBSPOT_INDUSTRY_PROPERTY:-blog_industry}"
export LQABR_SUMMARY_ROUTES="${LQABR_SUMMARY_ROUTES:-all}"
# First deploy should be a dry run: the summary is computed and logged, and
# nothing is written to the CRM until you flip this to 0.
export LQABR_SUMMARY_DRY_RUN="${LQABR_SUMMARY_DRY_RUN:-1}"
export LQABR_SUMMARY_MCP_STARTUP_CHECK="${LQABR_SUMMARY_MCP_STARTUP_CHECK:-warn}"

echo "summary-agent config: project=${PROJECT_ID} region=${REGION} service=${SERVICE_NAME} tag=${IMAGE_TAG}"
