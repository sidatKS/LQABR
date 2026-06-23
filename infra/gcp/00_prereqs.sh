#!/usr/bin/env bash
# 00_prereqs.sh — enable required APIs. Run once per project.
set -euo pipefail
source ./config.sh

gcloud config set project "${PROJECT_ID}"

gcloud services enable \
  bigquery.googleapis.com \
  bigquerydatapolicy.googleapis.com \
  bigqueryconnection.googleapis.com \
  datacatalog.googleapis.com \
  dataplex.googleapis.com \
  storage.googleapis.com \
  iam.googleapis.com

echo "APIs enabled."
