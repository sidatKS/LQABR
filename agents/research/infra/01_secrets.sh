#!/usr/bin/env bash
# Grant the runtime SA read access to the two secrets research needs.
#
# Research resolves secrets with LQABR_RESEARCH_SECRETS_SOURCE=env, NOT
# secret_manager: requirements.txt does not include google-cloud-secret-manager,
# so the secret_manager path raises at import. --set-secrets injects the values
# under the names secrets.py:_env_name derives (uppercase, hyphens to
# underscores), so the SA still gates access and no token enters the image.
set -euo pipefail
source "$(dirname "$0")/config.sh"

for SECRET in "${MODEL_SECRET:-lqabr-anthropic-api-key}" "${HUBSPOT_SECRET:-lqabr-hubspot-access-token}"; do
  if ! gcloud secrets describe "${SECRET}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    echo "FATAL: secret ${SECRET} does not exist - create it before deploying." >&2
    exit 1
  fi
  if ! gcloud secrets versions describe latest --secret "${SECRET}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
    # The live failure mode seen on the gateway 2026-08-29: a secret with zero
    # versions binds fine at deploy time and injects NOTHING at runtime.
    echo "FATAL: ${SECRET} exists but has NO VERSION - versions/latest will not resolve." >&2
    exit 1
  fi
  gcloud secrets add-iam-policy-binding "${SECRET}" \
    --member="serviceAccount:${RUNTIME_SA}" \
    --role="roles/secretmanager.secretAccessor" \
    --project "${PROJECT_ID}" >/dev/null
  echo "ok: ${RUNTIME_SA} can read ${SECRET}"
done
