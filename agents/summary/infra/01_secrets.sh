#!/usr/bin/env bash
# Grant this service's runtime SA read access to the model key.
# The agent resolves it with LQABR_SUMMARY_SECRETS_SOURCE=secret_manager, so
# the value never appears in a deploy command, a log line or the image.
set -euo pipefail
source "$(dirname "$0")/config.sh"

gcloud secrets describe "${MODEL_SECRET}" --project "${PROJECT_ID}" >/dev/null 2>&1 || {
  echo "creating secret ${MODEL_SECRET} (paste the key, then Ctrl-D)"
  gcloud secrets create "${MODEL_SECRET}" --replication-policy=automatic --project "${PROJECT_ID}"
  gcloud secrets versions add "${MODEL_SECRET}" --data-file=- --project "${PROJECT_ID}"
}

gcloud secrets add-iam-policy-binding "${MODEL_SECRET}" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" \
  --project "${PROJECT_ID}"

echo "ok: ${RUNTIME_SA} can read ${MODEL_SECRET}"
