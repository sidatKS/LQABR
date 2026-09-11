#!/usr/bin/env bash
# Verify a deployed lqabr-dev-gtwy. Spec: docs/gateway_verify_spec.md
#
#   A  control plane  what Cloud Run says the service IS      (gcloud)
#   B  startup        what it said as it booted               (Cloud Logging)
#   C  data plane     what it actually DOES                   (curl, from HERE)
#
# Layer C runs from the host, not a Cloud Run job: the gateway is
# --allow-unauthenticated, so every route is directly reachable. That is also
# the reason C5 exists - the door is open by design and the v3 HMAC is the lock.
#
# No check dispatches to an agent: the one signed request carries a deliberately
# unroutable event, so it terminates in a discard (spec 5.7).
set -uo pipefail
cd "$(dirname "$0")/.."
source infra/config.sh

FAILED=0
pass() { printf 'PASS %-22s %s\n' "$1" "$2"; [[ -n "${3:-}" ]] && printf '          %s\n' "$3"; return 0; }
fail() { printf 'FAIL %-22s %s\n' "$1" "$2"; [[ -n "${3:-}" ]] && printf '          got: %s\n' "$3"
         [[ -n "${4:-}" ]] && printf '          FIX: %s\n' "$4"; FAILED=$((FAILED+1)); return 0; }
rule() { echo; echo "======================================================================"
         echo "$1"; echo "======================================================================"; }

cat <<'LEGEND'
Spec: docs/gateway_verify_spec.md   Every check prints what it asserts; a FAIL
also prints what was seen and what to do. Layers: A control plane (gcloud) /
B startup (logs) / C data plane (curl - this service is public).
LEGEND

# ===================================================== LAYER A - control plane
rule "LAYER A - control plane"
SVC="$(gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" \
        --project="${PROJECT_ID}" --format=json 2>/dev/null)" || SVC=""
if [[ -z "${SVC}" ]]; then
  fail A1 "the service exists in this project and region" \
       "${SERVICE_NAME} not found in ${REGION}/${PROJECT_ID}" \
       "not deployed. Run: bash infra/03_deploy_run.sh"
  exit 1
fi
echo "${SVC}" > /tmp/gtwy_svc.json
pass A1 "the service exists in this project and region" "${SERVICE_NAME}"

# allUsers invoker is a separate API call from describe.
IAM="$(gcloud run services get-iam-policy "${SERVICE_NAME}" --region="${REGION}" \
        --project="${PROJECT_ID}" --format=json 2>/dev/null || echo '{}')"
echo "${IAM}" > /tmp/gtwy_iam.json

# A9 needs to know whether versions/latest actually resolves - describe alone
# cannot tell you, and that was the live root cause.
if gcloud secrets versions describe latest --secret "${APP_SECRET_NAME}" \
     --project "${PROJECT_ID}" >/dev/null 2>&1; then
  export APP_SECRET_HAS_VERSION=1
else
  export APP_SECRET_HAS_VERSION=0
fi
export APP_SECRET_NAME

python3 - <<'PYA'
import json, sys
svc = json.load(open("/tmp/gtwy_svc.json"))
iam = json.load(open("/tmp/gtwy_iam.json"))
meta, tpl, status = svc["metadata"], svc["spec"]["template"], svc["status"]
ann_s, ann_t = meta.get("annotations", {}), tpl["metadata"].get("annotations", {})
tspec = tpl["spec"]; c = tspec["containers"][0]
env = {e["name"]: e.get("value") for e in c.get("env", [])}
secrets = {e["name"] for e in c.get("env", []) if "valueFrom" in e}
url = status.get("url", "")

