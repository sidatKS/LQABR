#!/usr/bin/env bash
# 02 — create every LQABR service secret in Secret Manager. Idempotent:
# existing secrets are kept; you're prompted only for missing values.
#
# API keys / certificates for MailGun, Twilio, HubSpot, ZoomInfo, and Zoom
# live ONLY here — never in code, configs, or .env files that get committed.
set -euo pipefail
: "${PROJECT_ID:?source ./config.sh first}"

for secret in "${LQABR_SECRETS[@]}"; do
  if gcloud secrets describe "${secret}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    echo "02: ${secret} exists — skipping (add a new version manually to rotate)"
    continue
  fi
  gcloud secrets create "${secret}" \
    --replication-policy automatic --project "${PROJECT_ID}"
  read -r -s -p "Value for ${secret} (input hidden, enter to leave empty): " value
  echo
  if [[ -n "${value}" ]]; then
    printf '%s' "${value}" | gcloud secrets versions add "${secret}" \
      --data-file=- --project "${PROJECT_ID}"
  else
    echo "02: ${secret} created with NO version — add one before deploying:"
    echo "    printf '%s' 'THE-VALUE' | gcloud secrets versions add ${secret} --data-file=- --project ${PROJECT_ID}"
  fi
done

echo "02: Secret Manager provisioning complete (${#LQABR_SECRETS[@]} secrets)."
