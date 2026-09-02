#!/usr/bin/env bash
# Ensure the HubSpot private-app CLIENT SECRET exists and the runtime SA can read it.
#
# This is NOT lqabr-hubspot-access-token. That is the CRM access token agents use.
# THIS is the client secret from the private app's Auth tab, and it is the key the
# v3 webhook signature is computed with. Using the wrong one 401s every webhook.
set -euo pipefail
source "$(dirname "$0")/config.sh"

if ! gcloud secrets describe "${APP_SECRET_NAME}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo ">> creating ${APP_SECRET_NAME} (paste the CLIENT SECRET, then Ctrl-D)"
  gcloud secrets create "${APP_SECRET_NAME}" --replication-policy=automatic --project "${PROJECT_ID}"
  gcloud secrets versions add "${APP_SECRET_NAME}" --data-file=- --project "${PROJECT_ID}"
fi

# The live failure mode on 2026-08-29: the secret existed with ZERO versions, so
# versions/latest did not resolve. Cloud Run then marks the service Ready=False,
# the env var is never injected, and the gateway 401s every webhook while looking
# otherwise healthy. Catch it here rather than from a lead that never got contacted.
if ! gcloud secrets versions describe latest --secret "${APP_SECRET_NAME}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  echo ">> ${APP_SECRET_NAME} exists but has NO VERSION."
  echo ">> paste the private app CLIENT SECRET, then Ctrl-D"
  gcloud secrets versions add "${APP_SECRET_NAME}" --data-file=- --project "${PROJECT_ID}"
fi

gcloud secrets add-iam-policy-binding "${APP_SECRET_NAME}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project "${PROJECT_ID}"

echo "ok: ${RUNTIME_SA} can read ${APP_SECRET_NAME}"
