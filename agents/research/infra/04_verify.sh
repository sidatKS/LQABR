#!/usr/bin/env bash
# Verify a deployed lqabr-dev-research. Spec: docs/research_verify_spec.md
#
#   A  control plane   what Cloud Run says the service IS   (gcloud)
#   B  startup         what it said as it booted            (Cloud Logging)
#   C  data plane      what it can reach and do             (in-VPC job)
#
# DEFAULT MODE WRITES NOTHING. A research campaign writes lead_context onto every
# contact in the post's industry, and each write trips the gateway's
# R2-lead-context route into the Email agent. C5/C6 are therefore opt-in:
#   RUN_CAMPAIGN=1 TICKET_ID=<post objectId> bash infra/04_verify.sh
set -uo pipefail
cd "$(dirname "$0")/.."
source infra/config.sh

JOB="research-verify"
RUNTIME_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO:-lqabr}/lqabr-dev-mcp:0.1.0"
RUN_CAMPAIGN="${RUN_CAMPAIGN:-0}"
TICKET_ID="${TICKET_ID:-}"
CAMPAIGN_LIMIT="${CAMPAIGN_LIMIT:-1}"

FAILED=0
pass() { printf 'PASS %-24s %s\n' "$1" "$2"; [[ -n "${3:-}" ]] && printf '          %s\n' "$3"; return 0; }
fail() { printf 'FAIL %-24s %s\n' "$1" "$2"; [[ -n "${3:-}" ]] && printf '          got: %s\n' "$3"
         [[ -n "${4:-}" ]] && printf '          FIX: %s\n' "$4"; FAILED=$((FAILED+1)); return 0; }
rule() { echo; echo "======================================================================"
         echo "$1"; echo "======================================================================"; }

cat <<'LEGEND'
Spec: docs/research_verify_spec.md   Every check prints what it asserts; a FAIL
also prints what was seen and what to do. Layers: A control plane / B startup /
C data plane (in-VPC job). Default mode runs NO campaign and writes nothing.
LEGEND

# ===================================================== LAYER A
rule "LAYER A - control plane"
SVC="$(gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" \
        --project="${PROJECT_ID}" --format=json 2>/dev/null)" || SVC=""
if [[ -z "${SVC}" ]]; then
  fail A1 "the service exists in this project and region" \
       "${SERVICE_NAME} not found" "not deployed. Run: bash infra/03_deploy_run.sh"
  exit 1
fi
echo "${SVC}" > /tmp/research_svc.json
pass A1 "the service exists in this project and region" "${SERVICE_NAME}"

MCP_EXPECTED="${LQABR_RESEARCH_MCP_BASE_URL}" python3 - <<'PYA'
import json, os, sys
svc = json.load(open("/tmp/research_svc.json"))
meta, tpl, status = svc["metadata"], svc["spec"]["template"], svc["status"]
ann_s, ann_t = meta.get("annotations", {}), tpl["metadata"].get("annotations", {})
tspec = tpl["spec"]; c = tspec["containers"][0]
env = {e["name"]: e.get("value") for e in c.get("env", [])}
secrets = {e["name"] for e in c.get("env", []) if "valueFrom" in e}

