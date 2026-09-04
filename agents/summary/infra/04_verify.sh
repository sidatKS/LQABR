#!/usr/bin/env bash
# Verify a deployed lqabr-dev-summary, end to end, in one run.
#
# Specification: docs/summary_verify_spec.md. Three layers:
#   A  control plane  - what Cloud Run says the service IS      (gcloud, host)
#   B  startup        - what the container said as it came up   (logs, host)
#   C  data plane     - what it actually DOES                   (in-VPC job)
#
# Layer C needs a Cloud Run job because the service is ingress=internal: a
# laptop curl returns 404 even with a valid ID token. That is the control
# working, not a fault.
#
# Usage:
#   bash infra/04_verify.sh                 # full, includes one real HubSpot write
#   RUN_E2E=0 bash infra/04_verify.sh       # A+B and the read-only C1/C2 only
#   BLOG_URL=... PUBLISHED_AT=... bash infra/04_verify.sh
set -uo pipefail
cd "$(dirname "$0")/.."
source infra/config.sh

JOB="summary-verify"
RUNTIME_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/lqabr-dev-mcp:0.1.0"
MCP_SERVICE="${MCP_SERVICE:-lqabr-dev-mcp}"
BLOG_URL="${BLOG_URL:-https://aidefinitive.com/hipaa-rag-claude}"
# FULL ISO 8601. _iso_published_at passes through ANY value containing "T", so a
# month-first form like 08:28:2026T20:12:00 reaches HubSpot malformed and silently
# no-ops. See summary_deploy_spec 5.1. This is 2026-08-28 20:12:00 UTC.
PUBLISHED_AT="${PUBLISHED_AT:-2026-08-28T20:12:00.000Z}"
RUN_E2E="${RUN_E2E:-1}"

FAILED=0
# pass <id> <what it asserts> [detail]
# fail <id> <what it asserts> <what was seen> <what to do>
pass() { printf 'PASS %-22s %s\n' "$1" "$2"; [[ -n "${3:-}" ]] && printf '          %s\n' "$3"; return 0; }
fail() { printf 'FAIL %-22s %s\n' "$1" "$2"; [[ -n "${3:-}" ]] && printf '          got: %s\n' "$3"
         [[ -n "${4:-}" ]] && printf '          FIX: %s\n' "$4"; FAILED=$((FAILED+1)); return 0; }
rule() { echo; echo "======================================================================"; \
         echo "$1"; echo "======================================================================"; }

# ===================================================== LAYER A - control plane
cat <<'LEGEND'
Spec: docs/summary_verify_spec.md   Every check prints what it asserts; a FAIL
also prints what was seen and what to do. Layers: A control plane (gcloud) /
B startup events (logs) / C data plane (in-VPC job).
LEGEND
rule "LAYER A - control plane (what Cloud Run says the service IS)"
SVC_JSON="$(gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" \
             --project="${PROJECT_ID}" --format=json 2>/dev/null)" || SVC_JSON=""
if [[ -z "${SVC_JSON}" ]]; then
  fail A1 "service ${SERVICE_NAME} not found in ${REGION}/${PROJECT_ID} - nothing else can be checked"
  exit 1
fi
echo "${SVC_JSON}" > /tmp/summary_svc.json
pass A1 "service ${SERVICE_NAME} exists"

# A2-A8 are one python pass over the describe output: shelling out per field
# invites a typo'd --format silently returning empty and reading as a pass.
python3 - "$@" <<'PYA'
import json, sys
svc = json.load(open("/tmp/summary_svc.json"))
meta, spec, status = svc["metadata"], svc["spec"]["template"], svc["status"]
ann_svc = meta.get("annotations", {})
ann_rev = spec["metadata"].get("annotations", {})
tspec = spec["spec"]
c = tspec["containers"][0]
env = {e["name"]: e.get("value", "<secretRef>") for e in c.get("env", [])}
# id -> (what it asserts, what to do when it fails). Mirrors
# summary_verify_spec 6.1 and 7 so this output needs no companion document.
CHECKS = {
 "A2": ("latest revision is Ready",
        "the container never became healthy. A bare 'failed to start and listen on PORT=8080' "
        "was once an IndexError at IMPORT - read the revision's stderr, not Cloud Run's message"),
 "A3": ("one revision serves 100% of traffic",
        "a traffic split left an old revision live; re-run 03_deploy_run.sh"),
 "A4": ("ingress is internal, so the agent is not internet-reachable",
        "deployed without 03_deploy_run.sh; redeploy with it"),
 "A5": ("runs as lqabr-agent-dev, not the default compute SA",
        "--service-account was omitted; the default compute SA holds no roles"),
 "A6": ("is ON the VPC (network+subnet+all-traffic egress)",
        "WITHOUT THIS IT CANNOT REACH THE ingress=internal MCP AT ALL; redeploy with 03_deploy_run.sh"),
 "A7": ("a writable volume is mounted at /app/logs",
        "file logging errors on a read-only path; add --add-volume/--add-volume-mount"),
 "A8": ("deployed env matches the spec exactly",
        "SECRETS_SOURCE must be secret_manager (summary ships google-cloud-secret-manager); "
        "DRY_RUN=1 suppresses writes; summary_core's default tool names do not exist on this MCP"),
}
fails = []
def chk(cid, cond, detail):
    meaning, fix = CHECKS.get(cid, ("", ""))
    print(("PASS " if cond else "FAIL ") + "{0:<4} {1}".format(cid, meaning))
    print("          " + ("" if cond else "got: ") + detail)
    if not cond:
        fails.append(cid)
        print("          FIX: " + fix)

