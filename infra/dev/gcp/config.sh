#!/usr/bin/env bash
# ============================================================
# config.sh — DEV environment (project ldqfingsrv-dev).
# Self-contained: this folder sets up ONLY the dev project.
# SP-2 architecture: Agent Gateway (public) + shared HubSpot MCP +
# internal OIDC agents (Lead Profile, Email, Voice/Vapi), scale-to-zero.
#     source ./config.sh && bash 00_enable_apis.sh
# ============================================================

export LQABR_ENV="dev"

# ── GCP Project ──────────────────────────────────────────────
export PROJECT_ID="ldqfingsrv-dev"
export REGION="us-central1"
export LQABR_LABELS="environment=dev,app=lqabr,owner=platform,cost-center=engineering"

# ── Service accounts ─────────────────────────────────────────
export AGENT_SA_NAME="lqabr-agent-dev"
export AGENT_SA="${AGENT_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# ── Developer group (owner-granted deploy-and-operate roles) ─
export DEV_GROUP="ai2d@aidefinitive.com"

# ── Artifact Registry ────────────────────────────────────────
export AR_REPO="lqabr"
export IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}"

# ── Secret Manager secret names ──────────────────────────────
# SP-2: voice is VAPI. Zoom/ZoomInfo are PARKED (empty containers).
export LQABR_SECRETS=(
  lqabr-anthropic-api-key           # Email agent model (Claude via LiteLLM)
  lqabr-hubspot-access-token        # MCP → HubSpot REST
  lqabr-hubspot-signing-secret      # Gateway → verify HubSpot workflow signature
  lqabr-mailgun-api-key             # Email agent → Mailgun send
  lqabr-mailgun-webhook-signing-key # Gateway → verify Mailgun HMAC
  lqabr-vapi-api-key                # Voice agent → Vapi start call
  lqabr-vapi-webhook-secret         # Gateway → verify Vapi webhook signature
)
export LQABR_SECRETS_PARKED=(
  lqabr-zoominfo-username
  lqabr-zoominfo-password
  lqabr-zoom-account-id
  lqabr-zoom-client-id
  lqabr-zoom-client-secret
  lqabr-zoom-webhook-secret-token
)

# ── Pub/Sub topics (planned spine) ───────────────────────────
export TOPIC_INGESTION="lqabr-ingestion-trigger"
export TOPIC_ENGAGEMENT="lqabr-engagement-events"

echo "config[DEV/gcp]: project=${PROJECT_ID} region=${REGION} sa=${AGENT_SA}"
