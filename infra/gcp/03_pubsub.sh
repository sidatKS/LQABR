#!/usr/bin/env bash
# 03 — Pub/Sub topics + subscriptions for pipeline events. Idempotent.
#
#   lqabr-ingestion-trigger : scheduled/manual ingestion kicks (source=csv|zoominfo)
#   lqabr-engagement-events : fan-out copy of engagement events for future
#                             analytics/eval consumers (HubSpot remains the
#                             system of record)
set -euo pipefail
: "${PROJECT_ID:?source ./config.sh first}"

for topic in "${TOPIC_INGESTION}" "${TOPIC_ENGAGEMENT}"; do
  gcloud pubsub topics describe "${topic}" --project "${PROJECT_ID}" >/dev/null 2>&1 || \
  gcloud pubsub topics create "${topic}" --project "${PROJECT_ID}"
done

gcloud pubsub subscriptions describe "${TOPIC_ENGAGEMENT}-pull" --project "${PROJECT_ID}" >/dev/null 2>&1 || \
gcloud pubsub subscriptions create "${TOPIC_ENGAGEMENT}-pull" \
  --topic "${TOPIC_ENGAGEMENT}" --ack-deadline 60 --project "${PROJECT_ID}"

echo "03: Pub/Sub topics ready: ${TOPIC_INGESTION}, ${TOPIC_ENGAGEMENT}."
