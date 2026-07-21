#!/usr/bin/env bash
# 00 — enable the GCP APIs the LQABR platform needs. Idempotent.
set -euo pipefail
: "${PROJECT_ID:?source ./config.sh first}"

gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  pubsub.googleapis.com \
  cloudscheduler.googleapis.com \
  aiplatform.googleapis.com \
  iam.googleapis.com \
  --project "${PROJECT_ID}"

# Artifact Registry repo for agent images (safe to re-run).
gcloud artifacts repositories describe "${AR_REPO}" \
  --location "${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1 || \
gcloud artifacts repositories create "${AR_REPO}" \
  --repository-format=docker --location "${REGION}" --project "${PROJECT_ID}" \
  --description "LQABR agent and webhook images"

echo "00: APIs enabled, Artifact Registry repo '${AR_REPO}' ready."