CHECKS = {
 "A2": ("latest revision is Ready", "the message below is the diagnosis; else read the revision's stderr"),
 "A3": ("one revision serves 100% of traffic", "a split left an old revision live"),
 "A4": ("ingress is internal", "otherwise the agent is internet-reachable"),
 "A5": ("runs as lqabr-agent-dev", "--service-account omitted; the default compute SA holds no roles"),
 "A6": ("is ON the VPC with all-traffic egress",
        "WITHOUT THIS IT CANNOT REACH THE ingress=internal MCP AT ALL"),
 "A7": ("a writable volume is mounted at /app/logs",
        "config.yaml logs to logs/agents/research/agent.log, a tree absent from the image"),
 "A8": ("LQABR_RESEARCH_SECRETS_SOURCE is env, NOT secret_manager",
        "secret_manager raises at import: research does not ship google-cloud-secret-manager"),
 "A9": ("both secrets are bound: LQABR_ANTHROPIC_API_KEY and LQABR_HUBSPOT_ACCESS_TOKEN",
        "the env source reads exactly those names (secrets.py:_env_name). Run 01_secrets.sh"),
 "A10": ("LQABR_RESEARCH_DRY_RUN is 0", "writes are suppressed"),
 "A11": ("LQABR_RESEARCH_MCP_BASE_URL points at the live MCP",
         "this was a guessed hostname once, which resolved to nothing"),
}
fails = []
def chk(cid, cond, detail):
    m, f = CHECKS[cid]
    print(("PASS " if cond else "FAIL ") + "{0:<4} {1}".format(cid, m))
    print("          " + ("" if cond else "got: ") + detail)
    if not cond: fails.append(cid); print("          FIX: " + f)

ready = [x for x in status.get("conditions", []) if x["type"] == "Ready"]
chk("A2", bool(ready) and ready[0]["status"] == "True",
    "Ready={0} revision={1} {2}".format(ready[0]["status"] if ready else "?",
        status.get("latestReadyRevisionName"), (ready[0].get("message") or "") if ready else ""))
tr = status.get("traffic", [])
chk("A3", any(t.get("percent") == 100 for t in tr),
    "traffic {0}".format([(t.get("revisionName"), t.get("percent")) for t in tr]))
chk("A4", ann_s.get("run.googleapis.com/ingress") == "internal",
    "ingress={0}".format(ann_s.get("run.googleapis.com/ingress")))
chk("A5", (tspec.get("serviceAccountName") or "").startswith("lqabr-agent-dev@"),
    "serviceAccount={0}".format(tspec.get("serviceAccountName")))
chk("A6", "network-interfaces" in json.dumps(ann_t)
          and ann_t.get("run.googleapis.com/vpc-access-egress") == "all-traffic",
    "vpc={0} egress={1}".format(ann_t.get("run.googleapis.com/network-interfaces"),
                                ann_t.get("run.googleapis.com/vpc-access-egress")))
chk("A7", "/app/logs" in [m.get("mountPath") for m in c.get("volumeMounts", [])],
    "volumeMounts={0}".format([m.get("mountPath") for m in c.get("volumeMounts", [])]))
chk("A8", env.get("LQABR_RESEARCH_SECRETS_SOURCE") == "env",
    "LQABR_RESEARCH_SECRETS_SOURCE={0!r}".format(env.get("LQABR_RESEARCH_SECRETS_SOURCE")))
need = {"LQABR_ANTHROPIC_API_KEY", "LQABR_HUBSPOT_ACCESS_TOKEN"}
chk("A9", need <= secrets, "secret-bound={0}".format(sorted(secrets) or "none"))
chk("A10", env.get("LQABR_RESEARCH_DRY_RUN") == "0",
    "LQABR_RESEARCH_DRY_RUN={0!r}".format(env.get("LQABR_RESEARCH_DRY_RUN")))
expected = os.environ.get("MCP_EXPECTED", "")
chk("A11", (env.get("LQABR_RESEARCH_MCP_BASE_URL") or "") == expected,
    "deployed={0!r} config={1!r}".format(env.get("LQABR_RESEARCH_MCP_BASE_URL"), expected))
print("A-detail image:", c.get("image"))
sys.exit(len(fails))
PYA
[[ $? -eq 0 ]] || FAILED=$((FAILED+1))

# ===================================================== LAYER B
rule "LAYER B - startup records for the serving revision"
REV="$(python3 -c "import json;print(json.load(open('/tmp/research_svc.json'))['status']['latestReadyRevisionName'])")"
echo "revision: ${REV}"
# jsonPayload.event, NOT textPayload: the obs stream is structured JSON.
EVENTS="$(gcloud logging read \
  "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE_NAME}\" AND resource.labels.revision_name=\"${REV}\"" \
  --project="${PROJECT_ID}" --limit=400 --order=asc --format='value(jsonPayload.event)' 2>/dev/null)"
