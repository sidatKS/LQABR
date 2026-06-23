#!/usr/bin/env bash
# 07_policy_tags.sh — create a PII policy-tag taxonomy, grant fine-grained
# read only to approved viewers, and attach the tag to Email/Phone/Name columns.
# Result: anyone WITHOUT the pii-approved role cannot SELECT those columns
# from the curated tables. App-devs instead use the masked sandbox views (step 07).
set -euo pipefail
source ./config.sh

LOC="${TAXONOMY_REGION}"

# 1. taxonomy (idempotent: reuse if present)
TAX_ID=$(gcloud data-catalog taxonomies list --location="${LOC}" \
  --project="${PROJECT_ID}" --filter="displayName=${TAXONOMY_NAME}" \
  --format="value(name)" | head -1 || true)
if [[ -z "${TAX_ID}" ]]; then
  gcloud data-catalog taxonomies create --location="${LOC}" \
    --project="${PROJECT_ID}" --display-name="${TAXONOMY_NAME}"
  TAX_ID=$(gcloud data-catalog taxonomies list --location="${LOC}" \
    --project="${PROJECT_ID}" --filter="displayName=${TAXONOMY_NAME}" \
    --format="value(name)" | head -1)
fi
echo "taxonomy: ${TAX_ID}"

# 2. policy tag
PT_NAME=$(gcloud data-catalog taxonomies policy-tags list --location="${LOC}" \
  --taxonomy="${TAX_ID}" --format="value(name)" \
  --filter="displayName=${POLICY_TAG_PII}" | head -1 || true)
if [[ -z "${PT_NAME}" ]]; then
  gcloud data-catalog taxonomies policy-tags create --location="${LOC}" \
    --taxonomy="${TAX_ID}" --display-name="${POLICY_TAG_PII}"
  PT_NAME=$(gcloud data-catalog taxonomies policy-tags list --location="${LOC}" \
    --taxonomy="${TAX_ID}" --format="value(name)" \
    --filter="displayName=${POLICY_TAG_PII}" | head -1)
fi
echo "policy tag: ${PT_NAME}"

# 3. only approved viewers may read tagged columns
gcloud data-catalog taxonomies add-iam-policy-binding "${TAX_ID}" \
  --location="${LOC}" --project="${PROJECT_ID}" \
  --member="${GROUP_PII_VIEWERS}" \
  --role="roles/datacatalog.categoryFineGrainedReader"

# 4. attach the tag to PII columns in the curated tables
python3 apply_policy_tags.py "${PROJECT_ID}" "${DS_CURATED}" "${PT_NAME}"

echo "PII policy tags applied."
