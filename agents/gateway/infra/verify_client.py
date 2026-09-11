"""Layer C of the gateway verify. Spec: docs/gateway_verify_spec.md 6.3.

Runs from the HOST, not a Cloud Run job: lqabr-dev-gtwy is
--allow-unauthenticated, so every route is reachable directly. That is also why
C5 exists - the door is open by design, and the v3 HMAC is the only lock.

No check dispatches to an agent. The one signed request carries a deliberately
unroutable event (spec 5.7), so it terminates in a discard.

env: GATEWAY_URL, APP_SECRET, INGRESS_PATH
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ["GATEWAY_URL"].rstrip("/")
SECRET = os.environ.get("APP_SECRET", "")
PATH = os.environ.get("INGRESS_PATH", "/hubspot/events")

FAILURES = []

CHECKS = {
    "C1": ("GET / returns the live route table from agents_registry.yaml",
           "routes missing or renamed - compare config/agents_registry.yaml with the image"),
    "C2": ("GET /healthz reports the process is alive",
           "the container is not serving; check the revision's stderr"),
    "C3": ("GET /readyz is 200 with every agent endpoint resolving",
           "503 means an endpoint_env does not resolve (A11) or _config_problems() "
           "found something (Layer B). The body names which"),
    "C4": ("GET /metrics returns handoff, ingress and dedupe counters",
           "route missing or the app failed to build its audit metrics"),
    "C5": ("an UNSIGNED POST to the ingress is REJECTED with 401",
           "*** 200 MEANS THE PUBLIC INGRESS ACCEPTS FORGED TRIGGERS. *** "
           "gateway.ingress.signature.enabled is false in the IMAGE. config.yaml is "
           "baked in, so fix it and REBUILD (gateway_deploy_spec 5)"),
    "C6": ("a correctly signed but STALE delivery is rejected with 401",
           "the 300s replay window is not being enforced - check "
           "gateway.ingress.signature.max_age_seconds"),
    "C7": ("a validly signed, deliberately unroutable event returns 200 and is discarded",
           "401 = wrong secret or LQABR_GATEWAY_PUBLIC_URL mismatch (A9/A10). "
           "503 = it matched a route and dispatch failed; the probe must stay unroutable"),
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


def rule(title):
    print("\n" + "-" * 72 + "\n" + title + "\n" + "-" * 72, flush=True)


def call(method, path, body=None, headers=None, timeout=30):
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(URL + path, data=data,
                                 headers=headers or {}, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, "{0}: {1}".format(type(exc).__name__, exc)


def sign(method, uri, body, ts):
    """HubSpot v3: base64(HMAC-SHA256(method + uri + body + timestamp)).
    uri is the FULL request URI - the one LQABR_GATEWAY_PUBLIC_URL pins."""
    msg = "{0}{1}{2}{3}".format(method.upper(), uri, body, ts)
    return base64.b64encode(
        hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()


# ------------------------------------------------------------------ C1 identity
rule("C1  GET /")
status, raw = call("GET", "/")
print("HTTP", status)
if status != 200:
    bad("C1", "HTTP {0}: {1}".format(status, raw[:200]))
else:
    doc = json.loads(raw)
    print(json.dumps(doc, indent=2)[:1500], flush=True)
    ids = sorted(r.get("id") for r in doc.get("routes", []))
    expected = sorted(["R2-lead-context", "R3-email-opened", "R-blog-summary"])
    if ids == expected:
        ok("C1", "routes={0}  carries={1}".format(ids, doc.get("carries")))
    else:
        bad("C1", "routes={0} expected={1}".format(ids, expected))

# ------------------------------------------------------------------ C2 healthz
rule("C2  GET /healthz")
status, raw = call("GET", "/healthz")
print("HTTP", status, raw[:300])
if status == 200 and json.loads(raw).get("status") == "ok":
    ok("C2", raw[:160])
else:
    bad("C2", "HTTP {0}: {1}".format(status, raw[:200]))

# ------------------------------------------------------------------- C3 readyz
rule("C3  GET /readyz")
status, raw = call("GET", "/readyz")
print("HTTP", status)
print(raw[:1200], flush=True)
if status == 200:
    ok("C3", "all agent endpoints resolve, no config problems")
else:
    bad("C3", "HTTP {0}: {1}".format(status, raw[:500]))

# ------------------------------------------------------------------ C4 metrics
rule("C4  GET /metrics")
status, raw = call("GET", "/metrics")
print("HTTP", status, raw[:400])
if status == 200:
    ok("C4", raw[:200])
else:
    bad("C4", "HTTP {0}: {1}".format(status, raw[:200]))

# ============================================================ signature checks
probe = json.dumps([{
    "eventId": int(time.time() * 1000),
    "subscriptionType": "contact.propertyChange",
    # Deliberately not a routed property: R2 watches lead_context, R3 watches
    # email_status, R-blog watches blog_summary on a ticket. This matches none,
    # so it is discarded and NO agent is dispatched (spec 5.7).
    "propertyName": "lqabr_verify_probe",
    "propertyValue": "verify",
    "objectId": 0,
    "attemptNumber": 0,
}])
JSON_HDR = {"Content-Type": "application/json"}

rule("C5  UNSIGNED POST to the ingress  (must be 401)")
status, raw = call("POST", PATH, probe, dict(JSON_HDR))
print("HTTP", status, raw[:300])
if status == 401:
    ok("C5", "rejected, as it must be")
else:
    bad("C5", "HTTP {0} - expected 401. Body: {1}".format(status, raw[:300]))

if not SECRET:
    rule("C6/C7 SKIPPED - APP_SECRET not supplied")
    print("Provide it to exercise signature ACCEPTANCE as well as rejection.", flush=True)
else:
    uri = "{0}{1}".format(URL, PATH)

    rule("C6  correctly signed but STALE  (must be 401)")
    stale_ts = str(int((time.time() - 600) * 1000))
    hdr = dict(JSON_HDR)
    hdr["X-HubSpot-Request-Timestamp"] = stale_ts
    hdr["X-HubSpot-Signature-v3"] = sign("POST", uri, probe, stale_ts)
    status, raw = call("POST", PATH, probe, hdr)
    print("HTTP", status, raw[:300])
    if status == 401:
        ok("C6", "replay window enforced (600s old rejected)")
    else:
        bad("C6", "HTTP {0} - expected 401".format(status))

    rule("C7  validly signed, UNROUTABLE event  (must be 200, discarded)")
    ts = str(int(time.time() * 1000))
    hdr = dict(JSON_HDR)
    hdr["X-HubSpot-Request-Timestamp"] = ts
    hdr["X-HubSpot-Signature-v3"] = sign("POST", uri, probe, ts)
    print("signed uri:", uri, flush=True)
    status, raw = call("POST", PATH, probe, hdr, timeout=60)
    print("HTTP", status, raw[:600])
    if status == 200:
        ok("C7", "signature accepted; event discarded, nothing dispatched")
    else:
        bad("C7", "HTTP {0}: {1}".format(status, raw[:400]))

rule("LAYER C RESULT")
if FAILURES:
    print("FAILED: {0}\n".format(", ".join(FAILURES)), flush=True)
    for cid in FAILURES:
        print("  {0:<4} {1}\n       FIX: {2}\n".format(cid, CHECKS[cid][0], CHECKS[cid][1]))
else:
    print("ALL LAYER C CHECKS PASSED", flush=True)
sys.exit(len(FAILURES))
