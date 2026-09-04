"""Layer C of the summary verify - the data-plane checks, run from inside the VPC.

lqabr-dev-summary and lqabr-dev-mcp are both ingress=internal, so these four
checks cannot run from a laptop. 04_verify.sh ships this file into a Cloud Run
job on lqabr-vpc.

  C1  GET  /health      the instance is up and bound to a reachable MCP
  C2  GET  /mcp/tools   configured tool names exist on the live MCP (missing == [])
  C3  POST /summary/run one real end-to-end run, and the input set it derives
  C4  MCP  get_blog_summary   read the ticket back out of HubSpot on the same key

Every check prints "PASS <id>" or "FAIL <id>". Exit code is the number of
failures, so the wrapper can branch without parsing prose.

No "at" sign may appear in this file; the wrapper passes it to gcloud --args
using a caret-at-caret field delimiter.
"""
import json
import os
import sys
import urllib.error
import urllib.request

SUMMARY = os.environ["SUMMARY_URL"].rstrip("/")
MCP = os.environ["MCP_URL"].rstrip("/")
BLOG = os.environ["BLOG_URL"]
PUBLISHED_AT = os.environ["PUBLISHED_AT"]
RUN_E2E = os.environ.get("RUN_E2E", "1") == "1"
PROTOCOL = "2025-06-18"
META = ("http://metadata.google.internal/computeMetadata/v1/instance/"
        "service-accounts/default/identity?audience=")

FAILURES = []

#: id -> (what it asserts, what to do when it fails). Mirrors
#: summary_verify_spec 6.3 and 7 so the output needs no companion document.
CHECKS = {
    "C1":  ("service is UP and bound to a reachable MCP",
            "mcp.reachable false -> MCP down or wrong MCP_BASE_URL. Verify the MCP first: "
            "bash infra/gcp/mcp/02_probe.sh"),
    "C1b": ("no log stream is degraded",
            "a stream's file could not be opened or rotated - check the /app/logs volume (A7)"),
    "C1c": ("dry_run is false, so writes reach HubSpot",
            "LQABR_SUMMARY_DRY_RUN=1 suppresses every write; redeploy with 0"),
    "C2":  ("the tools this flow CALLS (read, write) exist on the live MCP",
            "align MCP_TOOL_READ / MCP_TOOL_WRITE to the four the MCP exposes: "
            "get_blog_summary, get_lead_profile, upsert_blog_summary, upsert_lead_profile"),
    "C2b": ("a configured tool nothing calls is absent (latent, not blocking)",
            "either add it to the MCP or drop the env var; it breaks the day a caller appears"),
    "C3":  ("one real run completed AND hubspot.status is 'written'",
            "'skipped' still reports status=completed - read the error text, it names which "
            "of the four required args was blank. 'error' + \"is not one of\" = industry enum; "
            "'halted' = MCP-side systemic fault"),
    "C4":  ("HubSpot returns the ticket the write created - the ONLY proof it landed",
            "isError means the tool call itself was rejected (check the argument name against "
            "the deployed image). found:false or a reply that does not name the ticket means "
            "the upsert did not land - read sent_published_at in hubspot_write_raw_result"),
}


def _meaning(check):
    return CHECKS.get(check, ("", ""))[0]


def _fix(check):
    return CHECKS.get(check, ("", ""))[1]


def ok(check, detail=""):
    print("PASS {0:<4} {1}".format(check, _meaning(check)), flush=True)
    if detail:
        print("          {0}".format(detail), flush=True)


def bad(check, detail=""):
    FAILURES.append(check)
    print("FAIL {0:<4} {1}".format(check, _meaning(check)), flush=True)
    if detail:
        print("          got: {0}".format(detail), flush=True)
    print("          FIX: {0}".format(_fix(check)), flush=True)


def warn(check, detail=""):
    print("WARN {0:<4} {1}".format(check, _meaning(check)), flush=True)
    if detail:
        print("          {0}".format(detail), flush=True)
    print("          FIX: {0}".format(_fix(check)), flush=True)


