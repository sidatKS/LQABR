#!/usr/bin/env bash
# 04_create_datasets.sh — create the three BQ datasets.
# No raw_b2b dataset in the BigLake architecture — raw files live in GCS.
# External tables and curated models share DS_CURATED; sandbox holds masked views.
# Idempotent — existing datasets are skipped.
set -euo pipefail
source ./config.sh

for ds in "${DS_CLEANSED}" "${DS_CURATED}" "${DS_SANDBOX}"; do
  if bq --location="${REGION}" show --dataset "${PROJECT_ID}:${ds}" >/dev/null 2>&1; then
    echo "exists: ${ds}"
  else
    bq --location="${REGION}" mk --dataset \
      --description "Lead-qual ${ds} layer" \
      "${PROJECT_ID}:${ds}"
    echo "created: ${ds}"
  fi
done

echo "BQ datasets ready: ${DS_CLEANSED}, ${DS_CURATED}, ${DS_SANDBOX}"
