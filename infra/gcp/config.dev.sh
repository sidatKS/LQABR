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
# Three service kinds (infra/gcp/cloud-run/entrypoint.sh):
#
#   SERVICE_AGENTS  the agent's OWN domain surface — uvicorn service_app:app.
#                   POST /hubspot/campaign, the Mailgun route, /health AND
#                   /healthz. THIS is what the gateway dispatches to.
#   ADK_AGENTS      the generic ADK api_server — POST /run with an ADK
#                   envelope after a session handshake. A gateway domain
#                   call cannot reach it. For agents with no domain surface
#                   yet.
#   WEBHOOK_AGENTS  provider events only — uvicorn webhook_app:app.
#
# email moved from ADK_AGENTS to SERVICE_AGENTS on 2026-08-04: the gateway
# dispatches a domain call (as it does to text_voice), which the ADK runner
# cannot parse. The service NAME is pinned below so the existing URL — and
# therefore LQABR_EMAIL_AGENT_URL — does not change.
#
# email is NOT in WEBHOOK_AGENTS (2026-08-04). Platform Engineering:
#
#   Swaroop, 12:45 — "why do you guys create the webhook image?"
#   Swaroop, 15:08 — "your mail gun has to send back the event triggers back
#                     to the SAME agent."
#
# One image, one service, one endpoint. Mailgun is configured with the email
# agent's own URL and the handling is business logic inside the agent. The
# separate webhook image is gone and agents/email/src/webhook_app.py is
# deleted, so it cannot be rebuilt by a stray build arg.
#
# Because Mailgun must be able to POST to it, that service is deployed
# --allow-unauthenticated. /mailgun/events proves itself via the Mailgun
# HMAC. /hubspot/campaign and /engagement/sync are currently
# UNAUTHENTICATED — the gateway-token check was removed 2026-08-05.
export SERVICE_AGENTS=(email)
export ADK_AGENTS=(ingestion lead_profile text_voice scheduling orchestrator)
export WEBHOOK_AGENTS=(text_voice scheduling)

# Cloud Run service name overrides, "<agent>:<kind>=<service name>". Without
# this, email's service kind would deploy as lqabr-email-service and orphan
# the URL the gateway already holds.
export SERVICE_NAME_OVERRIDES=("email:service=lqabr-email-agent")

# Route groups the email service exposes. `all` is the deployed shape: one
# service serving the gateway entry AND the Mailgun push.
export LQABR_EMAIL_ROUTES="all"

# ── Credential source ────────────────────────────────────────
# secret_manager = ignore the environment, always read the Secret Manager
# API. This is what makes "it comes from Secret Manager" true and provable;
# every resolution logs the secret NAME and its source (never the value).
# Needs roles/secretmanager.secretAccessor on the runtime SA.
# Use `auto` to prefer --set-secrets injection and skip the API call.
export LQABR_SECRETS_SOURCE="secret_manager"

# Model is config-driven. The provider's API key is resolved from Secret
# Manager at runtime (lqabr_core.model.ensure_provider_credentials), so an
# Anthropic model does NOT need ANTHROPIC_API_KEY in the environment.
export LQABR_EMAIL_MODEL="anthropic/claude-sonnet-5"
export LQABR_EMAIL_TEMPERATURE="1.0"

# ── HubSpot property names (owned by the HubSpot schema) ─────
# Confirm both against GET /crm/v3/properties/contacts before a real run.
# The selection property picks WHICH leads a campaign works; if the name is
# wrong the run now fails loudly rather than emailing a different audience.
export LQABR_HUBSPOT_OBJECT_ID_PROPERTY="object_id"
# Set when lqabr_email_status reaches OPENED — the step-10 hand-off to voice.
export LQABR_HUBSPOT_CAMPAIGN_COMPLETE_PROPERTY="email_campaign_complete"

# ── Non-secret runtime config passed to services ─────────────
export MAILGUN_DOMAIN="dev.reply.tekninjas.com"  # CHANGE ME (dev sending domain)
export MAILGUN_FROM="LQABR Dev Outreach <outreach@${MAILGUN_DOMAIN}>"
export TWILIO_FROM_NUMBER="+15551234567"         # CHANGE ME (dev number)
export LQABR_SENDER_NAME="Your Name"             # CHANGE ME
export LQABR_CTA_URL="https://dev.yourdomain.com/overview"
# Optional fixed Zoom booking page (else the Scheduler API is queried):
export ZOOM_BOOKING_URL=""

echo "config[DEV]: project=${PROJECT_ID} region=${REGION} sa=${AGENT_SA}"
