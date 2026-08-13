#!/usr/bin/env bash
# 02 — create every LQABR service secret in Secret Manager. Idempotent:
# existing secrets are kept; you're prompted only for missing ACTIVE values.
# Parked secrets are created as empty containers (populate later).
# Self-contained: reads this environment's values from ./config.sh.
set -euo pipefail
: "${PROJECT_ID:?source ./config.sh first}"

provision_secret() {  # <name> <prompt_for_value: yes|no>
  local secret="$1" prompt="$2"
  if gcloud secrets describe "${secret}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    echo "02: ${secret} exists — skipping (add a new version manually to rotate)"
    return
  fi
  gcloud secrets create "${secret}" \
    --replication-policy automatic --project "${PROJECT_ID}"
  if [[ "${prompt}" != "yes" ]]; then
    echo "02: ${secret} created PARKED (no version) — populate when credentials are available."
    return
  fi
  read -r -s -p "Value for ${secret} (input hidden, enter to leave empty): " value
  echo
  if [[ -n "${value}" ]]; then
    printf '%s' "${value}" | gcloud secrets versions add "${secret}" \
      --data-file=- --project "${PROJECT_ID}"
  else
    echo "02: ${secret} created with NO version — add one before deploying:"
    echo "    printf '%s' 'THE-VALUE' | gcloud secrets versions add ${secret} --data-file=- --project ${PROJECT_ID}"
  fi
}

for secret in "${LQABR_SECRETS[@]}"; do
  provision_secret "${secret}" yes
done
for secret in "${LQABR_SECRETS_PARKED[@]:-}"; do
  [[ -n "${secret}" ]] && provision_secret "${secret}" no
done

echo "02: Secret Manager provisioning complete (${#LQABR_SECRETS[@]} active + ${#LQABR_SECRETS_PARKED[@]:-0} parked) in ${PROJECT_ID}."
