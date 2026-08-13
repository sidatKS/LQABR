#!/usr/bin/env bash
# 00 — enable the GCP APIs the LQABR platform needs. Idempotent.
# Self-contained: reads this environment's values from ./config.sh.
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
  iamcredentials.googleapis.com \
  cloudidentity.googleapis.com \
  --project "${PROJECT_ID}"
# iamcredentials — mint OIDC ID tokens (Gateway → internal agents, agent → MCP).
# cloudidentity  — query the developer group membership.

# Artifact Registry repo for service images (CI pushes here; safe to re-run).
gcloud artifacts repositories describe "${AR_REPO}" \
  --location "${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1 || \
gcloud artifacts repositories create "${AR_REPO}" \
  --repository-format=docker --location "${REGION}" --project "${PROJECT_ID}" \
  --description "LQABR gateway, MCP and agent images"

echo "00: APIs enabled, Artifact Registry repo '${AR_REPO}' ready (${PROJECT_ID})."
