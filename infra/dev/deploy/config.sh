#!/usr/bin/env bash
# ============================================================
# config.sh — DEV deployment config (project ldqfingsrv-dev).
# Self-contained: deploys ONLY the dev services from CI-built images.
#     source ./config.sh && bash deploy.sh
# ============================================================

export LQABR_ENV="dev"
export PROJECT_ID="ldqfingsrv-dev"
export REGION="us-central1"

export AGENT_SA_NAME="lqabr-agent-dev"
export AGENT_SA="${AGENT_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

export AR_REPO="lqabr"
export IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}"
export IMAGE_TAG="latest"                        # CHANGE ME to the CI-built tag (e.g. git sha)

# ── Service topology — service_name | agent_dir | kind | exposure ─
export LQABR_SERVICES=(
  "lqabr-dev-gtwy|gateway|gateway|public"
  "lqabr-dev-mcp|hubspot_mcp|mcp|internal"
  "lqabr-dev-ldpf|lead_profile|agent|internal"
  "lqabr-dev-email-agent|email|agent|internal"
  "lqabr-dev-voice|voice|agent|internal"
)

# ── Email model (dev = Anthropic/Claude via LiteLLM) ─────────
export MODEL_KEY_ENV="LQABR_ANTHROPIC_API_KEY"
export MODEL_SECRET="lqabr-anthropic-api-key"
export LQABR_EMAIL_MODEL="claude-3-5-sonnet-latest"   # CHANGE ME if needed

# ── Non-secret runtime config passed to services ─────────────
export MAILGUN_DOMAIN="dev.reply.tekninjas.com"       # CHANGE ME (dev sending domain)
export MAILGUN_FROM="LQABR Dev Outreach <outreach@${MAILGUN_DOMAIN}>"
export LQABR_SENDER_NAME="Your Name"                  # CHANGE ME
export LQABR_CTA_URL="https://dev.yourdomain.com/overview"
export VAPI_PHONE_NUMBER_ID=""                         # CHANGE ME
export VAPI_ASSISTANT_ID=""                            # CHANGE ME

echo "config[DEV/deploy]: project=${PROJECT_ID} tag=${IMAGE_TAG} services=${#LQABR_SERVICES[@]}"
