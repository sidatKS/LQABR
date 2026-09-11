#!/usr/bin/env bash
# smoke_research.sh — fire ONE real campaign at the deployed research agent
# through Cloud Scheduler, and watch what it did.
#
#   source ./config.sh
#   BLOG_OBJECT_ID=330008697562 bash smoke_research.sh
#
# WHY SCHEDULER AND NOT CURL
# lqabr-dev-research is deployed with `--ingress internal`. A request from the
# public internet is rejected before authentication is even considered, so a
# curl from a laptop cannot reach it no matter what token it carries. Cloud
# Scheduler in the SAME PROJECT is on the allowed-sources list for an internal
# service at its run.app URL, so it can — with no VPC connector and no Direct
# VPC egress to set up.
#
# WHAT THIS ACTUALLY DOES
# It posts the gateway's real A2A envelope at /research/campaign/a2a. That is
# not a health check: the agent reads the post, lists every lead in its
# industry, researches each one and WRITES lead_context to HubSpot — which
# raises contact.propertyChange and hands those contacts to the Email agent.
# `limit: 1` below bounds it to a single lead for exactly that reason.
set -euo pipefail
: "${PROJECT_ID:?source ./config.sh first}"
: "${REGION:?source ./config.sh first}"
: "${AGENT_SA:?source ./config.sh first}"
: "${BLOG_OBJECT_ID:?set BLOG_OBJECT_ID to the blog POST ticket id to campaign on}"

SERVICE="${RESEARCH_SERVICE:-lqabr-dev-research}"
JOB="${SMOKE_JOB:-lqabr-dev-research-smoke}"
ROUTE="${RESEARCH_ROUTE:-/research/campaign/a2a}"
# How many leads in that industry this run is allowed to touch. The gateway
# cannot send `limit` (it is absent from its ALLOWED_METADATA_KEYS and an
# unlisted key makes the dispatch raise) — but Scheduler posts straight at the
# agent, so here it IS honoured, bounded 1..1000 by A2AEnvelope._int.
LIMIT="${SMOKE_LIMIT:-1}"
# One id every smoke run shares, so `runId=smoke-scheduler` finds all of them
# in Cloud Logging and nothing else.
RUN_ID="${SMOKE_RUN_ID:-smoke-scheduler}"

URL="$(gcloud run services describe "${SERVICE}" \
  --region "${REGION}" --project "${PROJECT_ID}" --format 'value(status.url)')"
[[ -n "${URL}" ]] || { echo "could not resolve a URL for ${SERVICE}" >&2; exit 1; }

cat <<BANNER
== research smoke
   service   ${SERVICE}  (${URL})
   route     ${ROUTE}
   post      ${BLOG_OBJECT_ID}
   limit     ${LIMIT} lead(s) — a REAL campaign, real HubSpot writes, unless the
             revision sets LQABR_RESEARCH_DRY_RUN=1
   run_id    ${RUN_ID}
BANNER

# The runtime SA must be allowed to invoke the service; the job authenticates
# AS that SA. Idempotent.
gcloud run services add-iam-policy-binding "${SERVICE}" \
  --region "${REGION}" --project "${PROJECT_ID}" \
  --member "serviceAccount:${AGENT_SA}" --role roles/run.invoker --quiet >/dev/null

# The envelope is the gateway's, verbatim in shape — JSON-RPC outside,
# HubSpot's own event camelCase inside `params.metadata`. `subscriptionType`
# is what makes the route accept it: without it the record kind is unknown,
# and a contact event would be refused at the door instead of researched.
BODY="$(cat <<JSON
{"jsonrpc":"2.0","id":"${RUN_ID}","method":"message/send",
 "params":{"metadata":{"objectId":"${BLOG_OBJECT_ID}",
                       "subscriptionType":"ticket.propertyChange",
                       "propertyName":"blog_summary",
                       "limit":"${LIMIT}",
                       "runId":"${RUN_ID}"}}}
JSON
)"

# Replace rather than update: a stale message-body on an existing job is the
# kind of thing that silently campaigns the wrong post.
gcloud scheduler jobs describe "${JOB}" --location "${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1 && \
gcloud scheduler jobs delete "${JOB}" --location "${REGION}" --project "${PROJECT_ID}" --quiet

# 03:00 on 1 January: far enough away that this never fires on its own. It is
# driven by `jobs run` below — the schedule is required by the API, not wanted.
gcloud scheduler jobs create http "${JOB}" \
  --location "${REGION}" --project "${PROJECT_ID}" \
  --schedule "0 3 1 1 *" --time-zone "Etc/UTC" \
  --uri "${URL}${ROUTE}" \
  --http-method POST \
  --headers "Content-Type=application/json" \
  --message-body "${BODY}" \
  --oidc-service-account-email "${AGENT_SA}" \
  --oidc-token-audience "${URL}" \
  --attempt-deadline 60s \
  --max-retry-attempts 0

# --oidc-token-audience is set EXPLICITLY. Left unset, gcloud uses the full
# --uri as the audience, path included; Cloud Run validates the audience
# against the service URL, so the path is the difference between a 200 and a
# 401 nobody can explain.
#
# --max-retry-attempts 0: a campaign that already started must not be started
# again because the ACK was slow. The route answers inside ~5s and does the
# work in the background, so a retry is a SECOND campaign, not a repair.

echo "== firing"
gcloud scheduler jobs run "${JOB}" --location "${REGION}" --project "${PROJECT_ID}"

cat <<NEXT

== fired. The ACK is immediate; the campaign runs in the background.

Watch it:
  gcloud logging read \
    'resource.type="cloud_run_revision"
     resource.labels.service_name="${SERVICE}"
     jsonPayload.run_id="${RUN_ID}"' \
    --project "${PROJECT_ID}" --limit 100 --freshness 30m \
    --format 'value(jsonPayload.event, jsonPayload.status, jsonPayload.reason)'

What good looks like, in order:
  http_in -> read_blog -> list_leads -> read_lead -> research -> write_context
  -> campaign_complete  status=completed  written=${LIMIT}

If it stops at http_in with status=400, read the reason: the route names what
it refused and why. A 401/403 in Scheduler's own log instead means the OIDC
audience or the run.invoker binding, not the agent.

Delete the job when you are done:
  gcloud scheduler jobs delete ${JOB} --location ${REGION} --project ${PROJECT_ID}
NEXT