for E in service_start mcp_startup_check_ok; do
  grep -q "${E}" <<<"${EVENTS}" && pass "B-${E}" "emitted" \
    || fail "B-${E}" "expected at startup" "not emitted by ${REV}" \
            "mcp_startup_check_ok missing usually means A6 - the service is off the VPC"
done
grep -q "mcp_startup_check_failed" <<<"${EVENTS}" \
  && fail "B-no_check_failed" "mcp_startup_check_failed is ABSENT" "it was emitted" \
          "configured tool names are not on the live MCP; the service starts anyway under warn" \
  || pass "B-no_check_failed" "mcp_startup_check_failed is ABSENT"
grep -q "mcp_startup_check_unreachable" <<<"${EVENTS}" \
  && fail "B-no_unreachable" "mcp_startup_check_unreachable is ABSENT" "it was emitted" \
          "the MCP could not be reached at all - almost always A6, or the MCP is down" \
  || pass "B-no_unreachable" "mcp_startup_check_unreachable is ABSENT"

# ===================================================== LAYER C
rule "LAYER C - data plane (in-VPC job)"
RESEARCH_URL="$(python3 -c "import json;print(json.load(open('/tmp/research_svc.json'))['status']['url'])")"
echo "research: ${RESEARCH_URL}"
echo "campaign: RUN_CAMPAIGN=${RUN_CAMPAIGN} TICKET_ID=${TICKET_ID:-<unset>} limit=${CAMPAIGN_LIMIT}"
[[ "${RUN_CAMPAIGN}" == "1" ]] && echo ">> WARNING: a campaign WILL write lead_context to real leads and trigger outreach."

CLIENT="$(cat infra/verify_client.py)"
gcloud run jobs delete "${JOB}" --region="${REGION}" --project="${PROJECT_ID}" --quiet 2>/dev/null || true
gcloud run jobs create "${JOB}" \
  --image="${RUNTIME_IMAGE}" \
  --region="${REGION}" --project="${PROJECT_ID}" \
  --service-account="${RUNTIME_SA}" \
  --network="${VPC_NETWORK}" --subnet="${VPC_SUBNET}" --vpc-egress="${VPC_EGRESS}" \
  --set-env-vars="RESEARCH_URL=${RESEARCH_URL},CAMPAIGN_ROUTE=${LQABR_RESEARCH_ROUTE_CAMPAIGN_A2A:-/research/campaign/a2a},RUN_CAMPAIGN=${RUN_CAMPAIGN},TICKET_ID=${TICKET_ID},CAMPAIGN_LIMIT=${CAMPAIGN_LIMIT}" \
  --command=python --args="^@^-c@${CLIENT}" \
  --max-retries=0 --task-timeout=900s >/dev/null

gcloud run jobs execute "${JOB}" --region="${REGION}" --project="${PROJECT_ID}" --wait || FAILED=$((FAILED+1))
EXEC="$(gcloud run jobs executions list --job="${JOB}" --region="${REGION}" \
        --project="${PROJECT_ID}" --limit=1 --format='value(name)')"
echo; echo "execution: ${EXEC}"; echo
gcloud logging read \
  "resource.type=\"cloud_run_job\" AND labels.\"run.googleapis.com/execution_name\"=\"${EXEC}\"" \
  --project="${PROJECT_ID}" --limit=400 --order=asc --format='value(textPayload)'
gcloud run jobs delete "${JOB}" --region="${REGION}" --project="${PROJECT_ID}" --quiet 2>/dev/null || true

rule "VERDICT"
[[ ${FAILED} -eq 0 ]] && echo "A and B passed. Read the PASS/FAIL lines above for layer C." \
                      || echo "${FAILED} host-side check group(s) FAILED."
exit ${FAILED}
