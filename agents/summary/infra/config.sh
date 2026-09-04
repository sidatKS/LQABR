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

# Follows the LIVE platform convention lqabr-dev-<component>, matching
# lqabr-dev-mcp / lqabr-dev-gtwy / lqabr-dev-txtv / lqabr-dev-research.
# WAS lqabr-summary-agent (this agent's own older spelling) — realigned
# 2026-08-26 before the first deploy, while renaming was still free.
export SERVICE_NAME="lqabr-dev-summary"
export AR_REPO="${AR_REPO:-lqabr}"
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/${SERVICE_NAME}"
export IMAGE_TAG="${IMAGE_TAG:-$(cat "$(dirname "${BASH_SOURCE[0]}")/../VERSION" 2>/dev/null || echo latest)}"

# Runtime service account. Needs roles/secretmanager.secretAccessor for the
# model key, and run.invoker on the HubSpot MCP service if that service is
# deployed authenticated.
# The single runtime service account for every LQABR service (runbook D2).
# WAS lqabr-agent-runtime@, which does not exist in this project — the deploy
# would have failed at --service-account with a confusing IAM error.
export RUNTIME_SA="${RUNTIME_SA:-lqabr-agent-dev@${PROJECT_ID}.iam.gserviceaccount.com}"

# The MCP container this agent dials at runtime. THE one coupling.
# The real MCP service URL, deployed 2026-08-26. WAS a guessed hostname
# (lqabr-hubspot-mcp-${REGION}.run.app) that resolves to nothing. The service
# is `lqabr-dev-mcp`, is ingress=internal, and requires an ID token — which
# summary_core/mcp/client.py now attaches automatically.
export LQABR_SUMMARY_MCP_BASE_URL="${LQABR_SUMMARY_MCP_BASE_URL:-https://lqabr-dev-mcp-432617526728.us-central1.run.app/mcp}"

# Secret Manager secret holding the model provider key.
# --- Network (runbook P6) -------------------------------------------------
# INBOUND: ingress=internal rejects the public internet and accepts traffic
# from inside the VPC / internal Google sources.
# OUTBOUND: --network/--subnet/--vpc-egress put the service ON the VPC, which
# is what lets it reach the ingress=internal MCP at all, and routes SaaS calls
# (Anthropic) out through Cloud NAT's fixed IP 34.45.4.100.
# These are two separate axes; the service needs both.
export VPC_NETWORK="${VPC_NETWORK:-lqabr-vpc}"
export VPC_SUBNET="${VPC_SUBNET:-lqabr-run-uscentral1}"
export VPC_EGRESS="${VPC_EGRESS:-all-traffic}"
export INGRESS="${INGRESS:-internal}"

# --- MCP tool names -------------------------------------------------------
# summary_core's defaults (get_lead_profile_details / post_patch_crm) do not
# exist on the deployed MCP, which exposes exactly four tools:
#   get_blog_summary  get_lead_profile  upsert_blog_summary  upsert_lead_profile
# This agent works on BLOG SUMMARIES on tickets (see OBJECT_TYPE/SUMMARY_PROPERTY
# below), so it reads and writes the blog-summary pair. Config change only —
# the agent already sources these from env (settings.py:194-196).
export LQABR_SUMMARY_MCP_TOOL_READ="${LQABR_SUMMARY_MCP_TOOL_READ:-get_blog_summary}"
export LQABR_SUMMARY_MCP_TOOL_WRITE="${LQABR_SUMMARY_MCP_TOOL_WRITE:-upsert_blog_summary}"

# Write style. summary_core defaults to "patch", which sends
# {object_id, properties{}} and therefore REQUIRES a HubSpot object id. The
# deployed MCP instead exposes upsert_blog_summary, which takes four flat args
# and keys on blog_published_at — it creates or updates the ticket itself, so
# no object id is needed. Without this the write path asks for an id that the
# caller has no reason to know.
export LQABR_SUMMARY_MCP_WRITE_STYLE="${LQABR_SUMMARY_MCP_WRITE_STYLE:-blog_summary}"

export MODEL_SECRET="${MODEL_SECRET:-lqabr-anthropic-api-key}"

export LQABR_SUMMARY_MODEL="${LQABR_SUMMARY_MODEL:-anthropic/claude-sonnet-5}"
export LQABR_SUMMARY_HUBSPOT_OBJECT_TYPE="${LQABR_SUMMARY_HUBSPOT_OBJECT_TYPE:-ticket}"
export LQABR_SUMMARY_HUBSPOT_SUMMARY_PROPERTY="${LQABR_SUMMARY_HUBSPOT_SUMMARY_PROPERTY:-blog_summary}"
export LQABR_SUMMARY_HUBSPOT_INDUSTRY_PROPERTY="${LQABR_SUMMARY_HUBSPOT_INDUSTRY_PROPERTY:-blog_industry}"
export LQABR_SUMMARY_ROUTES="${LQABR_SUMMARY_ROUTES:-all}"
# First deploy should be a dry run: the summary is computed and logged, and
# nothing is written to the CRM until you flip this to 0.
# Writes are LIVE. Was 1 (dry-run) through initial bring-up; flipped to 0 on
# 2026-08-26 once the MCP write path was verified end to end. Set
# LQABR_SUMMARY_DRY_RUN=1 in the environment to suppress writes again.
export LQABR_SUMMARY_DRY_RUN="${LQABR_SUMMARY_DRY_RUN:-0}"
export LQABR_SUMMARY_MCP_STARTUP_CHECK="${LQABR_SUMMARY_MCP_STARTUP_CHECK:-warn}"

echo "summary-agent config: project=${PROJECT_ID} region=${REGION} service=${SERVICE_NAME} tag=${IMAGE_TAG}"

# --- Cloud Build identity (runbook P3) -------------------------------------
# Cloud Build will NOT accept a human account or the Google-managed
# 432617526728@cloudbuild.gserviceaccount.com ("provide a user-managed service
# account"), and the default compute SA holds no roles at all. This SA is
# user-managed and holds only what a build needs.
export BUILD_SA="${BUILD_SA:-projects/${PROJECT_ID}/serviceAccounts/lqabr-build@${PROJECT_ID}.iam.gserviceaccount.com}"

# --- Writable log path -----------------------------------------------------
# settings.py resolves the agent log to <root>/logs/agents/summary/agent.log,
# and inside the image <root> is /app. The Dockerfile chowns /app to `agent`,
# but Cloud Run's container filesystem is otherwise read-only-ish and the
# nested logs/ tree does not exist in the image. 03_deploy_run.sh therefore
# mounts an in-memory volume at /app/logs so the agent can create it.
#
# NOTE this is tmpfs: it counts against --memory and is discarded on
# scale-to-zero. It exists so file logging does not ERROR, not because anything
# reads it — stdout is the real log path, and Cloud Logging is where to look.
# Set LQABR_SUMMARY_LOG_FILE="" to disable file logging entirely.
