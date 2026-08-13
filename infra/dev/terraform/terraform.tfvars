# DEV environment values (project ldqfingsrv-dev).
project_id    = "ldqfingsrv-dev"
region        = "us-central1"
agent_sa_name = "lqabr-agent-dev"
dev_group     = "ai2d@aidefinitive.com"
ar_repo       = "lqabr"

# Secret CONTAINERS (values added out-of-band). Active + parked (Zoom/ZoomInfo).
secrets = [
  "lqabr-anthropic-api-key",
  "lqabr-hubspot-access-token",
  "lqabr-hubspot-signing-secret",
  "lqabr-mailgun-api-key",
  "lqabr-mailgun-webhook-signing-key",
  "lqabr-vapi-api-key",
  "lqabr-vapi-webhook-secret",
  "lqabr-zoominfo-username",
  "lqabr-zoominfo-password",
  "lqabr-zoom-account-id",
  "lqabr-zoom-client-id",
  "lqabr-zoom-client-secret",
  "lqabr-zoom-webhook-secret-token",
]
