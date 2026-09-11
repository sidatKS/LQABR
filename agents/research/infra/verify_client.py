"""Layer C of the research verify. Spec: docs/research_verify_spec.md 6.3/6.4.

Runs inside a Cloud Run job on lqabr-vpc: lqabr-dev-research is ingress=internal,
so a laptop curl returns 404 even with a valid ID token.

DEFAULT MODE WRITES NOTHING. A research campaign writes lead_context onto every
contact in an industry, and each write trips the gateway's R2-lead-context route
into the Email agent - so C5/C6 are opt-in via RUN_CAMPAIGN=1.

No "at" sign may appear in this file; the wrapper passes it to gcloud --args
using a caret-at-caret field delimiter.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ["RESEARCH_URL"].rstrip("/")
CAMPAIGN_ROUTE = os.environ.get("CAMPAIGN_ROUTE", "/research/campaign/a2a")
RUN_CAMPAIGN = os.environ.get("RUN_CAMPAIGN", "0") == "1"
TICKET_ID = os.environ.get("TICKET_ID", "").strip()
LIMIT = os.environ.get("CAMPAIGN_LIMIT", "1")
META = ("http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/identity?audience=")

FAILURES = []
CHECKS = {
    "C1":  ("GET /health is UP with a reachable MCP and writes enabled",
            "mcp.reachable false is almost always A6 - the service is off the VPC and "
            "cannot reach the ingress=internal MCP. Else verify the MCP: "
            "bash infra/gcp/mcp/02_probe.sh"),
    "C1b": ("no log stream is degraded",
            "a stream's file could not be opened - check the /app/logs volume (A7)"),
    "C2":  ("the tools research CALLS (read_lead, read_blog, write) exist on the MCP",
            "align LQABR_RESEARCH_MCP_TOOL_* to the four the MCP exposes"),
    "C2b": ("list_leads is absent (expected) - campaign uses hubspot_direct instead",
            "not blocking: use_direct_lead_lookup=true bypasses the MCP for this one read"),
    "C3":  ("GET / returns identity and the campaign route",
            "the route index does not name the campaign route the gateway posts to"),
    "C4":  ("a hand-off with NO objectId is REJECTED, writing nothing",
            "if this is accepted the guard is gone and a malformed gateway hand-off would "
            "start a campaign against nothing"),
    "C5":  ("a real campaign hand-off is ACCEPTED (queued)",
            "rejection text names the reason: no objectId, or the record kind is not a post"),
    "C6":  ("accept is not completion - the run must be followed in the obs log",
            "read campaign_complete / run_crashed for this run_id on the host"),
}


def ok(cid, detail=""):
    print("PASS {0:<4} {1}".format(cid, CHECKS[cid][0]), flush=True)
    if detail:
        print("          {0}".format(detail), flush=True)


def bad(cid, detail=""):
    FAILURES.append(cid)
    print("FAIL {0:<4} {1}".format(cid, CHECKS[cid][0]), flush=True)
    if detail:
        print("          got: {0}".format(detail), flush=True)
    print("          FIX: {0}".format(CHECKS[cid][1]), flush=True)


def warn(cid, detail=""):
    print("WARN {0:<4} {1}".format(cid, CHECKS[cid][0]), flush=True)
    if detail:
        print("          {0}".format(detail), flush=True)


def rule(t):
    print("\n" + "-" * 72 + "\n" + t + "\n" + "-" * 72, flush=True)


TOKEN = urllib.request.urlopen(urllib.request.Request(
    META + BASE, headers={"Metadata-Flavor": "Google"}), timeout=15).read().decode()
HDRS = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}


def call(method, path, body=None, timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=HDRS, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, "{0}: {1}".format(type(exc).__name__, exc)


rule("C1  GET /health")
status, raw = call("GET", "/health")
print("HTTP", status)
if status != 200:
    bad("C1", "HTTP {0}: {1}".format(status, raw[:300]))
else:
    h = json.loads(raw)
    print(json.dumps(h, indent=2)[:2000], flush=True)
    mcp = h.get("mcp") or {}
    if h.get("status") == "UP" and mcp.get("reachable") and h.get("dry_run") is False:
        ok("C1", "UP; mcp reachable; dry_run={0}".format(h.get("dry_run")))
    else:
        bad("C1", "status={0} reachable={1} dry_run={2} error={3}".format(
            h.get("status"), mcp.get("reachable"), h.get("dry_run"), mcp.get("error")))
    deg = ((h.get("logging") or {}).get("degraded")) or []
    ok("C1b", "none") if not deg else bad("C1b", str(deg))

rule("C2  GET /mcp/tools")
status, raw = call("GET", "/mcp/tools")
print("HTTP", status)
if status != 200:
    bad("C2", "HTTP {0}: {1}".format(status, raw[:300]))
else:
    t = json.loads(raw)
    print(json.dumps(t, indent=2)[:1500], flush=True)
    cfg = t.get("configured") or {}
    missing = t.get("missing") or []
    called = {cfg.get("read_lead"), cfg.get("read_blog"), cfg.get("write")}
    blocking = sorted(n for n in missing if n in called)
    latent = sorted(n for n in missing if n not in called)
    if blocking:
        bad("C2", "called tools absent: {0}".format(blocking))
    else:
        ok("C2", "read_lead={0} read_blog={1} write={2}".format(
            cfg.get("read_lead"), cfg.get("read_blog"), cfg.get("write")))
    if latent:
        warn("C2b", "absent: {0} - campaign uses hubspot_direct for this".format(latent))

rule("C3  GET /")
status, raw = call("GET", "/")
print("HTTP", status, raw[:400])
if status == 200 and CAMPAIGN_ROUTE in raw:
    ok("C3", "campaign route advertised: {0}".format(CAMPAIGN_ROUTE))
else:
    bad("C3", "HTTP {0}: {1}".format(status, raw[:300]))

rule("C4  campaign hand-off with NO objectId  (must be rejected, writes nothing)")
status, raw = call("POST", CAMPAIGN_ROUTE, {"message": {"parts": [{"text": "{}"}]}})
print("HTTP", status, raw[:500])
low = raw.lower()
if "reject" in low or "no objectid" in low or "carries no objectid" in low:
    ok("C4", "guard held; nothing was queued")
else:
    bad("C4", "HTTP {0}: {1}".format(status, raw[:400]))

if not RUN_CAMPAIGN:
    rule("C5/C6 SKIPPED - RUN_CAMPAIGN=0 (default)")
    print("A campaign writes lead_context to EVERY lead in the post's industry, and each",
          flush=True)
    print("write trips the gateway R2-lead-context route into the Email agent.", flush=True)
    print("Enable deliberately:  RUN_CAMPAIGN=1 TICKET_ID=<post objectId>", flush=True)
elif not TICKET_ID:
    rule("C5/C6 SKIPPED - RUN_CAMPAIGN=1 but TICKET_ID is empty")
else:
    rule("C5  REAL campaign hand-off  (this WILL write to leads)")
    body = {"jsonrpc": "2.0", "id": "verify", "method": "message/send",
            "params": {"message": {"parts": [{"text": json.dumps(
                {"objectId": TICKET_ID, "limit": int(LIMIT)})}]},
                "metadata": {"object_id": TICKET_ID,
                             "subscription_type": "ticket.propertyChange"}}}
    print("request:", json.dumps(body)[:500], flush=True)
    status, raw = call("POST", CAMPAIGN_ROUTE, body, timeout=120)
    print("HTTP", status, raw[:600])
    if status == 200 and "accepted" in raw.lower():
        run_id = ""
        try:
            d = json.loads(raw)
            run_id = (d.get("result") or d).get("run_id", "")
        except Exception:
            pass
        ok("C5", "queued; run_id={0}".format(run_id or "<unparsed>"))
        warn("C6", "accept is NOT completion - follow run_id={0} on the host:\n"
                   "          gcloud logging read 'resource.type=\"cloud_run_revision\" AND "
                   "resource.labels.service_name=\"lqabr-dev-research\" AND "
                   "jsonPayload.run_id=\"{0}\"' --project=ldqfingsrv-dev --order=asc "
                   "--format='value(jsonPayload.event,jsonPayload)'".format(run_id))
    else:
        bad("C5", "HTTP {0}: {1}".format(status, raw[:400]))

rule("LAYER C RESULT")
if FAILURES:
    print("FAILED: {0}\n".format(", ".join(FAILURES)), flush=True)
    for cid in FAILURES:
        print("  {0:<4} {1}\n       FIX: {2}\n".format(cid, CHECKS[cid][0], CHECKS[cid][1]))
else:
    print("ALL LAYER C CHECKS PASSED", flush=True)
sys.exit(len(FAILURES))