ready = [x for x in status.get("conditions", []) if x["type"] == "Ready"]
chk("A2", bool(ready) and ready[0]["status"] == "True",
    "Ready={0} revision={1}".format(ready[0]["status"] if ready else "?",
                                    status.get("latestReadyRevisionName")))
traffic = status.get("traffic", [])
chk("A3", any(t.get("percent") == 100 for t in traffic),
    "traffic {0}".format([(t.get("revisionName"), t.get("percent")) for t in traffic]))
chk("A4", ann_svc.get("run.googleapis.com/ingress") == "internal",
    "ingress={0}".format(ann_svc.get("run.googleapis.com/ingress")))
chk("A5", tspec.get("serviceAccountName", "").startswith("lqabr-agent-dev@"),
    "serviceAccount={0}".format(tspec.get("serviceAccountName")))
chk("A6", "network-interfaces" in json.dumps(ann_rev)
          and ann_rev.get("run.googleapis.com/vpc-access-egress") == "all-traffic",
    "vpc={0} egress={1}".format(ann_rev.get("run.googleapis.com/network-interfaces"),
                                ann_rev.get("run.googleapis.com/vpc-access-egress")))
mounts = [m.get("mountPath") for m in c.get("volumeMounts", [])]
chk("A7", "/app/logs" in mounts, "volumeMounts={0}".format(mounts))

expected = {
    "LQABR_SUMMARY_MCP_TOOL_READ": "get_blog_summary",
    "LQABR_SUMMARY_MCP_TOOL_WRITE": "upsert_blog_summary",
    "LQABR_SUMMARY_MCP_WRITE_STYLE": "blog_summary",
    "LQABR_SUMMARY_SECRETS_SOURCE": "secret_manager",
    "LQABR_SUMMARY_HUBSPOT_OBJECT_TYPE": "ticket",
    "LQABR_SUMMARY_DRY_RUN": "0",
}
wrong = {k: (env.get(k), v) for k, v in expected.items() if env.get(k) != v}
chk("A8", not wrong, "env mismatches={0}".format(wrong or "none"))
print("A8-detail image:", c.get("image"))
sys.exit(len(fails))
PYA
[[ $? -eq 0 ]] || FAILED=$((FAILED+1))

# ========================================================== LAYER B - startup
rule "LAYER B - startup events for the serving revision"
REV="$(python3 -c "import json;print(json.load(open('/tmp/summary_svc.json'))['status']['latestReadyRevisionName'])")"
echo "revision: ${REV}"
# timestamp> on the revision, never --freshness: stale entries inside a freshness
# window have twice been mistaken for current results (summary_deploy_spec F10).
# ⚠ jsonPayload.event, NOT textPayload. summary emits STRUCTURED obs events, so
# --format='value(textPayload)' returns empty for every one of them and all four
# assertions below report a false FAIL on a perfectly healthy service. Cost this
# spec four false negatives on its first real run, 2026-08-29.
LOGS="$(gcloud logging read \
  "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE_NAME}\" AND resource.labels.revision_name=\"${REV}\"" \
  --project="${PROJECT_ID}" --limit=400 --order=asc --format='value(jsonPayload.event)' 2>/dev/null)"
