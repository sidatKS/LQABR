#!/usr/bin/env bash
# 09_iam_rbac.sh — three personas, least privilege (BigLake edition).
#
#   data-eng    -> BQ WRITER on curated + sandbox; GCS object-viewer on bucket
#   app-dev     -> BQ READER on sandbox only (masked views); no curated access
#   agent SA    -> BQ READER on curated + sandbox; GCS object-viewer (to scan files via BigLake)
#
# BigLake-specific additions vs. old 06_iam_rbac.sh:
#   - Agent SA and data-eng need storage.objectViewer on the GCS bucket
#   - All query principals need bigquery.connectionUser (granted in 03_create_biglake_connection.sh)
# Idempotent — binding already present is a no-op.
set -euo pipefail
source ./config.sh

# 1. agent runtime service account (idempotent)
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" \
    --project="${PROJECT_ID}" --display-name="Lead-qual agent runtime"
  echo "created SA: ${SA_EMAIL}"
else
  echo "exists SA: ${SA_EMAIL}"
fi

# 2. project-level: ability to run BQ jobs (not data access)
for m in "${GROUP_DATAENG}" "${GROUP_APPDEV}" "serviceAccount:${SA_EMAIL}"; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="${m}" --role="roles/bigquery.jobUser" --condition=None --quiet
done
echo "bigquery.jobUser granted to all principals"

# 3. GCS bucket read — data-eng and agent SA need to read the raw files via BigLake
for m in "${GROUP_DATAENG}" "serviceAccount:${SA_EMAIL}"; do
  gsutil iam ch "${m}:roles/storage.objectViewer" "gs://${BUCKET_NAME}"
done
echo "storage.objectViewer on gs://${BUCKET_NAME} granted"

# 4. dataset-level data access (least privilege)
python3 apply_dataset_iam.py

echo "RBAC applied."
echo ""
echo "Summary:"
echo "  ${GROUP_DATAENG}    — BQ write (curated), GCS read (bucket), BQ job runner"
echo "  ${GROUP_APPDEV}     — BQ read (sandbox masked views only), BQ job runner"
echo "  serviceAccount:${SA_EMAIL} — BQ read (curated + sandbox), GCS read, BQ job runner"