def rule(title):
    print("\n" + "-" * 72 + "\n" + title + "\n" + "-" * 72, flush=True)


def token_for(audience):
    req = urllib.request.Request(META + audience, headers={"Metadata-Flavor": "Google"})
    return urllib.request.urlopen(req, timeout=15).read().decode()


def call(url, body=None, headers=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")
    except Exception as exc:                      # connection-level failure
        return 0, {}, "{0}: {1}".format(type(exc).__name__, exc)


def unwrap(raw):
    lines = [l[6:] for l in raw.splitlines() if l.startswith("data: ")]
    return "\n".join(lines) if lines else raw


SUMMARY_HDRS = {"Authorization": "Bearer " + token_for(SUMMARY),
                "Content-Type": "application/json"}

# ------------------------------------------------------------------ C1 health
rule("C1  GET /health")
status, _, raw = call(SUMMARY + "/health", headers=SUMMARY_HDRS, timeout=120)
print("HTTP", status)
health = {}
if status != 200:
    bad("C1", "expected 200, got {0}: {1}".format(status, raw[:300]))
else:
    health = json.loads(raw)
    print(json.dumps(health, indent=2)[:2500], flush=True)
    mcp = health.get("mcp") or {}
    if health.get("status") != "UP":
        bad("C1", "status is {0}".format(health.get("status")))
    elif not mcp.get("reachable"):
        bad("C1", "mcp.reachable is false: {0}".format(mcp.get("error")))
    else:
        ok("C1", "UP; mcp reachable; dry_run={0}; write_tool={1}".format(
            health.get("dry_run"), mcp.get("write_tool")))
    degraded = ((health.get("logging") or {}).get("degraded")) or []
    if degraded:
        bad("C1b", "logging streams degraded: {0}".format(degraded))
    else:
        ok("C1b", "no degraded log streams")
    if health.get("dry_run") is not False:
        bad("C1c", "dry_run is {0} - writes are suppressed".format(health.get("dry_run")))
    else:
        ok("C1c", "dry_run is False, writes are live")

# ------------------------------------------------------------- C2 /mcp/tools
rule("C2  GET /mcp/tools")
status, _, raw = call(SUMMARY + "/mcp/tools", headers=SUMMARY_HDRS, timeout=180)
print("HTTP", status)
if status != 200:
    bad("C2", "expected 200, got {0}: {1}".format(status, raw[:300]))
else:
    tools = json.loads(raw)
    print(json.dumps(tools, indent=2)[:2000], flush=True)
    # Only the tools this flow actually CALLS are a hard failure. `list_leads`
    # is configured by default but has no caller in summary (grep: the method
    # exists, nothing invokes it), and ensure_ready() deliberately asserts only
    # read+write -- "asserting it would refuse to start over a tool nothing
    # calls". Reporting it as FAIL made a healthy service look broken.
    missing = tools.get("missing") or []
    configured = tools.get("configured") or {}
    called = {configured.get("read"), configured.get("write")}
    blocking = sorted(n for n in missing if n in called)
    latent = sorted(n for n in missing if n not in called)
    if blocking:
        bad("C2", "tools this flow CALLS are absent on the MCP: {0}".format(blocking))
    else:
        ok("C2", "read={0} write={1} both present".format(
            configured.get("read"), configured.get("write")))
    if latent:
        warn("C2b", "absent and never called by this flow: {0}".format(latent))

if not RUN_E2E:
    rule("C3/C4 SKIPPED (RUN_E2E=0) - no HubSpot write attempted")
    print("\nfailures:", FAILURES or "none", flush=True)
    sys.exit(len(FAILURES))

# ------------------------------------------------------------ C3 /summary/run
rule("C3  POST /summary/run  (this WRITES to HubSpot)")
request_body = {"source": {"kind": "url", "url": BLOG},
                "hubspot": {"object_type": "ticket", "blog_published_at": PUBLISHED_AT}}
print("request:")
print(json.dumps(request_body, indent=2), flush=True)

status, _, raw = call(SUMMARY + "/summary/run", request_body, SUMMARY_HDRS, timeout=600)
print("\nHTTP", status)
run = {}
try:
    run = json.loads(raw)
    print(json.dumps(run, indent=2)[:6000], flush=True)
except Exception:
    bad("C3", "unparseable response: {0}".format(raw[:500]))

hs = run.get("hubspot") or {}
summ = run.get("summary") or {}
if run.get("status") == "completed" and hs.get("status") == "written":
    ok("C3", "run {0} wrote via {1}".format(run.get("run_id"), hs.get("tool")))
else:
    bad("C3", "status={0} hubspot.status={1} error={2}".format(
        run.get("status"), hs.get("status"), run.get("error") or hs.get("error")))

rule("INPUT SET the agent sent to upsert_blog_summary")
body_text = summ.get("summary") or ""
print("tool              :", hs.get("tool"))
print("properties        :", hs.get("properties"))
print("subject           :", summ.get("title"))
print("blog_published_at :", PUBLISHED_AT, "  <- the upsert KEY")
print("blog_industry     :", summ.get("industry"))
print("blog_summary      :", len(body_text), "chars; first 300:")
print(body_text[:300], flush=True)

# --------------------------------------------------- C4 read it back from HubSpot
rule("C4  MCP get_blog_summary  (reads the HubSpot ticket back)")
# ARGUMENT NAME: the promoted image (digest aa91c5b8, 2026-08-28) takes objectId.
# An earlier image took blog_published_at. Calling it with the wrong name returns
# {"isError": true} with a pydantic validation message - which an absence-of-
# "found": false test happily passes. Hence the positive assertion below.
ticket_id = str(hs.get("object_id") or "").strip()
if hs.get("status") != "written":
    print("SKIP C4  C3 did not write, so there is nothing to read back.", flush=True)
    print("         C4 is only meaningful after a successful upsert.", flush=True)
elif not ticket_id:
    bad("C4", "the write succeeded but returned no object_id to read back")
else:
    base = {"Authorization": "Bearer " + token_for(MCP),
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"}
    status, headers, raw = post(MCP + "/mcp", {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": PROTOCOL, "capabilities": {},
                   "clientInfo": {"name": "summary-verify", "version": "1.0"}}}, base, timeout=90)
    if status not in (200, 202):
        bad("C4", "MCP initialize returned {0}: {1}".format(status, raw[:300]))
    else:
        base["Mcp-Session-Id"] = headers.get("Mcp-Session-Id") or headers.get("mcp-session-id") or ""
        base["MCP-Protocol-Version"] = PROTOCOL
        post(MCP + "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}, base, timeout=60)
        status, _, raw = post(MCP + "/mcp", {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "get_blog_summary",
                       "arguments": {"objectId": ticket_id}}}, base, timeout=180)
        try:
            result = json.loads(unwrap(raw))["result"]
        except Exception as exc:
            bad("C4", "could not parse get_blog_summary: {0} :: {1}".format(exc, raw[:400]))
        else:
            print(json.dumps(result, indent=2)[:3000], flush=True)
            record = result.get("structuredContent", result)
            blob = json.dumps(record).lower()
            # POSITIVE assertion. isError, an explicit found:false, or a payload
            # that never names the ticket are all failures - never a silent pass.
            if result.get("isError"):
                bad("C4", "the MCP returned isError - the call itself was rejected")
            elif '"found": false' in blob or '"found":false' in blob:
                bad("C4", "no ticket in HubSpot for objectId {0}".format(ticket_id))
            elif ticket_id.lower() not in blob:
                bad("C4", "the reply does not name ticket {0}: {1}".format(ticket_id, blob[:300]))
            else:
                ok("C4", "HubSpot returned ticket {0}".format(ticket_id))

rule("LAYER C RESULT")
if FAILURES:
    print("FAILED: {0}\n".format(", ".join(FAILURES)), flush=True)
    for check in FAILURES:
        print("  {0:<4} {1}".format(check, _meaning(check)))
        print("       FIX: {0}\n".format(_fix(check)))
else:
    print("ALL LAYER C CHECKS PASSED", flush=True)
sys.exit(len(FAILURES))
