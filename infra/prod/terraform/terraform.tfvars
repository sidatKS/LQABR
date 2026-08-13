# PROD environment values.
project_id    = "ldqfingsrv-prod"          # CHANGE ME (prod project id)
region        = "us-central1"
agent_sa_name = "lqabr-agent-runtime"
dev_group     = "ai2d@aidefinitive.com"    # CHANGE ME if prod uses a distinct group
ar_repo       = "lqabr"

# Secret CONTAINERS (values added out-of-band). Prod uses lqabr-google-api-key.
secrets = [
  "lqabr-google-api-key",
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