CHECKS = {
 "A2": ("the SERVICE Ready condition is True",
        "the message printed above is the diagnosis - Cloud Run states the exact cause. "
        "A secret_key_ref that 'was not found' means the secret has no version (A9)"),
 "A3": ("one revision serves 100% of traffic", "a split left an old revision live"),
 "A4": ("ingress is ALL - required, HubSpot cannot present an ID token",
        "internal ingress blocks HubSpot entirely; redeploy with 03_deploy_run.sh"),
 "A5": ("allUsers may invoke - also required for the same reason",
        "without it every HubSpot delivery 403s before reaching the container"),
 "A6": ("runs as lqabr-agent-dev", "--service-account omitted; default compute SA holds no roles"),
 "A7": ("is ON the VPC with all-traffic egress",
        "EGRESS, not ingress: lqabr-dev-research is ingress=internal and R-blog-summary "
        "dispatches to it. Off-VPC that is a 404"),
 "A8": ("maxScale is 1 and minScale >= 1",
        "DedupeStore is in-memory per process - N instances is N stores, so a HubSpot "
        "retry can dispatch twice. min>=1 avoids cold-start retries"),
 "A9": ("HUBSPOT_APP_SECRET is bound to the expected secret AND it resolves",
        "a binding to a secret with no versions injects NOTHING: the container sees the var "
        "unset, signature verification has no key, and every webhook 401s while the service "
        "looks healthy. Run 01_secrets.sh"),
 "A10": ("LQABR_GATEWAY_PUBLIC_URL equals the service's own URL",
         "the v3 signature is computed over the full URI; any drift is a silent permanent 401"),
 "A11": ("all three LQABR_*_AGENT_URL are set",
         "names come from endpoint_env in agents_registry.yaml. Unset => matching events are "
         "audited as routing errors and /readyz is 503. Re-run 03_deploy_run.sh"),
}
fails = []
def chk(cid, cond, detail):
    meaning, fix = CHECKS[cid]
    print(("PASS " if cond else "FAIL ") + "{0:<4} {1}".format(cid, meaning))
    print("          " + ("" if cond else "got: ") + detail)
    if not cond:
        fails.append(cid); print("          FIX: " + fix)

ready = [x for x in status.get("conditions", []) if x["type"] == "Ready"]
chk("A2", bool(ready) and ready[0]["status"] == "True",
    "Ready={0} revision={1}{2}".format(
        ready[0]["status"] if ready else "?", status.get("latestReadyRevisionName"),
        "\n          message: " + (ready[0].get("message") or "") if ready and ready[0].get("message") else ""))
tr = status.get("traffic", [])
chk("A3", any(t.get("percent") == 100 for t in tr),
    "traffic {0}".format([(t.get("revisionName"), t.get("percent")) for t in tr]))
chk("A4", ann_s.get("run.googleapis.com/ingress") == "all",
    "ingress={0}".format(ann_s.get("run.googleapis.com/ingress")))
members = [m for b in iam.get("bindings", []) if b.get("role") == "roles/run.invoker"
           for m in b.get("members", [])]
chk("A5", "allUsers" in members, "run.invoker members={0}".format(members or "none"))
chk("A6", (tspec.get("serviceAccountName") or "").startswith("lqabr-agent-dev@"),
    "serviceAccount={0}".format(tspec.get("serviceAccountName")))
chk("A7", "network-interfaces" in json.dumps(ann_t)
          and ann_t.get("run.googleapis.com/vpc-access-egress") == "all-traffic",
    "vpc={0} egress={1}".format(ann_t.get("run.googleapis.com/network-interfaces"),
                                ann_t.get("run.googleapis.com/vpc-access-egress")))
mx = ann_t.get("autoscaling.knative.dev/maxScale")
mn = ann_t.get("autoscaling.knative.dev/minScale")
chk("A8", mx == "1" and (mn or "0") != "0", "maxScale={0} minScale={1}".format(mx, mn))
bound = {e["name"]: (e.get("valueFrom") or {}).get("secretKeyRef", {}).get("name")
         for e in c.get("env", []) if "valueFrom" in e}
