#!/usr/bin/env bash
# ============================================================
# config.dev.sh — DEV environment config (project ldqfingsrv-dev).
# Prod lives in config.sh; this file mirrors it with dev values.
# Source this instead of config.sh before running any infra script:
#     source ./config.dev.sh && bash 00_enable_apis.sh
# Do not hardcode values in individual scripts.
# ============================================================

# ── Environment marker ───────────────────────────────────────
export LQABR_ENV="dev"

# ── GCP Project ──────────────────────────────────────────────
export PROJECT_ID="ldqfingsrv-dev"
export REGION="us-central1"

# ── Resource labels (applied by provisioning scripts) ────────
# environment=dev separates every dev resource from prod.
export LQABR_LABELS="environment=dev,app=lqabr,owner=platform,cost-center=engineering"

# ── Service accounts ─────────────────────────────────────────
# Dev runtime SA is named distinctly (…-agent-dev) so it's identifiable
# by principalEmail in logs / process signatures without checking the
# project. Prod uses lqabr-agent-runtime.
export AGENT_SA_NAME="lqabr-agent-dev"
export AGENT_SA="${AGENT_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# ── Artifact Registry ────────────────────────────────────────
export AR_REPO="lqabr"
export IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}"

# ── Secret Manager secret names (one per service credential) ─
# Same NAMES as prod; the VALUES differ and live only in this
# project's Secret Manager. Set them via 02_secret_manager.sh.
export LQABR_SECRETS=(
  lqabr-anthropic-api-key
  lqabr-hubspot-access-token
  lqabr-mailgun-api-key
  lqabr-mailgun-webhook-signing-key
  lqabr-twilio-account-sid
  lqabr-twilio-auth-token
  lqabr-zoominfo-username
  lqabr-zoominfo-password
  lqabr-zoom-account-id
  lqabr-zoom-client-id
  lqabr-zoom-client-secret
  lqabr-zoom-webhook-secret-token
)

# ── Pub/Sub topics ───────────────────────────────────────────
export TOPIC_INGESTION="lqabr-ingestion-trigger"
export TOPIC_ENGAGEMENT="lqabr-engagement-events"

# ── Cloud Run services ───────────────────────────────────────
# ADK agents (adk api_server):    lqabr-<agent>-agent
# Webhook apps (uvicorn/FastAPI): lqabr-<agent>-webhook
export ADK_AGENTS=(ingestion lead_profile email text_voice scheduling orchestrator)
export WEBHOOK_AGENTS=(email text_voice scheduling)

# ── Non-secret runtime config passed to services ─────────────
export MAILGUN_DOMAIN="dev.reply.tekninjas.com"  # CHANGE ME (dev sending domain)
export MAILGUN_FROM="LQABR Dev Outreach <outreach@${MAILGUN_DOMAIN}>"
export TWILIO_FROM_NUMBER="+15551234567"         # CHANGE ME (dev number)
export LQABR_SENDER_NAME="Your Name"             # CHANGE ME
export LQABR_CTA_URL="https://dev.yourdomain.com/overview"
# Optional fixed Zoom booking page (else the Scheduler API is queried):
export ZOOM_BOOKING_URL=""

echo "config[DEV]: project=${PROJECT_ID} region=${REGION} sa=${AGENT_SA}"