declare -A B_MEANS=(
  [service_start]="the app booted"
  [mcp_initialized]="the MCP session handshake succeeded"
  [mcp_tools_discovered]="tools/list returned from the MCP"
  [mcp_startup_check_ok]="the configured tool names EXIST on the live MCP"
)
declare -A B_FIX=(
  [service_start]="the container never reached startup; read the revision's stderr"
  [mcp_initialized]="cannot reach the MCP - almost always A6, the service is off the VPC"
  [mcp_tools_discovered]="MCP reachable but tools/list failed; verify the MCP: bash infra/gcp/mcp/02_probe.sh"
  [mcp_startup_check_ok]="tool names do not match the live MCP - same cause as A8"
)
for EVENT in service_start mcp_initialized mcp_tools_discovered mcp_startup_check_ok; do
  if grep -q "${EVENT}" <<<"${LOGS}"; then
    pass "B-${EVENT}" "${B_MEANS[$EVENT]}"
  else
    fail "B-${EVENT}" "${B_MEANS[$EVENT]}" "event not emitted by ${REV}" "${B_FIX[$EVENT]}"
  fi
done
if grep -q "mcp_startup_check_failed" <<<"${LOGS}"; then
  fail "B-startup_check" "mcp_startup_check_failed is ABSENT" \
       "mcp_startup_check_failed was emitted" \
       "under MCP_STARTUP_CHECK=warn the service starts anyway and fails later at write time - fix the tool names (A8)"
fi

# ======================================================== LAYER C - data plane
rule "LAYER C - data plane (in-VPC Cloud Run job)"
SUMMARY_URL="$(gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" \
                --project="${PROJECT_ID}" --format='value(status.url)')"
MCP_URL="$(gcloud run services describe "${MCP_SERVICE}" --region="${REGION}" \
            --project="${PROJECT_ID}" --format='value(status.url)')"
echo "summary=${SUMMARY_URL}"
echo "mcp    =${MCP_URL}"
echo "blog   =${BLOG_URL}"
echo "key    =${PUBLISHED_AT}   (RUN_E2E=${RUN_E2E})"

CLIENT="$(cat infra/verify_client.py)"
gcloud run jobs delete "${JOB}" --region="${REGION}" --project="${PROJECT_ID}" --quiet 2>/dev/null || true
gcloud run jobs create "${JOB}" \
  --image="${RUNTIME_IMAGE}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --service-account="${RUNTIME_SA}" \
  --network="${VPC_NETWORK}" --subnet="${VPC_SUBNET}" --vpc-egress="${VPC_EGRESS}" \
  --set-env-vars="SUMMARY_URL=${SUMMARY_URL},MCP_URL=${MCP_URL},BLOG_URL=${BLOG_URL},PUBLISHED_AT=${PUBLISHED_AT},RUN_E2E=${RUN_E2E}" \
  --command=python --args="^@^-c@${CLIENT}" \
  --max-retries=0 --task-timeout=900s >/dev/null

# 900s: cold start plus a real model call is 1-3 minutes, and a probe deleted
# 27s in once captured nothing at all (summary_deploy_spec F11).
gcloud run jobs execute "${JOB}" --region="${REGION}" --project="${PROJECT_ID}" --wait || FAILED=$((FAILED+1))

EXEC="$(gcloud run jobs executions list --job="${JOB}" --region="${REGION}" \
        --project="${PROJECT_ID}" --limit=1 --format='value(name)')"
echo; echo "execution: ${EXEC}"; echo
gcloud logging read \
  "resource.type=\"cloud_run_job\" AND labels.\"run.googleapis.com/execution_name\"=\"${EXEC}\"" \
  --project="${PROJECT_ID}" --limit=400 --order=asc --format='value(textPayload)'

gcloud run jobs delete "${JOB}" --region="${REGION}" --project="${PROJECT_ID}" --quiet 2>/dev/null || true

# ==================================================================== verdict
rule "VERDICT"
if [[ ${FAILED} -eq 0 ]]; then
  echo "A and B PASSED. Read the PASS/FAIL lines above for layer C."
else
  echo "${FAILED} host-side check group(s) FAILED - see the FAIL lines above."
fi
cat <<EOF

Authoritative record of what summary sent to the MCP (obs log, not reconstructed):

gcloud logging read \\
  'resource.type="cloud_run_revision" AND resource.labels.service_name="${SERVICE_NAME}"
   AND (textPayload:"hubspot_write_raw_result" OR textPayload:"hubspot_write_dry_run"
        OR textPayload:"hubspot_write_failed" OR textPayload:"hubspot_write_skipped")' \\
  --project=${PROJECT_ID} --limit=50 --order=desc --format='value(textPayload)'
EOF
exit ${FAILED}
