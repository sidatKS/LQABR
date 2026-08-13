#!/usr/bin/env bash
# ============================================================
# config.sh — single source of truth for every infra script.
# Edit ONCE, then `source ./config.sh` before running any script.
# Do not hardcode values in individual scripts.
# ============================================================

# ── GCP Project ──────────────────────────────────────────────
export PROJECT_ID="ldqfingsrv-dev"          # CHANGE ME
export REGION="us-central1"                      # CHANGE ME if needed

# ── Service accounts ─────────────────────────────────────────
export AGENT_SA_NAME="lqabr-agent-runtime"
export AGENT_SA="${AGENT_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# ── Artifact Registry ────────────────────────────────────────
export AR_REPO="lqabr"
export IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}"

# ── Secret Manager secret names (one per service credential) ─
# Values are set in 02_secret_manager.sh; agents read them via
# lqabr_core.secrets (env override locally, --set-secrets on Cloud Run).
export LQABR_SECRETS=(
  lqabr-google-api-key
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
export MAILGUN_DOMAIN="mg.yourdomain.com"        # CHANGE ME
export MAILGUN_FROM="LQABR Outreach <outreach@${MAILGUN_DOMAIN}>"
export TWILIO_FROM_NUMBER="+15551234567"         # CHANGE ME
export LQABR_SENDER_NAME="Your Name"             # CHANGE ME
export LQABR_CTA_URL="https://yourdomain.com/overview"
# Optional fixed Zoom booking page (else the Scheduler API is queried):
export ZOOM_BOOKING_URL=""

echo "config: project=${PROJECT_ID} region=${REGION} sa=${AGENT_SA}"
