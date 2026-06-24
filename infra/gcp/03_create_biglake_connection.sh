#!/usr/bin/env bash
# 03_create_biglake_connection.sh — create a BigLake connection resource that
# bridges BQ external tables to GCS. The connection gets its own managed service
# account; we grant that SA read access to the bucket so BQ can scan the files.
# Idempotent — connection creation is skipped if it already exists.
set -euo pipefail
source ./config.sh

FULL_CONN="${PROJECT_ID}.${BIGLAKE_REGION}.${BIGLAKE_CONNECTION}"

# 1. create the connection (no-op if present)
if bq show --connection "${FULL_CONN}" >/dev/null 2>&1; then
  echo "exists: ${FULL_CONN}"
else
  bq mk --connection \
    --connection_type=CLOUD_RESOURCE \
    --project_id="${PROJECT_ID}" \
    --location="${BIGLAKE_REGION}" \
    "${BIGLAKE_CONNECTION}"
  echo "created: ${FULL_CONN}"
fi

# 2. extract the connection's managed service account
CONN_SA=$(bq show --connection --format=json "${FULL_CONN}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['cloudResource']['serviceAccountId'])")
echo "connection SA: ${CONN_SA}"

# 3. grant the connection SA object-viewer on our bucket
gsutil iam ch "serviceAccount:${CONN_SA}:roles/storage.objectViewer" \
  "gs://${BUCKET_NAME}"
echo "granted storage.objectViewer on gs://${BUCKET_NAME} to ${CONN_SA}"

# 4. also grant bigquery.connectionUser to the principals who will run queries
#    (they need this in addition to dataset-level access)
for m in "${GROUP_DATAENG}" "${GROUP_APPDEV}" "serviceAccount:${SA_EMAIL}"; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="${m}" \
    --role="roles/bigquery.connectionUser" \
    --condition=None --quiet
done

echo "BigLake connection ready: ${FULL_CONN}"
echo "Paste this connection ID into 05_create_external_tables.sh if prompted."
