#!/usr/bin/env bash
# 02_upload_to_gcs.sh — upload source CSVs from data/seeds/b2b/ to their GCS prefixes.
# Files stay in GCS as the durable source of truth; no BQ ingestion happens here.
# gsutil cp -n skips files that already exist (idempotent).
set -euo pipefail
source ./config.sh

BASE="gs://${BUCKET_NAME}/${GCS_RAW_PREFIX}"

upload() { # $1=local_file  $2=gcs_folder
  local src="${DATA_DIR}/$1"
  local dst="${BASE}/$2/$1"
  if [[ ! -f "${src}" ]]; then
    echo "WARNING: ${src} not found — skipping"
    return
  fi
  gsutil cp -n "${src}" "${dst}"
  echo "uploaded: ${dst}"
}

# Company data
upload companies_clean_734.csv    companies
upload companies_noisy_734.csv    companies

# Employee / contact data
upload employees_clean_5234.csv   employees
upload employees_noisy_5234.csv   employees
upload employee_contacts_5234.csv contacts

echo "Upload complete. Verify with:"
echo "  gsutil ls -r gs://${BUCKET_NAME}/${GCS_RAW_PREFIX}/"
