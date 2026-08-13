#!/usr/bin/env bash
# ============================================================
# config.sh — PROD deployment config.
# Self-contained: deploys ONLY the prod services from CI-built images.
#     source ./config.sh && bash deploy.sh
# ============================================================

export LQABR_ENV="prod"
export PROJECT_ID="ldqfingsrv-prod"              # CHANGE ME (prod project id)
export REGION="us-central1"                      # CHANGE ME if needed

export AGENT_SA_NAME="lqabr-agent-runtime"
export AGENT_SA="${AGENT_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

export AR_REPO="lqabr"
export IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}"
export IMAGE_TAG="latest"                        # CHANGE ME to the released tag

# ── Service topology — service_name | agent_dir | kind | exposure ─
export LQABR_SERVICES=(
  "lqabr-gtwy|gateway|gateway|public"
  "lqabr-mcp|hubspot_mcp|mcp|internal"
  "lqabr-ldpf|lead_profile|agent|internal"
  "lqabr-email-agent|email|agent|internal"
  "lqabr-voice|voice|agent|internal"
)

# ── Email model (prod = Google/Gemini by default) ────────────
export MODEL_KEY_ENV="LQABR_GOOGLE_API_KEY"
export MODEL_SECRET="lqabr-google-api-key"
export LQABR_EMAIL_MODEL="gemini-2.0-flash"           # CHANGE ME if needed

# ── Non-secret runtime config passed to services ─────────────
export MAILGUN_DOMAIN="mg.yourdomain.com"             # CHANGE ME
export MAILGUN_FROM="LQABR Outreach <outreach@${MAILGUN_DOMAIN}>"
export LQABR_SENDER_NAME="Your Name"                  # CHANGE ME
export LQABR_CTA_URL="https://yourdomain.com/overview"
export VAPI_PHONE_NUMBER_ID=""                         # CHANGE ME
export VAPI_ASSISTANT_ID=""                            # CHANGE ME

echo "config[PROD/deploy]: project=${PROJECT_ID} tag=${IMAGE_TAG} services=${#LQABR_SERVICES[@]}"
