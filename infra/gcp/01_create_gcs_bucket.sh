#!/usr/bin/env bash
# 01_create_gcs_bucket.sh — create the GCS data lake bucket with structured
# folder prefixes. This replaces the raw BQ dataset in the old architecture.
# Files in GCS are the authoritative source; BigLake external tables read them
# directly without ingestion. Idempotent — safe to re-run.
set -euo pipefail
source ./config.sh

# 1. create bucket (single-region or multi-region to match BQ REGION)
if gsutil ls -b "gs://${BUCKET_NAME}" >/dev/null 2>&1; then
  echo "exists: gs://${BUCKET_NAME}"
else
  gsutil mb -p "${PROJECT_ID}" -l "${BUCKET_LOCATION}" "gs://${BUCKET_NAME}"
  echo "created: gs://${BUCKET_NAME}"
fi

# 2. uniform bucket-level access (required for BigLake column/row policies to work)
gsutil uniformbucketlevelaccess set on "gs://${BUCKET_NAME}"

# 3. touch placeholder objects to materialise the folder structure
#    BigLake globs the prefix, so the folders must exist before external tables are created
for prefix in \
  "${GCS_RAW_PREFIX}/companies/" \
  "${GCS_RAW_PREFIX}/employees/" \
  "${GCS_RAW_PREFIX}/contacts/"; do
  echo "" | gsutil cp - "gs://${BUCKET_NAME}/${prefix}.keep" 2>/dev/null || true
  echo "folder ready: gs://${BUCKET_NAME}/${prefix}"
done

echo "GCS bucket ready: gs://${BUCKET_NAME}"