import os as _os
expected_secret = _os.environ.get("APP_SECRET_NAME", "")
chk("A9", bound.get("HUBSPOT_APP_SECRET") == expected_secret and
          _os.environ.get("APP_SECRET_HAS_VERSION") == "1",
    "bound to {0!r} (expected {1!r}); has_version={2}".format(
        bound.get("HUBSPOT_APP_SECRET"), expected_secret,
        _os.environ.get("APP_SECRET_HAS_VERSION")))
chk("A10", (env.get("LQABR_GATEWAY_PUBLIC_URL") or "").rstrip("/") == url.rstrip("/"),
    "public_url={0!r} service_url={1!r}".format(env.get("LQABR_GATEWAY_PUBLIC_URL"), url))
need = ["LQABR_EMAIL_AGENT_URL", "LQABR_TEXT_VOICE_AGENT_URL", "LQABR_RESEARCH_AGENT_URL"]
absent = [n for n in need if not (env.get(n) or "").strip()]
chk("A11", not absent, "unset={0}".format(absent or "none"))
print("A-detail image:", c.get("image"))
sys.exit(len(fails))
PYA
[[ $? -eq 0 ]] || FAILED=$((FAILED+1))

# ========================================================== LAYER B - startup
rule "LAYER B - startup records for the serving revision"
REV="$(python3 -c "import json;print(json.load(open('/tmp/gtwy_svc.json'))['status']['latestReadyRevisionName'])")"
echo "revision: ${REV}"
# jsonPayload, NOT textPayload: the audit stream is structured JSON. Filtering
# on the revision rather than --freshness, so a stale entry cannot read as current.
LOGS="$(gcloud logging read \
  "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${SERVICE_NAME}\" AND resource.labels.revision_name=\"${REV}\"" \
  --project="${PROJECT_ID}" --limit=400 --order=asc --format=json 2>/dev/null || echo '[]')"
echo "${LOGS}" > /tmp/gtwy_logs.json
python3 - <<'PYB'
import json
entries = json.load(open("/tmp/gtwy_logs.json"))
blob = json.dumps(entries).lower()
started = "startup" in blob or "gateway_start" in blob or "entrypoint" in blob
print(("PASS " if started else "FAIL ") + "{0:<22} {1}".format(
    "B-startup", "a startup record was emitted"))
if not started:
    print("          got: no startup record in %d entries" % len(entries))
    print("          FIX: the container never reached startup; read the revision's stderr")
cfg_err = "config_error" in blob or "record_config_error" in blob
print(("FAIL " if cfg_err else "PASS ") + "{0:<22} {1}".format(
    "B-no_config_error", "NO config-error record was emitted"))
if cfg_err:
    print("          got: a config error was recorded")
    print("          FIX: _config_problems() fails CLOSED - the service looks healthy and")
    print("               401s forever. The record names the variable; usually A9 or A10")
PYB

# ======================================================== LAYER C - data plane
rule "LAYER C - data plane (curl, from here: the service is public)"
GATEWAY_URL="$(python3 -c "import json;print(json.load(open('/tmp/gtwy_svc.json'))['status']['url'])")"
echo "gateway: ${GATEWAY_URL}${INGRESS_PATH}"
# Read the client secret so C6/C7 can prove signature ACCEPTANCE, not just
# rejection. Never echoed.
APP_SECRET="$(gcloud secrets versions access latest --secret="${APP_SECRET_NAME}" \
              --project="${PROJECT_ID}" 2>/dev/null || true)"
[[ -n "${APP_SECRET}" ]] && echo "app secret: read (${#APP_SECRET} chars)" \
                         || echo "app secret: NOT readable - C6/C7 will be skipped"

GATEWAY_URL="${GATEWAY_URL}" APP_SECRET="${APP_SECRET}" INGRESS_PATH="${INGRESS_PATH}" \
  python3 infra/verify_client.py
[[ $? -eq 0 ]] || FAILED=$((FAILED+1))

rule "VERDICT"
if [[ ${FAILED} -eq 0 ]]; then
  echo "All layers passed."
else
  echo "${FAILED} check group(s) FAILED - see the FAIL lines above."
fi
exit ${FAILED}
